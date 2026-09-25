"""Convert LiveKit tools into the exact JSON the backend Responses model receives.

This mirrors ``_build_delegation_tools`` in ``livekit/plugins/openai/realtime/gpt_live_model.py``
(livekit-plugins-openai 1.8.3): function tools use LiveKit's *legacy* (non-strict) schema in
the Responses "internally tagged" shape, and OpenAI provider tools serialize via ``to_dict()``
(so ``WebSearch`` becomes ``{"type": "web_search", ...}``). Using the same conversion for both
the brain-tier runner and the change-impact fingerprint means "the schema changed" is judged on
what the model actually sees, not on Python source text.

Import-light on purpose (LiveKit is imported lazily): the impact probe imports this module
while an *older* ``voice_agent`` tree is on ``sys.path``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def tool_to_responses_schema(tool: Any) -> dict[str, Any] | None:
    """Return the Responses API tool definition for ``tool`` or ``None`` if unsupported
    (GPT-Live delegation silently ignores those too)."""
    from livekit.agents import llm

    if isinstance(tool, llm.FunctionTool):
        return llm.utils.build_legacy_openai_schema(tool, internally_tagged=True)
    if isinstance(tool, llm.RawFunctionTool):
        schema = dict(tool.info.raw_schema)
        schema.pop("meta", None)
        schema["type"] = "function"
        return schema
    to_dict = getattr(tool, "to_dict", None)
    if callable(to_dict):  # livekit.plugins.openai.tools.OpenAITool (e.g. WebSearch)
        result = to_dict()
        return dict(result) if isinstance(result, dict) else None
    return None


def tools_to_responses_schemas(tools: Iterable[Any]) -> list[dict[str, Any]]:
    return [s for s in (tool_to_responses_schema(t) for t in tools) if s is not None]


def schema_tool_name(schema: dict[str, Any]) -> str:
    """Stable display name: function name, or the provider tool ``type`` (e.g. ``web_search``)."""
    return str(schema.get("name") or schema.get("type") or "?")
