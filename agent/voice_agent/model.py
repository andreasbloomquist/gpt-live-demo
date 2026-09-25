"""Build the GPT-Live duplex model from settings + a composed prompt bundle.

GPT-Live runs two brains:

* the **voice model** (``gpt-live-1``) listens and speaks full-duplex and decides turn-taking
  itself -- no VAD, STT, TTS, or turn detector in the pipeline. Its instructions are the
  bundle's ``voice_instructions`` and are passed via the :class:`~livekit.agents.Agent`.
* the **backend Responses model** receives delegated work: reasoning and every tool call.
  Its instructions are the bundle's ``backend_instructions`` and are passed here, through
  ``responses_options``.

Reasoning effort and verbosity default to *low*: on a phone call, latency matters more than
depth, and the voice model paraphrases whatever the backend returns, so short is better.
"""

from __future__ import annotations

from livekit.plugins.openai.realtime import GPTLiveModel, ResponsesDelegationOptions

from .config import Settings
from .prompts import PromptBundle


def build_responses_options(settings: Settings, bundle: PromptBundle) -> ResponsesDelegationOptions:
    """Backend (delegation) options. Exposed separately so evals can reuse the exact config."""
    options = ResponsesDelegationOptions(
        model=settings.gpt_live_backend_model,
        instructions=bundle.backend_instructions,
        reasoning={"effort": settings.gpt_live_backend_reasoning_effort},
        text={"verbosity": settings.gpt_live_backend_verbosity},
        parallel_tool_calls=True,
    )
    if settings.gpt_live_backend_max_output_tokens is not None:
        options["max_output_tokens"] = settings.gpt_live_backend_max_output_tokens
    return options


def build_gpt_live_model(settings: Settings, bundle: PromptBundle) -> GPTLiveModel:
    """Create the :class:`GPTLiveModel` for one session.

    Raises :class:`~voice_agent.config.ConfigurationError` if ``OPENAI_API_KEY`` is missing --
    at session start, never at import time.
    """
    return GPTLiveModel(
        model=settings.gpt_live_model,
        voice=settings.gpt_live_voice,
        delegation="responses",  # framework @function_tools + provider tools run on the backend
        responses_options=build_responses_options(settings, bundle),
        api_key=settings.require_openai_api_key(),
    )
