"""Agent server entrypoint.

Run it with the built-in CLI subcommands::

    uv run voice-agent console   # talk to the agent from your terminal (no room needed)
    uv run voice-agent dev       # connect to LiveKit (no hot reload; use `lk agent dev` for that)
    uv run voice-agent start     # production worker

LiveKit Agents 1.8 marks that built-in Python CLI as deprecated in favour of the LiveKit CLI
(``lk agent console|dev|start``, or ``python -m livekit.agents start agent/voice_agent/main.py``),
which discovers the module-level ``server`` global. Both paths run the same code.

Dispatch: the worker registers under the agent name ``$LIVEKIT_AGENT_NAME`` (default
``gpt-live-agent``), which enables *explicit* dispatch -- the frontend requests this agent by
name when it creates the room token. Set ``LIVEKIT_AGENT_NAME=`` (empty) to fall back to
automatic dispatch into every new room.

Importing this module is side-effect free apart from loading ``.env``: no API keys are needed
until the worker starts, so tests, evals, and ``--help`` work on a fresh checkout.

Startup validation: :func:`preflight` runs when the worker starts (``console``, ``dev``,
``start``, or ``lk agent ...``), *before* it registers with LiveKit. It checks the same things a
session needs -- settings, prompt composition, tools, the OpenAI key, the analyzer config -- so
a misconfigured worker exits with an actionable message instead of accepting a job and leaving
the caller in a silent room.

Per session, the entrypoint:

1. composes the prompt profile **once** (GPT-Live voice instructions are immutable after start),
   injecting runtime variables (today's date, timezone);
2. resolves the profile's tools through the registry;
3. builds the GPT-Live model (voice brain) with the backend brain's instructions;
4. starts an :class:`AgentSession` and publishes the prompt fingerprint for traceability;
5. logs usage as it accrues and a summary at shutdown;
6. at shutdown, builds a call record from the transcript and sends it to the Call Analyzer
   (or writes it to ``CALL_RECORDS_DIR``); see :mod:`voice_agent.recording`.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

from dotenv import load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    CloseEvent,
    JobContext,
    SessionUsageUpdatedEvent,
)
from livekit.agents import cli as lk_cli

from .agent import VoiceAgent
from .config import Settings, get_settings
from .model import build_gpt_live_model
from .prompts import PromptBundle, PromptComposer
from .recording import CallRecordExporter, build_call_record, new_call_id
from .runtime import runtime_prompt_variables
from .tools import resolve_tools

load_dotenv()

DEFAULT_AGENT_NAME = "gpt-live-agent"
# LiveKit reads LIVEKIT_AGENT_NAME when the session is registered below (the in-code
# `agent_name=` argument is deprecated in 1.8), so provide the default via the environment.
# LiveKit checks the env var before `[agent] name` in livekit.toml, so skip the default when a
# livekit.toml is present (e.g. LiveKit Cloud deploys) to let that file name the agent.
if not os.path.exists("livekit.toml"):
    os.environ.setdefault("LIVEKIT_AGENT_NAME", DEFAULT_AGENT_NAME)

logger = logging.getLogger("voice_agent")


def compose_session_prompts(profile: str | None = None) -> PromptBundle:
    """Compose the prompt bundle for a new session (also handy for debugging)."""
    settings = get_settings()
    return PromptComposer().compose(
        profile or settings.agent_profile,
        extra_variables=runtime_prompt_variables(settings),
    )


def preflight(settings: Settings) -> None:
    """Fail fast on configuration a session would trip over.

    Raises :class:`~voice_agent.config.ConfigurationError` (missing key, analyzer URL without
    token), :class:`~voice_agent.prompts.PromptCompositionError` (bad profile or manifest), or
    :class:`~voice_agent.tools.UnknownToolError` (profile names an unregistered tool). Builds
    nothing that holds connections; it only runs the same checks the entrypoint does.
    """
    bundle = PromptComposer().compose(
        settings.agent_profile, extra_variables=runtime_prompt_variables(settings)
    )
    resolve_tools(bundle.tools, settings)
    settings.require_openai_api_key()
    if settings.call_recording_enabled:
        CallRecordExporter.from_settings(settings)


class ValidatingAgentServer(AgentServer):
    """An :class:`AgentServer` that runs :func:`preflight` before it starts taking jobs.

    ``run`` is the one method every launch path goes through (the ``voice-agent`` script and
    ``lk agent ...``/``python -m livekit.agents`` both end in ``server.run``), and it runs before
    the worker registers with LiveKit. The alternative hook, ``setup_fnc`` (a.k.a. prewarm),
    runs in each job *subprocess* after registration, so the worker would already be accepting
    dispatches when it failed.

    A failed check exits the process with the actionable message (``SystemExit``) rather than
    raising: LiveKit's CLI catches exceptions from ``run``, logs them as a traceback, and then
    drains the never-started worker, which crashes with an unrelated ``AttributeError`` that
    buries the real cause. ``SystemExit`` propagates past that handler and exits with status 1.
    """

    async def run(self, *, devmode: bool = False, unregistered: bool = False) -> None:
        try:
            preflight(get_settings())
        except Exception as exc:  # settings validation, missing keys, bad prompts or tools
            raise SystemExit(f"voice-agent: invalid configuration, not starting: {exc}") from None
        await super().run(devmode=devmode, unregistered=unregistered)


server = ValidatingAgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    started_at = dt.datetime.now(dt.timezone.utc)
    settings = get_settings()
    logging.getLogger("voice_agent").setLevel(settings.log_level.upper())

    bundle = compose_session_prompts(settings.agent_profile)
    # Every log line from this job carries the prompt version.
    ctx.log_context_fields = {
        "profile": bundle.profile,
        "prompt_fingerprint": bundle.version,
    }
    logger.info(
        "composed prompts",
        extra={
            "profile": bundle.profile,
            "prompt_fingerprint": bundle.fingerprint,
            "voice_fingerprint": bundle.fingerprint_for("voice")[:12],
            "backend_fingerprint": bundle.fingerprint_for("backend")[:12],
            "voice_modules": list(bundle.modules["voice"]),
            "backend_modules": list(bundle.modules["backend"]),
            "tools": list(bundle.tools),
            "today": bundle.variables.get("today"),
        },
    )

    tools = resolve_tools(bundle.tools, settings)
    model = build_gpt_live_model(settings, bundle)
    exporter = (
        CallRecordExporter.from_settings(settings) if settings.call_recording_enabled else None
    )

    # A duplex model needs nothing else: no STT/TTS/VAD/turn detector.
    session: AgentSession = AgentSession(llm=model)

    @session.on("session_usage_updated")
    def _on_usage(ev: SessionUsageUpdatedEvent) -> None:
        for usage in ev.usage.model_usage:
            logger.debug("usage updated", extra={"usage": usage.model_dump(exclude_defaults=True)})

    close_reason: str | None = None

    @session.on("close")
    def _on_close(ev: CloseEvent) -> None:
        nonlocal close_reason
        close_reason = close_reason or ev.reason.value

    async def _on_shutdown(shutdown_reason: str) -> None:
        # The job closes the AgentSession before running shutdown callbacks, so the history
        # (including a final interrupted turn) is complete by the time this runs.
        summary = [u.model_dump(exclude_defaults=True) for u in session.usage.model_usage]
        logger.info("session usage", extra={"usage": summary, "prompt_fingerprint": bundle.version})
        if exporter is not None:
            await _record_call(
                ctx,
                session,
                exporter,
                bundle=bundle,
                settings=settings,
                started_at=started_at,
                end_reason=close_reason or shutdown_reason or None,
            )

    ctx.add_shutdown_callback(_on_shutdown)

    # Default RoomOptions publish transcripts both ways on the `lk.transcription` text stream:
    # the agent's speech (synced to audio) and the caller's speech, which GPT-Live transcribes
    # itself (user_transcription=True), attributed to the caller's participant identity.
    await session.start(agent=VoiceAgent(bundle, tools), room=ctx.room)

    # Publish the prompt version on the agent participant so frontends, recordings, and
    # dashboards can tie a conversation to the exact prompts that produced it.
    try:
        await ctx.room.local_participant.set_attributes(bundle.trace_attributes())
    except Exception:  # console mode has no real room; attributes are best-effort anyway
        logger.debug("could not set participant attributes", exc_info=True)


async def _record_call(
    ctx: JobContext,
    session: AgentSession,
    exporter: CallRecordExporter,
    *,
    bundle: PromptBundle,
    settings: Settings,
    started_at: dt.datetime,
    end_reason: str | None,
) -> None:
    """Build this session's call record and export it. Never raises (runs during shutdown)."""
    room = ctx.job.room.name
    call_id = new_call_id(room)
    try:
        record = build_call_record(
            session.history.items,
            call_id=call_id,
            room=room,
            agent_name=ctx.job.agent_name
            or os.environ.get("LIVEKIT_AGENT_NAME")
            or DEFAULT_AGENT_NAME,
            started_at=started_at,
            ended_at=dt.datetime.now(dt.timezone.utc),
            end_reason=end_reason,
            bundle=bundle,
            settings=settings,
            usage=[u.model_dump(mode="json") for u in session.usage.model_usage],
        )
    except Exception:  # a recording bug must not turn a finished call into a job error
        logger.exception("could not build call record", extra={"call_id": call_id})
        return
    if not record["turns"]:
        # e.g. the session failed to start: there is no conversation to analyze.
        logger.info("nothing to record: no conversation turns", extra={"call_id": call_id})
        return
    await exporter.export(record)


def cli() -> None:
    """Console-script entry (``voice-agent``): LiveKit's ``console`` / ``dev`` / ``start``.

    Uses ``livekit.agents.cli.run_app``. LiveKit is steering users toward the ``lk agent ...``
    CLI; the module-level ``server`` global is what that discovers, so both paths work.
    """
    lk_cli.run_app(server)


if __name__ == "__main__":
    cli()
