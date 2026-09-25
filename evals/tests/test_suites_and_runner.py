"""Suite schema validation, assertions, statistics, budget, reports and ``--dry-run``.

Nothing here touches the network or needs an API key.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError

from evals import cli
from evals.paths import SUITE_SCHEMA_PATH
from evals.runners.assertions import ToolCall, Transcript, check_expectations, match_value
from evals.runners.common import (
    Budget,
    BudgetExceeded,
    CaseResult,
    SuiteResult,
    TrialResult,
    pass_hat_k,
)
from evals.schema import Expect, Suite, load_suites, suite_json_schema

# ----------------------------------------------------------------------------- schema


def test_repo_suites_are_valid() -> None:
    suites = load_suites()
    assert {"restaurant_availability", "web_search", "conversation_style"} <= set(suites)
    for suite in suites.values():
        assert suite.cases_for("brain") or suite.cases_for("voice")


def test_committed_json_schema_is_up_to_date() -> None:
    committed = json.loads(SUITE_SCHEMA_PATH.read_text())
    assert committed == json.loads(json.dumps(suite_json_schema())), (
        "run `python -m evals schema --write`"
    )


def _suite(**over: object) -> dict:
    data: dict = {
        "name": "s",
        "profile": "concierge",
        "tiers": ["brain"],
        "tools": ["t"],
        "cases": [{"id": "c", "user": "hi", "expect": {"tool_calls": [{"name": "t"}]}}],
    }
    data.update(over)
    return data


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"cases": [{"id": "c", "user": "hi", "turns": ["x"], "expect": {}}]}, "exactly one"),
        ({"cases": [{"id": "c", "user": "hi", "expect": {"typo": 1}}]}, "Extra inputs"),
        ({"tools": []}, "not declared"),
        ({"cases": [{"id": "c", "user": "x", "expect": {}}] * 2}, "duplicate case ids"),
        ({"cases": [{"id": "c", "user": "x", "tiers": ["voice"], "expect": {}}]}, "not enabled"),
        (
            {
                "cases": [
                    {
                        "id": "c",
                        "user": "x",
                        "expect": {"no_tool_calls": True, "tool_calls": [{"name": "t"}]},
                    }
                ]
            },
            "contradicts",
        ),
        (
            {"cases": [{"id": "c", "user": "x", "expect": {"must_not_match": ["("]}}]},
            "invalid regex",
        ),
    ],
)
def test_suite_validation_rejects(override: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Suite.model_validate(_suite(**override))


# ----------------------------------------------------------------------------- assertions


def test_match_value_operators() -> None:
    today = dt.date(2026, 9, 25)
    assert match_value("Nopa", " nopa ")
    assert match_value(4, "4") and not match_value(4, 5)
    assert match_value({"$regex": "^zuni"}, "Zuni Cafe")
    assert match_value({"$in": ["20:00", "08:00"]}, "08:00")
    assert match_value({"$contains": "bird"}, "State Bird Provisions")
    assert match_value({"$exists": True}, "x") and match_value({"$exists": False}, None)
    assert match_value({"$days_from_today": 1}, "2026-09-26", today=today)
    assert not match_value({"$days_from_today": 1}, "2026-09-25", today=today)


def test_check_expectations_end_to_end() -> None:
    expect = Expect.model_validate(
        {
            "tool_calls": [
                {"name": "check", "args_subset": {"party_size": 4}},
                {"name": "web_search"},
            ],
            "forbidden_tools": ["book"],
            "max_words": 8,
            "must_not_match": ["https?://"],
        }
    )
    transcript = Transcript(
        messages=[
            ("assistant", "Hi, visit https://x.io"),
            ("user", "table?"),
            ("assistant", "One sec."),
            ("assistant", "Seven works."),
        ],
        tool_calls=[ToolCall("check", {"party_size": "4"})],
        unobservable_tools=frozenset({"web_search"}),
    )
    results = {c.name: c for c in check_expectations(expect, transcript)}
    assert results["tool_call:check"].passed
    assert results["tool_call:web_search"].skipped
    assert results["forbidden:book"].passed
    assert results["max_words"].passed and transcript.final_reply == "One sec. Seven works."
    # the greeting (before the first user turn) is not held against guardrails
    assert results["must_not_match:https?://"].passed


def test_argument_mismatch_is_reported() -> None:
    expect = Expect.model_validate(
        {"tool_calls": [{"name": "check", "args_subset": {"time": "19:00"}}]}
    )
    transcript = Transcript(tool_calls=[ToolCall("check", {"time": "07:00"})])
    (result,) = check_expectations(expect, transcript)
    assert not result.passed and "time='07:00'" in result.detail


def test_pass_hat_k_estimator() -> None:
    # fixed k=2: informative for 3 trials (not just 0/1), unbiased C(c,k)/C(n,k)
    assert pass_hat_k(3, 3) == 1.0
    assert pass_hat_k(3, 2) == pytest.approx(1 / 3)
    assert pass_hat_k(3, 1) == 0.0
    assert pass_hat_k(4, 3) == pytest.approx(0.5)
    assert pass_hat_k(1, 1) is None  # fewer than k trials: no estimate
    case = CaseResult(
        "c", [TrialResult(1, True), TrialResult(2, True), TrialResult(3, False)], 0.66
    )
    assert case.passed
    assert case.stats() == {"pass_rate": 0.667, "pass_hat_k": 0.333, "k": 2}


def test_budget_reservations_are_hard() -> None:
    budget = Budget(max_usd=1.0)
    first = budget.reserve(0.4)
    second = budget.reserve(0.4)  # in flight concurrently: 0.8 committed
    with pytest.raises(BudgetExceeded):
        budget.reserve(0.4)
    budget.settle(first, 0.1)
    budget.settle(second, 0.5)  # worst trial is now 0.5 → next reservation uses it
    with pytest.raises(BudgetExceeded):
        budget.reserve(0.01)
    assert budget.spent_usd == pytest.approx(0.6)


# ----------------------------------------------------------------------------- harness + report


class _FakeRunner:
    """Scripted tier runner: trial N passes iff N is in ``passing``."""

    tier = "brain"
    concurrency = 2

    def __init__(self, passing: set[int]) -> None:
        self.passing = passing
        self.calls = 0

    async def setup(self, suite: Suite) -> dict:
        return {"model": "fake"}

    def estimate_trial_usd(self, suite: Suite, case: object) -> float:
        return 0.01

    async def converse(self, suite: Suite, case: object, trial: int):
        from evals.runners.harness import Conversation

        self.calls += 1
        calls = [ToolCall("t", {})] if trial in self.passing else []
        return Conversation(Transcript([("user", "hi"), ("assistant", "ok")], calls), 0.01, 0.1)

    async def aclose(self) -> None:
        return None


def _run(runner: _FakeRunner, budget: Budget, trials: int = 3) -> SuiteResult:
    from evals.runners.harness import HarnessOptions, run_suite

    suite = Suite.model_validate(_suite(trials=trials))
    return asyncio.run(
        run_suite(
            runner,
            suite,
            judge_client=None,
            budget=budget,
            today=None,
            options=HarnessOptions(judge=False),
        )
    )


def test_harness_pass_threshold_and_early_stop() -> None:
    ok = _run(_FakeRunner({1, 2}), Budget(None))
    assert ok.passed and ok.cases[0].pass_rate == pytest.approx(2 / 3)

    runner = _FakeRunner(set())
    bad = _run(runner, Budget(None), trials=5)
    assert not bad.passed
    assert runner.calls == 2  # after 2 failures, 0.67 of 5 is unreachable → stop early


def test_harness_budget_stop() -> None:
    result = _run(_FakeRunner({1, 2, 3}), Budget(max_usd=0.015))
    assert result.status == "budget_exceeded" and not result.passed
    assert len(result.cases[0].trials) == 1


def test_report_writers(tmp_path: Path) -> None:
    from evals.report import aggregate_markdown, write_results

    result = SuiteResult(suite="s", tier="brain", profile="concierge")
    result.cases = [
        CaseResult("good", [TrialResult(1, True, latency_s=1.0)], 0.67),
        CaseResult("bad", [TrialResult(1, False, error="boom", latency_s=2.0)], 0.67),
    ]
    paths = write_results(result, tmp_path)
    junit = ET.parse(paths["junit"]).getroot()
    assert junit.get("tests") == "2" and junit.get("failures") == "1"
    md = aggregate_markdown(tmp_path, None, ["note"])
    assert "brain/s/bad" in md and "1/2" in md


# ----------------------------------------------------------------------------- CLI


def test_dry_run_needs_no_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = cli.main(["run", "--tier", "brain", "--dry-run", "--suite", "restaurant_availability"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "restaurant_availability" in out and "explicit_request" in out


def test_dry_run_unknown_suite_is_config_error() -> None:
    assert cli.main(["run", "--tier", "voice", "--dry-run", "--suite", "nope"]) == cli.EXIT_CONFIG


def test_run_without_key_skips_gracefully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = cli.main(
        [
            "run",
            "--tier",
            "voice",
            "--suite",
            "web_search",
            "--skip-if-no-key",
            "--results-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    data = json.loads((tmp_path / "voice__web_search.json").read_text())
    assert data["status"] == "skipped" and data["passed"] is True
    assert cli.main(["run", "--tier", "voice", "--suite", "web_search"]) == cli.EXIT_CONFIG


def test_validate_cross_checks_agent() -> None:
    assert cli.main(["validate"]) == 0
