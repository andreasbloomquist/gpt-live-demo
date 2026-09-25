"""Helpers for building throwaway prompt trees in tests (imported by test modules)."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

PromptTree = Callable[..., Path]


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
