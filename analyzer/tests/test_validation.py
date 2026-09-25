"""Output validation: what the analyzer keeps from a provider's (untrusted) draft."""

from __future__ import annotations

from typing import Any

import pytest

from call_analyzer.analysis import CallAnalyzer, validate_draft
from call_analyzer.models import DIMENSIONS, AssessmentDraft, CallRecord, Metrics
from call_analyzer.providers.base import ProviderError
from call_analyzer.rubric import Rubric
from tests.factories import make_record


def draft(**score_overrides: Any) -> AssessmentDraft:
    """A draft for the factory record with every dimension scored 4 unless overridden."""
    scores = {
        dim: {"score": 4, "rationale": "fine", "evidence": []} for dim in DIMENSIONS
    } | score_overrides
    return AssessmentDraft.model_validate(
        {
            "summary": "  The caller asked about Nopa.  ",
            "caller_intent": "Table at Nopa",
            "outcome": {"status": "resolved", "reason": "Open."},
            "scores": scores,
            "flags": [],
            "sentiment": [],
        }
    )


class FixedProvider:
    name = "openai"
    model = "fake-model"

    def __init__(self, result: AssessmentDraft) -> None:
        self.result = result

    async def assess(self, record: CallRecord, metrics: Metrics, rubric: Rubric) -> AssessmentDraft:
        return self.result

    async def aclose(self) -> None:
        return None


def test_evidence_must_appear_in_the_cited_turn() -> None:
    record = make_record()
    evidence = [
        {"turn_id": "t3", "quote": "Seven on Friday is open for two."},  # exact: kept
        {"turn_id": "t3", "quote": "“I HAVEN’T  booked it…”"},  # normalized: kept
        {"turn_id": "t3", "quote": "I booked it for you"},  # not in the turn: dropped
        {"turn_id": "t2", "quote": "Seven on Friday is open"},  # wrong turn: dropped
        {"turn_id": "nope", "quote": "Perfect"},  # unknown turn: dropped
        {"turn_id": "t4", "quote": "."},  # too short once unwrapped: dropped
    ]
    scores, _, _ = validate_draft(
        draft(resolution={"score": 5, "rationale": "r", "evidence": evidence}), record
    )
    kept = [(e.turn_id, e.quote) for e in scores["resolution"].evidence]
    assert kept == [
        ("t3", "Seven on Friday is open for two"),
        ("t3", "I HAVEN’T  booked it"),
    ]


@pytest.mark.parametrize("bad", [0, 6, -1, 5.6, float("nan"), float("inf")])
def test_out_of_range_scores_are_rejected_not_clamped(bad: float) -> None:
    scores, _, _ = validate_draft(
        draft(efficiency={"score": bad, "rationale": "r", "evidence": []}), make_record()
    )
    assert "efficiency" not in scores
    assert len(scores) == len(DIMENSIONS) - 1


def test_in_range_fractions_round_half_up() -> None:
    scores, _, _ = validate_draft(
        draft(
            efficiency={"score": 3.5, "rationale": "r", "evidence": []},
            resolution={"score": 4.49, "rationale": "r", "evidence": []},
        ),
        make_record(),
    )
    assert scores["efficiency"].score == 4
    assert scores["resolution"].score == 4


def test_mostly_invalid_scores_fail_the_analysis_as_retryable() -> None:
    bad = {dim: {"score": 9, "rationale": "r", "evidence": []} for dim in DIMENSIONS[:5]}
    with pytest.raises(ProviderError) as info:
        validate_draft(draft(**bad), make_record())
    assert info.value.retryable


def test_sentiment_and_flags_are_sanitized() -> None:
    messy = AssessmentDraft.model_validate(
        draft().model_dump()
        | {
            "sentiment": [
                {"turn_id": "t4", "value": 3.0},  # clamped to 1.0
                {"turn_id": "t2", "value": -0.5},
                {"turn_id": "t2", "value": 0.9},  # duplicate: first wins
                {"turn_id": "t3", "value": 0.1},  # agent turn: dropped
                {"turn_id": "ghost", "value": 0.1},  # unknown: dropped
            ],
            "flags": [
                {"type": "hallucination_risk", "turn_id": "ghost", "detail": "x" * 2000},
                {"type": "tool_failure", "turn_id": None, "detail": "provider's opinion"},
            ],
        }
    )
    _, flags, sentiment = validate_draft(messy, make_record())

    assert [(p.turn_id, p.value) for p in sentiment] == [("t2", -0.5), ("t4", 1.0)]
    assert len(flags) == 1  # tool_failure is code's job, not the provider's
    assert flags[0].turn_id is None
    assert len(flags[0].detail) <= 500


async def test_analysis_adds_metrics_deterministic_flags_and_overall_score(rubric: Rubric) -> None:
    record = make_record(
        tool_calls=[
            {
                "id": "c1",
                "name": "check_restaurant_availability",
                "arguments": "{}",
                "output": "The reservation system isn't responding right now.",
                "is_error": True,
            }
        ]
    )
    analyzer = CallAnalyzer(FixedProvider(draft()), rubric)
    analysis = await analyzer.analyze(record)

    assert analysis.summary == "The caller asked about Nopa."
    assert analysis.metrics is not None and analysis.metrics.tool_errors == 1
    assert [f.type for f in analysis.flags] == ["tool_failure"]
    # All dimensions 4, but frustration is inverted (4 = quite frustrated), so not 75.
    assert analysis.overall_score == 73  # 72.5, rounded half-up
    assert analysis.analyzer is not None
    assert (analysis.analyzer.provider, analysis.analyzer.model) == ("openai", "fake-model")
