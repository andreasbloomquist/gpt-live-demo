"""The shipped prompts/ directory must always compose cleanly."""

from __future__ import annotations

import re

import pytest

from voice_agent.prompts import DEFAULT_PROMPTS_DIR, PromptComposer
from voice_agent.tools import TOOL_REGISTRY


@pytest.fixture(scope="module")
def composer() -> PromptComposer:
    return PromptComposer()


def test_default_dir_is_repo_prompts() -> None:
    assert (DEFAULT_PROMPTS_DIR / "manifest.yaml").is_file()


def test_every_profile_composes(composer: PromptComposer) -> None:
    assert "concierge" in composer.list_profiles()
    for profile in composer.list_profiles():
        bundle = composer.compose(
            profile, extra_variables={"today": "Friday, 2026-09-25", "timezone": "UTC"}
        )
        assert bundle.voice_instructions and bundle.backend_instructions
        assert "<runtime:" not in bundle.voice_instructions + bundle.backend_instructions
        assert "<!--" not in bundle.voice_instructions + bundle.backend_instructions
        assert set(bundle.tools) <= set(TOOL_REGISTRY), "profile references unregistered tool"


def test_concierge_prompt_content(composer: PromptComposer) -> None:
    bundle = composer.compose("concierge", extra_variables={"today": "Friday, 2026-09-25"})
    assert bundle.tools == ("web_search", "check_restaurant_availability")
    assert "Friday, 2026-09-25" in bundle.backend_instructions
    # The voice prompt must not contain URLs (it's heard, never read).
    assert not re.search(r"https?://", bundle.voice_instructions)
    assert bundle.greeting


def test_concierge_fingerprint_is_deterministic(composer: PromptComposer) -> None:
    assert (
        composer.compose("concierge").fingerprint
        == PromptComposer().compose("concierge").fingerprint
    )
