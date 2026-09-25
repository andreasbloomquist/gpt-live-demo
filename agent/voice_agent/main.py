"""Agent server entrypoint.

Run it with the built-in CLI subcommands::

    uv run voice-agent console   # talk to the agent from your terminal (no room needed)
    uv run voice-agent dev       # connect to LiveKit (no hot reload; use `lk agent dev` for that)
    uv run voice-agent start     # production worker

LiveKit Agents 1.8 marks that built-in Python CLI as deprecated in favor of the LiveKit CLI
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
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    CloseEvent,
    JobContext,
    SessionUsageUpdatedEvent,
)
from livekit.agents import cli as lk_cli
from pydantic import SecretStr

from .agent import VoiceAgent
from .config import ConfigurationError, Settings, get_settings
from .model import build_gpt_live_model
from .prompts import PromptBundle, PromptComposer
from .recording import CallRecordExporter, build_call_record, new_call_id
from .runtime import runtime_prompt_variables
from .tools import resolve_tools

# Same `.env` that Settings reads (the working directory), so LiveKit's own variables and ours
# always come from one file. Without `usecwd`, python-dotenv searches upward from this module.
load_dotenv(find_dotenv(usecwd=True))

DEFAULT_AGENT_NAME = "gpt-live-agent"
# LiveKit reads LIVEKIT_AGENT_NAME when the session is registered below (the in-code
# `agent_name=` argument is deprecated in 1.8), so provide the default via the environment.
# LiveKit checks the env var before `[agent] name` in livekit.toml, so skip the default when a
# livekit.toml is present (e.g. LiveKit Cloud deploys) to let that file name the agent.
if not Path("livekit.toml").exists():
    os.environ.setdefault("LIVEKIT_AGENT_NAME", DEFAULT_AGENT_NAME)

logger = logging.getLogger("voice_agent")


def compose_session_prompts(
    profile: str | None = None, *, settings: Settings | None = None
) -> PromptBundle:
    """Compose the prompt bundle for a new session (also handy for debugging).

    Defaults to ``settings.agent_profile`` and the process settings; runtime variables
    (today's date, timezone) are computed now, in the agent's timezone.
    """
    if settings is None:
        settings = get_settings()
    return PromptComposer().compose(
        profile or settings.agent_profile,
        extra_variables=runtime_prompt_variables(settings),
    )


# Values shipped in the example env files. Starting with one of them can never work, and without
# this check the worker would retry the LiveKit connection instead of saying what's wrong.
_EXAMPLE_PLACEHOLDERS: dict[str, str] = {
    "LIVEKIT_URL": "wss://your-project.livekit.cloud",
    "LIVEKIT_API_KEY": "your_livekit_api_key",
    "LIVEKIT_API_SECRET": "your_livekit_api_secret",
    "OPENAI_API_KEY": "sk-your-openai-api-key",
    "CALL_ANALYZER_TOKEN": "change-me-shared-secret",
}


_LIVEKIT_ENV_VARS = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")


def _placeholder_settings(settings: Settings) -> list[str]:
    """Names of settings still holding a value copied verbatim from ``.env.example``."""
    configured = {
        # LiveKit reads its credentials straight from the environment, not from Settings.
        **{
            name: os.environ.get(name)
            for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
        },
        "OPENAI_API_KEY": _secret(settings.openai_api_key),
        "CALL_ANALYZER_TOKEN": _secret(settings.call_analyzer_token),
    }
    return [name for name, value in configured.items() if value == _EXAMPLE_PLACEHOLDERS[name]]


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def preflight(settings: Settings) -> None:
    """Fail fast on configuration a session would trip over.

    Raises :class:`~voice_agent.config.ConfigurationError` (missing or placeholder key,
    analyzer URL without token), :class:`~voice_agent.prompts.PromptCompositionError` (bad
    profile or manifest), or :class:`~voice_agent.tools.UnknownToolError` (profile names an
    unregistered tool). Builds nothing that holds connections; it only runs the same checks
    the entrypoint does, plus one for values left over from ``.env.example``.
    """
    if placeholders := _placeholder_settings(settings):
        raise ConfigurationError(
            "these settings still have their example values from .env.example: "
            f"{', '.join(placeholders)}. Replace them with your real credentials."
        )
    bundle = compose_session_prompts(settings=settings)
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

    A failed check prints one actionable line and exits with status 1. Raising doesn't work
    well here: LiveKit's CLI runs ``run`` in an unawaited task, catches exceptions from it,
    logs a long traceback and then drains the never-started worker, which crashes with an
    unrelated ``AttributeError``; even ``SystemExit`` gets logged as "Task exception was never
    retrieved". Nothing has started at this point (no registration, no connections, no child
    processes), so exiting immediately is safe.
    """

    async def run(self, *, devmode: bool = False, unregistered: bool = False) -> None:
        try:
            preflight(get_settings())
        except Exception as exc:  # settings validation, missing keys, bad prompts or tools
            _exit_with_error(f"voice-agent: invalid configuration, not starting: {exc}")
            return
        await super().run(devmode=devmode, unregistered=unregistered)


def _exit_with_error(message: str) -> None:
    """Print ``message`` to stderr and end the process with status 1 (patched in tests)."""
    print(message, file=sys.stderr, flush=True)
    logging.shutdown()
    os._exit(1)


server = ValidatingAgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    """Run one session (one room); the steps are listed in the module docstring."""
    started_at = dt.datetime.now(dt.timezone.utc)
    settings = get_settings()
    logger.setLevel(settings.log_level)

    bundle = compose_session_prompts(settings=settings)
    # Every log line from this job carries the prompt version.
    ctx.log_context_fields = {
        "profile": bundle.profile,
        "prompt_version": bundle.version,
    }
    _log_composed_prompts(bundle)

    tools = resolve_tools(bundle.tools, settings)
    model = build_gpt_live_model(settings, bundle)
    exporter = (
        CallRecordExporter.from_settings(settings) if settings.call_recording_enabled else None
    )

    # A duplex model needs nothing else: no STT/TTS/VAD/turn detector.
    session: AgentSession[None] = AgentSession(llm=model)

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
        logger.info("session usage", extra={"usage": summary, "prompt_version": bundle.version})
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


def _log_composed_prompts(bundle: PromptBundle) -> None:
    logger.info(
        "composed prompts",
        extra={
            "profile": bundle.profile,
            "prompt_fingerprint": bundle.fingerprint,
            "voice_version": bundle.short_fingerprint_for("voice"),
            "backend_version": bundle.short_fingerprint_for("backend"),
            "voice_modules": list(bundle.modules["voice"]),
            "backend_modules": list(bundle.modules["backend"]),
            "tools": list(bundle.tools),
            "today": bundle.variables.get("today"),
        },
    )


async def _record_call(
    ctx: JobContext,
    session: AgentSession[None],
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
