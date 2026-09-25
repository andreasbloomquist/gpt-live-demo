from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from call_analyzer.models import MAX_TURN_CHARS, MAX_TURNS, CallRecord
from tests.factories import demo_files, make_record, record_dict


def test_demo_records_are_valid() -> None:
    files = demo_files()
    assert len(files) == 6
    ids = {CallRecord.model_validate_json(f.read_bytes()).call_id for f in files}
    assert len(ids) == len(files)


def test_timestamps_are_normalized_to_utc() -> None:
    record = make_record(
        started_at="2026-09-23T18:00:00-07:00", ended_at="2026-09-23T18:01:00-07:00"
    )
    assert record.started_at == dt.datetime(2026, 9, 24, 1, 0, tzinfo=dt.timezone.utc)
    assert record.started_at.utcoffset() == dt.timedelta(0)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        make_record(started_at="2026-09-24T01:00:00")


def test_turn_limits() -> None:
    too_long = record_dict()
    too_long["turns"][0]["text"] = "x" * (MAX_TURN_CHARS + 1)
    with pytest.raises(ValidationError, match="at most 20000 characters"):
        CallRecord.model_validate(too_long)

    turn = {"id": "t", "role": "user", "text": "hi"}
    too_many = record_dict(turns=[{**turn, "id": f"t{i}"} for i in range(MAX_TURNS + 1)])
    with pytest.raises(ValidationError, match="at most 2000 items"):
        CallRecord.model_validate(too_many)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"call_id": "../../etc/passwd"}, "call_id"),
        ({"call_id": ""}, "call_id"),
        ({"duration_s": -1}, "duration_s"),
        ({"ended_at": "2026-09-24T00:59:00Z"}, "ended_at must not be before started_at"),
        ({"unexpected": True}, "Extra inputs are not permitted"),
    ],
)
def test_invalid_records_are_rejected(overrides: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_record(**overrides)


def test_duplicate_turn_ids_are_rejected() -> None:
    data = record_dict()
    data["turns"][1]["id"] = data["turns"][0]["id"]
    with pytest.raises(ValidationError, match="turn ids must be unique"):
        CallRecord.model_validate(data)


def test_confidence_must_be_a_probability() -> None:
    data = record_dict()
    data["turns"][1]["transcript_confidence"] = 1.5
    with pytest.raises(ValidationError, match="transcript_confidence"):
        CallRecord.model_validate(data)


def test_content_hash_ignores_key_order_and_timestamp_spelling() -> None:
    a = make_record(usage=[{"model": "m", "tokens": 1}])
    b = make_record(
        usage=[{"tokens": 1, "model": "m"}],
        started_at="2026-09-23T18:00:00-07:00",  # same instant as the base record
    )
    assert a.content_hash() == b.content_hash()
    assert a.content_hash() != make_record(end_reason="other").content_hash()
