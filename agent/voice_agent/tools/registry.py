"""Tool registry: maps the tool *names* used in ``prompts/manifest.yaml`` to implementations.

Profiles declare tools by name so prompts and tools stay in sync (the composer refuses a skill
module whose ``requires_tools`` aren't enabled). This registry is the single place that turns
those names into LiveKit tool objects for a session.

Adding a tool:

1. Implement it (a ``@function_tool`` or an OpenAI provider tool) under ``voice_agent/tools``.
2. Register a :class:`ToolSpec` below with a factory ``(Settings) -> list[tool]`` and the
   Python modules implementing it (``source_modules`` lets the eval change-detector know which
   code changes affect which tool).
3. Add the name to a profile's ``tools`` list and write its voice/backend prompt modules.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from livekit.agents import llm

from ..config import Settings
from .restaurants import (
    build_reservation_provider,
    build_restaurant_availability_tool,
    local_today,
)
from .web_search import build_web_search_tool

AgentTool = llm.Tool  # FunctionTool, RawFunctionTool and ProviderTool are all llm.Tool
ToolFactory = Callable[[Settings], list[AgentTool]]


class UnknownToolError(KeyError):
    """A profile asked for a tool name that isn't registered."""


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool.

    ``name`` is what profiles list under ``tools``; ``factory`` builds the session's tool
    objects from settings; ``source_modules`` are the Python modules implementing the tool,
    used by the eval change-detector to map code changes to the tools they affect.
    """

    name: str
    description: str
    factory: ToolFactory
    source_modules: tuple[str, ...]


def _web_search(settings: Settings) -> list[AgentTool]:
    return [build_web_search_tool(settings)]


def _restaurant_availability(settings: Settings) -> list[AgentTool]:
    return [
        build_restaurant_availability_tool(
            build_reservation_provider(settings), clock=local_today(settings.agent_timezone)
        )
    ]


TOOL_REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="web_search",
            description="OpenAI hosted web search, executed by the backend Responses model.",
            factory=_web_search,
            source_modules=("voice_agent.tools.web_search",),
        ),
        ToolSpec(
            name="check_restaurant_availability",
            description="Check (never book) restaurant availability via mock or OpenTable.",
            factory=_restaurant_availability,
            source_modules=("voice_agent.tools.restaurants",),
        ),
    )
}


def resolve_tools(names: Iterable[str], settings: Settings) -> list[AgentTool]:
    """Instantiate the tools for ``names`` (order preserved, duplicates ignored).

    Raises :class:`UnknownToolError` for unregistered names, so a typo in the manifest fails
    at session start rather than silently producing an agent without the tool.
    """
    tools: list[AgentTool] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        spec = TOOL_REGISTRY.get(name)
        if spec is None:
            raise UnknownToolError(
                f"unknown tool {name!r}; registered: {', '.join(sorted(TOOL_REGISTRY))}"
            )
        tools.extend(spec.factory(settings))
    return tools
