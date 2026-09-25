"""Test data builders (plain functions, importable from any test module)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from call_analyzer.models import CallRecord

DEMO_DIR = Path(__file__).resolve().parents[1] / "demo" / "calls"

_BASE_RECORD: dict[str, Any] = {
    "schema_version": 1,
    "call_id": "gpt-live-test0001-aaaa1111",
    "room": "gpt-live-test0001",
    "agent_name": "gpt-live-agent",
    "started_at": "2026-09-24T01:00:00Z",
    "ended_at": "2026-09-24T01:01:00Z",
    "duration_s": 60.0,
    "end_reason": "participant_disconnected",
    "prompt": {
        "profile": "concierge",
        "fingerprint": "ab" * 32,
        "version": "ab" * 6,
        "voice": "cd" * 6,
        "backend": "ef" * 6,
    },
    "models": {"voice_model": "gpt-live-1", "voice": "marin", "backend_model": "gpt-5.6-luna"},
    "turns": [
        {"id": "t1", "role": "assistant", "text": "Hi, this is Ava. How can I help?"},
        {
            "id": "t2",
            "role": "user",
            "text": "Is Nopa open Friday at seven for two?",
            "transcript_confidence": 0.95,
        },
        {
            "id": "t3",
            "role": "assistant",
            "text": "Seven on Friday is open for two. I haven't booked it.",
        },
        {"id": "t4", "role": "user", "text": "Perfect, thanks!", "transcript_confidence": 0.9},
    ],
    "tool_calls": [
        {
            "id": "c1",
            "name": "check_restaurant_availability",
            "arguments": '{"restaurant":"Nopa","date":"2026-09-25","time":"19:00","party_size":2}',
            "output": '{"status":"available","nearest_available_times":["19:00"]}',
            "is_error": False,
        }
    ],
    "usage": [],
}


def record_dict(**overrides: Any) -> dict[str, Any]:
    """A valid CallRecord as a plain dict, with top-level fields overridden."""
    data = copy.deepcopy(_BASE_RECORD)
    data.update(overrides)
    return data


def make_record(**overrides: Any) -> CallRecord:
    return CallRecord.model_validate(record_dict(**overrides))


def load_demo(name: str) -> CallRecord:
    return CallRecord.model_validate_json((DEMO_DIR / name).read_bytes())


def demo_files() -> list[Path]:
    return sorted(DEMO_DIR.glob("*.json"))
