"""Shared fixtures. Tests never touch the network and need no API keys."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from helpers import PromptTree, make_settings

from voice_agent.config import Settings


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide real credentials from the environment so no test can use them by accident."""
    for var in ("OPENAI_API_KEY", "OPENTABLE_CLIENT_ID", "OPENTABLE_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings() -> Settings:
    """Default settings, unaffected by any developer ``.env`` so tests are hermetic."""
    return make_settings()


@pytest.fixture
def prompt_tree(tmp_path: Path) -> PromptTree:
    """Write a minimal prompts dir: ``prompt_tree(manifest_yaml, {module_id: file_text})``."""

    def make(manifest: str, modules: dict[str, str]) -> Path:
        root = tmp_path / "prompts"
        (root / "modules").mkdir(parents=True, exist_ok=True)
        (root / "manifest.yaml").write_text(textwrap.dedent(manifest), encoding="utf-8")
        for module_id, text in modules.items():
            path = root / "modules" / f"{module_id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
        return root

    return make
