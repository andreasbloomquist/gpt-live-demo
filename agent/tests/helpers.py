"""Test helpers shared across test modules (fixtures live in ``conftest.py``)."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

from voice_agent.config import Settings

PromptTree = Callable[[str, dict[str, str]], Path]
"""The ``prompt_tree`` fixture: ``(manifest_yaml, {module_id: file_text}) -> prompts dir``."""


def make_settings(**overrides: Any) -> Settings:
    """Settings from ``overrides`` and the environment only, ignoring any developer ``.env``."""
    # pydantic-settings accepts `_env_file` at runtime but doesn't declare it to type checkers.
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def module(
    module_id: str,
    body: str,
    *,
    target: str = "voice",
    variables: list[str] | None = None,
    requires_tools: list[str] | None = None,
) -> str:
    """Build module file text with front matter."""
    lines = ["---", f"id: {module_id}", "version: 1", f"target: {target}", "description: test"]
    if variables is not None:
        lines.append(f"variables: [{', '.join(variables)}]")
    if requires_tools is not None:
        lines.append(f"requires_tools: [{', '.join(requires_tools)}]")
    lines.append("---")
    return "\n".join(lines) + "\n" + textwrap.dedent(body).strip() + "\n"
