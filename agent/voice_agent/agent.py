"""The LiveKit :class:`~livekit.agents.Agent` for this demo.

Why prompts are composed once per session: GPT-Live's voice instructions are **immutable after
the session starts** (the model reports ``mutable_instructions=False``, and changing them raises
``RealtimeError``). So the agent is constructed from a fully-rendered, frozen
:class:`~voice_agent.prompts.PromptBundle`; there's no point at which prompt modules could be
hot-swapped mid-call. For small mid-session nudges, GPT-Live offers
``agent.duplex_session.append_instructions(...)`` (a standing rule, ≤500 tokens) instead.

Because the bundle is frozen, its fingerprint uniquely identifies what the model was told.
``main.py`` logs it and publishes it as participant attributes, so any transcript can be tied
back to the exact prompt version and to the evals that ran against it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from livekit.agents import Agent, llm

from .prompts import PromptBundle

logger = logging.getLogger(__name__)

DEFAULT_GREETING = "Greet the caller briefly and ask how you can help."


class VoiceAgent(Agent):
    """An agent whose voice instructions and tools come entirely from a prompt bundle."""

    def __init__(self, bundle: PromptBundle, tools: Sequence[llm.Tool]) -> None:
        super().__init__(instructions=bundle.voice_instructions, tools=list(tools))
        self.bundle = bundle

    async def on_enter(self) -> None:
        """Speak first, so the caller isn't met with silence."""
        logger.info(
            "agent entered", extra={"profile": self.bundle.profile, "prompt": self.bundle.version}
        )
        # GPT-Live decides turn-taking itself; generate_reply with an instruction is delivered
        # as one-off commentary asking it to speak first. session.say() raises on a duplex
        # model (there is no TTS and the model can't speak fixed text), and interrupt() is a
        # no-op (barge-in is the model's own), so generate_reply is the way to prompt speech.
        self.session.generate_reply(instructions=self.bundle.greeting or DEFAULT_GREETING)
