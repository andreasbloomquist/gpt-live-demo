"""The heuristic provider on the demo calls: it should tell a good call from a bad one."""

from __future__ import annotations

import pytest

from call_analyzer.analysis import CallAnalyzer, normalize_for_match
from tests.factories import demo_files, load_demo, make_record, record_dict


@pytest.mark.parametrize("path", demo_files(), ids=lambda p: p.stem)
async def test_every_demo_call_gets_a_complete_labelled_analysis(
    path, heuristic_analyzer: CallAnalyzer
) -> None:
    record = load_demo(path.name)
    analysis = await heuristic_analyzer.analyze(record)

    assert analysis.status == "done"
    assert analysis.analyzer is not None
    assert (analysis.analyzer.provider, analysis.analyzer.model) == ("heuristic", None)
    assert analysis.analyzer.rubric_version == "1"
    assert len(analysis.scores) == 9  # the heuristic always answers every dimension
    assert all(s.rationale.startswith("Heuristic:") for s in analysis.scores.values())
    assert analysis.overall_score is not None and 0 <= analysis.overall_score <= 100
    # Every evidence quote survived validation, i.e. it's really in the cited turn.
    texts = {t.id: t.text for t in record.turns}
    for score in analysis.scores.values():
        for ev in score.evidence:
            assert normalize_for_match(ev.quote) in normalize_for_match(texts[ev.turn_id])
    # One sentiment point per caller turn.
    assert [p.turn_id for p in analysis.sentiment] == [
        t.id for t in record.turns if t.role == "user"
    ]


async def test_is_deterministic(heuristic_analyzer: CallAnalyzer) -> None:
    record = load_demo("03-frustrated-zuni.json")
    first = await heuristic_analyzer.analyze(record)
    second = await heuristic_analyzer.analyze(record)
    assert first.model_dump(exclude={"created_at"}) == second.model_dump(exclude={"created_at"})


async def test_ranks_happy_call_above_frustrated_call(heuristic_analyzer: CallAnalyzer) -> None:
    happy = await heuristic_analyzer.analyze(load_demo("01-available-state-bird.json"))
    frustrated = await heuristic_analyzer.analyze(load_demo("03-frustrated-zuni.json"))

    assert happy.outcome is not None and happy.outcome.status == "resolved"
    assert happy.overall_score is not None and frustrated.overall_score is not None
    assert happy.overall_score >= frustrated.overall_score + 20
    assert frustrated.scores["customer_frustration"].score >= 3
    assert frustrated.scores["customer_satisfaction"].score <= 2
    flag_types = {f.type for f in frustrated.flags}
    assert {"tool_failure", "caller_repeated"} <= flag_types
    assert min(p.value for p in frustrated.sentiment) < 0


async def test_large_party_is_partially_resolved(heuristic_analyzer: CallAnalyzer) -> None:
    analysis = await heuristic_analyzer.analyze(load_demo("05-large-party-foreign-cinema.json"))
    assert analysis.outcome is not None
    assert analysis.outcome.status == "partially_resolved"
    assert analysis.caller_intent == "Table for 12 at Foreign Cinema (Sat Oct 3, 7:00 PM)"


async def test_low_confidence_call_flags_long_silence(heuristic_analyzer: CallAnalyzer) -> None:
    analysis = await heuristic_analyzer.analyze(load_demo("06-low-confidence-kokkari.json"))
    assert analysis.metrics is not None and analysis.metrics.low_confidence_turns == 2
    assert [f.type for f in analysis.flags] == ["long_silence"]


async def test_booking_claim_is_a_policy_violation(heuristic_analyzer: CallAnalyzer) -> None:
    data = record_dict()
    data["turns"][2]["text"] = "Great news, I've booked a table for two at seven."
    analysis = await heuristic_analyzer.analyze(make_record(**data))

    assert analysis.scores["policy_adherence"].score == 1
    assert analysis.overall_score is not None and analysis.overall_score <= 40  # capped
    violation = next(f for f in analysis.flags if f.type == "policy_violation")
    assert violation.turn_id == "t3"


async def test_escalation_request_is_flagged(heuristic_analyzer: CallAnalyzer) -> None:
    data = record_dict()
    data["turns"][3]["text"] = "This is useless. Let me speak to a human."
    analysis = await heuristic_analyzer.analyze(make_record(**data))

    assert any(f.type == "escalation_needed" and f.turn_id == "t4" for f in analysis.flags)
    assert analysis.scores["customer_frustration"].score >= 3


async def test_silent_call_is_not_applicable(heuristic_analyzer: CallAnalyzer) -> None:
    record = make_record(
        turns=[{"id": "a", "role": "assistant", "text": "Hello? Are you there?"}], tool_calls=[]
    )
    analysis = await heuristic_analyzer.analyze(record)
    assert analysis.outcome is not None and analysis.outcome.status == "not_applicable"
    assert analysis.sentiment == []
