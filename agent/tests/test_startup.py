"""Call-recording settings and the worker's startup (preflight) validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from voice_agent import main
from voice_agent.config import ConfigurationError, Settings
from voice_agent.recording import CallRecordExporter
from voice_agent.tools import UnknownToolError


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# --- Settings ------------------------------------------------------------------------------


def test_recording_defaults_write_to_local_dir() -> None:
    settings = make_settings()
    assert settings.call_recording_enabled is True
    assert settings.call_records_dir == Path(".call-records")
    assert settings.call_analyzer_endpoint() is None


def test_analyzer_url_without_token_is_a_configuration_error() -> None:
    settings = make_settings(call_analyzer_url="http://localhost:8080")
    with pytest.raises(ConfigurationError, match="CALL_ANALYZER_TOKEN"):
        settings.call_analyzer_endpoint()
    with pytest.raises(ConfigurationError, match="CALL_ANALYZER_TOKEN"):
        CallRecordExporter.from_settings(settings)


def test_analyzer_endpoint_normalizes_url() -> None:
    settings = make_settings(
        call_analyzer_url=" https://calls.example.com/ ", call_analyzer_token="t"
    )
    assert settings.call_analyzer_endpoint() == ("https://calls.example.com", "t")


def test_blank_analyzer_env_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_ANALYZER_URL", "")
    monkeypatch.setenv("CALL_ANALYZER_TOKEN", "")
    assert make_settings().call_analyzer_endpoint() is None


def test_analyzer_url_must_be_http() -> None:
    with pytest.raises(ValidationError, match="CALL_ANALYZER_URL"):
        make_settings(call_analyzer_url="localhost:8080")


def test_token_is_not_exposed_in_repr() -> None:
    settings = make_settings(call_analyzer_url="http://a.test", call_analyzer_token="s3cret")
    assert "s3cret" not in repr(settings)


# --- preflight -------------------------------------------------------------------------------


def test_preflight_passes_with_a_complete_config() -> None:
    main.preflight(make_settings(openai_api_key="sk-test"))


def test_preflight_requires_openai_key() -> None:
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        main.preflight(make_settings())


def test_preflight_checks_analyzer_config_only_when_recording() -> None:
    half_configured = {"openai_api_key": "sk-test", "call_analyzer_url": "http://a.test"}
    with pytest.raises(ConfigurationError, match="CALL_ANALYZER_TOKEN"):
        main.preflight(make_settings(**half_configured))
    main.preflight(make_settings(**half_configured, call_recording_enabled=False))


def test_preflight_checks_tool_configuration() -> None:
    settings = make_settings(openai_api_key="sk-test", restaurant_provider="opentable")
    with pytest.raises(ConfigurationError, match="OPENTABLE_CLIENT_ID"):
        main.preflight(settings)


def test_preflight_rejects_unknown_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    def unknown(names: object, settings: Settings) -> list[Any]:
        raise UnknownToolError("unknown tool 'fax'")

    monkeypatch.setattr(main, "resolve_tools", unknown)
    with pytest.raises(UnknownToolError):
        main.preflight(make_settings(openai_api_key="sk-test"))


async def test_worker_validates_before_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict[str, bool]] = []

    async def fake_run(self: object, *, devmode: bool, unregistered: bool) -> None:
        started.append({"devmode": devmode, "unregistered": unregistered})

    monkeypatch.setattr(main.AgentServer, "run", fake_run)

    exits: list[str] = []
    monkeypatch.setattr(main, "_exit_with_error", exits.append)

    monkeypatch.setattr(main, "get_settings", make_settings)
    await main.server.run(devmode=False, unregistered=False)
    assert "OPENAI_API_KEY" in exits[-1]
    assert started == []  # never registered with LiveKit, so no job can be dispatched to it

    def invalid_settings() -> Settings:
        return Settings(_env_file=None, agent_timezone="Mars/Base")  # type: ignore[call-arg]

    monkeypatch.setattr(main, "get_settings", invalid_settings)
    await main.server.run(devmode=False, unregistered=False)
    assert "AGENT_TIMEZONE" in exits[-1]
    assert started == []

    monkeypatch.setattr(main, "get_settings", lambda: make_settings(openai_api_key="sk-test"))
    await main.server.run(devmode=True, unregistered=False)
    assert started == [{"devmode": True, "unregistered": False}]
