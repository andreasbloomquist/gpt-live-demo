from __future__ import annotations

import datetime as dt
import subprocess
import sys

import pytest
from pydantic import ValidationError

from voice_agent.config import ConfigurationError, Settings
from voice_agent.model import build_gpt_live_model, build_responses_options
from voice_agent.prompts import PromptComposer
from voice_agent.runtime import runtime_prompt_variables


def test_responses_options_use_backend_prompt(settings: Settings) -> None:
    bundle = PromptComposer().compose("concierge")
    opts = build_responses_options(settings, bundle)
    assert opts["instructions"] == bundle.backend_instructions
    assert opts["model"] == "gpt-5.6-luna"
    assert opts["reasoning"] == {"effort": "low"}
    assert opts["text"] == {"verbosity": "low"}
    assert "max_output_tokens" not in opts


def test_model_requires_api_key_only_when_built(settings: Settings) -> None:
    bundle = PromptComposer().compose("concierge")
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        build_gpt_live_model(settings, bundle)
    keyed = Settings(_env_file=None, openai_api_key="sk-test")  # type: ignore[call-arg]
    model = build_gpt_live_model(keyed, bundle)
    assert model.capabilities.mutable_instructions is False


def test_runtime_variables_in_agent_timezone(settings: Settings) -> None:
    now = dt.datetime(2026, 9, 26, 3, 0, tzinfo=dt.timezone.utc)  # still the 25th in LA
    values = runtime_prompt_variables(settings, now=now)
    assert values == {"today": "Friday, 2026-09-25", "timezone": "America/Los_Angeles"}


def test_unknown_timezone_is_rejected_at_startup() -> None:
    with pytest.raises(ValidationError, match="AGENT_TIMEZONE"):
        Settings(_env_file=None, agent_timezone="Mars/Olympus")  # type: ignore[call-arg]


def test_runtime_zone_still_falls_back_to_utc_defensively() -> None:
    # Settings validation normally prevents this; the fallback guards settings built without it.
    settings = Settings.model_construct(agent_timezone="Mars/Olympus")
    now = dt.datetime(2026, 9, 26, 3, 0, tzinfo=dt.timezone.utc)
    assert runtime_prompt_variables(settings, now=now)["today"] == "Saturday, 2026-09-26"


def test_log_level_is_case_insensitive_and_validated() -> None:
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="LOUD")  # type: ignore[call-arg]


def test_main_imports_without_keys() -> None:
    env = {"PATH": "", "PYTHONPATH": ":".join(sys.path)}
    proc = subprocess.run(
        [sys.executable, "-c", "import voice_agent.main as m; assert m.server"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
