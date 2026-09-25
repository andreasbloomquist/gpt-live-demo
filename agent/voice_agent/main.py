"""Agent server entrypoint.

Run it with the LiveKit CLI subcommands::

    uv run voice-agent console   # talk to the agent from your terminal (no room needed)
    uv run voice-agent dev       # connect to LiveKit with hot reload
    uv run voice-agent start     # production worker

Importing this module is side-effect free apart from loading ``.env``: no API keys are needed
until a session actually starts, so tests, evals, and ``--help`` work on a fresh checkout.

Per session, the entrypoint:

1. composes the prompt profile **once** (GPT-Live voice instructions are immutable after start),
   injecting runtime variables (today's date, timezone);
2. resolves the profile's tools through the registry;
3. builds the GPT-Live model (voice brain) with the backend brain's instructions;
4. starts an :class:`AgentSession` and publishes the prompt fingerprint for traceability;
5. logs usage as it accrues and a summary at shutdown.
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    SessionUsageUpdatedEvent,
)
from livekit.agents import cli as lk_cli

from .agent import VoiceAgent
from .config import get_settings
from .model import build_gpt_live_model
from .prompts import PromptBundle, PromptComposer
from .runtime import runtime_prompt_variables
from .tools import resolve_tools

load_dotenv()

logger = logging.getLogger("voice_agent")

server = AgentServer()


def compose_session_prompts(profile: str | None = None) -> PromptBundle:
    """Compose the prompt bundle for a new session (also handy for debugging)."""
    settings = get_settings()
    return PromptComposer().compose(
        profile or settings.agent_profile,
        extra_variables=runtime_prompt_variables(settings),
    )


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
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

    # A duplex model needs nothing else: no STT/TTS/VAD/turn detector.
    session: AgentSession = AgentSession(llm=model)

    @session.on("session_usage_updated")
    def _on_usage(ev: SessionUsageUpdatedEvent) -> None:
        for usage in ev.usage.model_usage:
            logger.debug("usage updated", extra={"usage": usage.model_dump(exclude_defaults=True)})

    async def _log_final_usage() -> None:
        summary = [u.model_dump(exclude_defaults=True) for u in session.usage.model_usage]
        logger.info("session usage", extra={"usage": summary, "prompt_fingerprint": bundle.version})

    ctx.add_shutdown_callback(_log_final_usage)

    await session.start(agent=VoiceAgent(bundle, tools), room=ctx.room)

    # Publish the prompt version on the agent participant so frontends, recordings, and
    # dashboards can tie a conversation to the exact prompts that produced it.
    try:
        await ctx.room.local_participant.set_attributes(bundle.trace_attributes())
    except Exception:  # console mode has no real room; attributes are best-effort anyway
        logger.debug("could not set participant attributes", exc_info=True)


def cli() -> None:
    """Console-script entry (``voice-agent``): LiveKit's ``console`` / ``dev`` / ``start``.

    Uses ``livekit.agents.cli.run_app``. LiveKit is steering users toward the ``lk agent ...``
    CLI; the module-level ``server`` global is what that discovers, so both paths work.
    """
    lk_cli.run_app(server)


if __name__ == "__main__":
    cli()
