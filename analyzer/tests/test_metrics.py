from __future__ import annotations

from call_analyzer.metrics import compute_metrics, find_long_silences
from tests.factories import load_demo, make_record, record_dict


def test_metrics_on_untimed_record_use_word_counts() -> None:
    metrics = compute_metrics(make_record())
    assert metrics.turns == 4
    assert metrics.user_turns == 2
    assert metrics.agent_turns == 2
    assert metrics.tool_calls == 1
    assert metrics.tool_errors == 0
    assert metrics.interruptions == 0
    # Agent: 8 + 11 words; caller: 8 + 2 words.
    assert metrics.avg_agent_words_per_turn == 9.5
    assert metrics.talk_ratio_agent == round(19 / 29, 3)
    assert metrics.mean_transcript_confidence == 0.925
    assert metrics.low_confidence_turns == 0


def test_metrics_use_speaking_time_when_every_turn_is_timed() -> None:
    data = record_dict(
        turns=[
            {
                "id": "a",
                "role": "assistant",
                "text": "one two",
                "started_at": "2026-09-24T01:00:00Z",
                "ended_at": "2026-09-24T01:00:03Z",
            },
            {
                "id": "b",
                "role": "user",
                "text": "a much longer caller sentence here",
                "started_at": "2026-09-24T01:00:04Z",
                "ended_at": "2026-09-24T01:00:05Z",
                "transcript_confidence": 0.4,
            },
        ]
    )
    metrics = compute_metrics(make_record(**data))
    assert metrics.talk_ratio_agent == 0.75  # 3 s of 4 s, despite fewer words
    assert metrics.low_confidence_turns == 1


def test_metrics_on_empty_call() -> None:
    metrics = compute_metrics(make_record(turns=[], tool_calls=[]))
    assert metrics.turns == 0
    assert metrics.talk_ratio_agent == 0.0
    assert metrics.avg_agent_words_per_turn == 0.0
    assert metrics.mean_transcript_confidence is None


def test_demo_frustrated_call_metrics() -> None:
    metrics = compute_metrics(load_demo("03-frustrated-zuni.json"))
    assert metrics.interruptions == 2
    assert metrics.tool_calls == 2
    assert metrics.tool_errors == 1
    assert metrics.low_confidence_turns == 1


def test_long_silence_is_found_and_overlaps_are_not() -> None:
    record = load_demo("06-low-confidence-kokkari.json")
    silences = find_long_silences(record)
    assert len(silences) == 1
    assert silences[0].seconds > 10
    # Barge-in overlaps (negative gaps) in the frustrated call are not silences.
    assert find_long_silences(load_demo("03-frustrated-zuni.json")) == []
