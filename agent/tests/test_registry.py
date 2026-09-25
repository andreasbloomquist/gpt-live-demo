"""Tool registry: name resolution, tool schemas, and reservation-provider selection."""

from __future__ import annotations

import pytest
from helpers import make_settings
from livekit.agents import FunctionTool
from livekit.agents.llm import utils as llm_utils
from livekit.plugins.openai.tools import WebSearch

from voice_agent.config import ConfigurationError, Settings
from voice_agent.tools import TOOL_REGISTRY, UnknownToolError, resolve_tools
from voice_agent.tools.restaurants import OpenTableProvider, build_reservation_provider


def test_registry_keys() -> None:
    assert set(TOOL_REGISTRY) == {"web_search", "check_restaurant_availability"}
    for name, spec in TOOL_REGISTRY.items():
        assert spec.name == name and spec.source_modules


def test_resolve_tools_preserves_order_and_dedupes(settings: Settings) -> None:
    tools = resolve_tools(["check_restaurant_availability", "web_search", "web_search"], settings)
    assert isinstance(tools[0], FunctionTool)
    assert tools[0].info.name == "check_restaurant_availability"
    assert isinstance(tools[1], WebSearch)
    assert tools[1].search_context_size == "low"
    assert len(tools) == 2


def test_function_tool_schema_is_strict_compatible(settings: Settings) -> None:
    (tool,) = resolve_tools(["check_restaurant_availability"], settings)
    assert isinstance(tool, FunctionTool)
    schema = llm_utils.build_strict_openai_schema(tool)["function"]
    assert set(schema["parameters"]["properties"]) == {
        "restaurant",
        "date",
        "time",
        "party_size",
        "city",
    }
    assert "does not book" in schema["description"]


def test_unknown_tool(settings: Settings) -> None:
    with pytest.raises(UnknownToolError):
        resolve_tools(["nope"], settings)


def test_opentable_requires_credentials() -> None:
    settings = make_settings(restaurant_provider="opentable")
    with pytest.raises(ConfigurationError, match="OPENTABLE_CLIENT_ID"):
        resolve_tools(["check_restaurant_availability"], settings)


def test_opentable_rejects_empty_secret() -> None:
    settings = make_settings(
        restaurant_provider="opentable",
        opentable_client_id="id",
        opentable_client_secret="",
    )
    with pytest.raises(ConfigurationError, match="OPENTABLE_CLIENT_SECRET"):
        build_reservation_provider(settings)


def test_opentable_provider_is_per_session() -> None:
    # Not cached per process: its asyncio.Lock binds to one event loop, and LiveKit's thread
    # executor runs each job on its own loop.
    settings = make_settings(
        restaurant_provider="opentable",
        opentable_client_id="id",
        opentable_client_secret="secret",
    )
    first = build_reservation_provider(settings)
    assert isinstance(first, OpenTableProvider)
    assert build_reservation_provider(settings) is not first
