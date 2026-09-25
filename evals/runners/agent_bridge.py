"""The only place the eval runners touch ``voice_agent``.

Runners build the agent exactly the way production does (same composer, runtime variables,
tool registry and backend options), so an eval exercises the shipped configuration rather
than a test double. Imports are lazy so ``--dry-run`` and ``validate`` stay fast and the
module imports cleanly even when the agent package is mid-refactor.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any


def load_settings() -> Any:
    """Agent settings, forced onto the deterministic mock reservation provider.

    Evals must be reproducible and must never hit a partner API, regardless of the
    developer's ``.env``.
    """
    os.environ["RESTAURANT_PROVIDER"] = "mock"
    from voice_agent.config import Settings

    return Settings()


def compose_bundle(profile: str, settings: Any, *, runtime_variables: bool = True) -> Any:
    """Compose a profile's prompt bundle.

    ``runtime_variables=True`` composes it exactly as a new session does
    (``compose_session_prompts``), with the real ``today``/``timezone`` so relative dates
    ("tomorrow") resolve as in production; ``False`` keeps the stable placeholders, which is
    all ``validate``/``--dry-run`` need.
    """
    if runtime_variables:
        from voice_agent.main import compose_session_prompts

        return compose_session_prompts(profile, settings=settings)
    from voice_agent.prompts import PromptComposer

    return PromptComposer().compose(profile)


def resolve_tools(bundle: Any, settings: Any) -> list[Any]:
    from voice_agent.tools import resolve_tools as _resolve

    return list(_resolve(bundle.tools, settings))


def responses_options(settings: Any, bundle: Any) -> dict[str, Any]:
    """The exact ``ResponsesDelegationOptions`` GPT-Live sends to its backend model."""
    from voice_agent.model import build_responses_options

    return dict(build_responses_options(settings, bundle))


def agent_today(settings: Any) -> dt.date:
    """Today in the agent's timezone: what ``$days_from_today`` matchers compare against."""
    from voice_agent.runtime import zone

    return dt.datetime.now(zone(settings.agent_timezone)).date()


def openai_api_key(settings: Any) -> str:
    return str(settings.require_openai_api_key())
