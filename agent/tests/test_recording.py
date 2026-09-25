"""Call recording: CallRecord building, export to the analyzer, and the on-disk fallback."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from helpers import make_settings
from livekit.agents import llm

from voice_agent import main, recording
from voice_agent.prompts import PromptBundle, PromptComposer
from voice_agent.recording import (
    MAX_TURN_CHARS,
    TRUNCATION_MARKER,
    CallRecordExporter,
    build_call_record,
    new_call_id,
)

T0 = dt.datetime(2026, 9, 25, 19, 0, 0, tzinfo=dt.timezone.utc)
EPOCH0 = T0.timestamp()
ANALYZER = "http://analyzer.test"
TOKEN = "test-token"


@pytest.fixture(scope="module")
def bundle() -> PromptBundle:
    return PromptComposer().compose("concierge")


def synthetic_history() -> list[llm.ChatItem]:
    """A realistic concierge call, including items the record must skip."""
    return [
        llm.ChatMessage(
            id="sys", role="system", content=["You are a concierge."], created_at=EPOCH0
        ),
        llm.ChatMessage(
            id="a1",
            role="assistant",
            content=['<expr emotion="warm"/>Hi, this is the concierge. How can I help?'],
            created_at=EPOCH0 + 0.5,
            metrics={"started_speaking_at": EPOCH0 + 1.0, "stopped_speaking_at": EPOCH0 + 3.5},
        ),
        llm.ChatMessage(
            id="u1",
            role="user",
            content=["A table for four at Nopa on Friday at seven, please."],
            transcript_confidence=0.93,
            created_at=EPOCH0 + 6.0,
            metrics={"started_speaking_at": EPOCH0 + 4.0, "stopped_speaking_at": EPOCH0 + 6.0},
        ),
        llm.FunctionCall(
            call_id="call_avail",
            name="check_restaurant_availability",
            arguments='{"restaurant":"Nopa","party_size":4,"date":"2026-09-25","time":"19:00"}',
            created_at=EPOCH0 + 7.0,
        ),
        llm.FunctionCallOutput(
            call_id="call_avail",
            name="check_restaurant_availability",
            output="The reservation service is temporarily unavailable.",
            is_error=True,
            created_at=EPOCH0 + 8.0,
        ),
        llm.ChatMessage(
            id="a2",
            role="assistant",
            content=["I couldn't reach the booking system just now. Shall I try"],
            interrupted=True,
            created_at=EPOCH0 + 9.0,
        ),
        # The model's own transcription can come back empty (noise, a cough): not a turn.
        llm.ChatMessage(id="u-empty", role="user", content=["   "], created_at=EPOCH0 + 10.0),
        llm.ChatMessage(
            id="dev", role="developer", content=["Be brief."], created_at=EPOCH0 + 10.5
        ),
        llm.AgentHandoff(new_agent_id="concierge", created_at=EPOCH0 + 10.6),
        llm.ChatMessage(
            id="u2",
            role="user",
            content=["Yes, try again. Also, is it open on Mondays?"],
            transcript_confidence=1.7,  # out of range: recorded as unknown, not rejected
            created_at=EPOCH0 + 12.0,
        ),
        llm.FunctionCall(
            call_id="call_web",
            name="web_search",
            arguments='{"query":"Nopa San Francisco Monday hours"}',
            created_at=EPOCH0 + 13.0,
        ),
        llm.FunctionCallOutput(
            call_id="call_web",
            output="Open daily from 5 pm.",
            is_error=False,
            created_at=EPOCH0 + 14,
        ),
        # An output whose call isn't in the history can't be described, so it's dropped.
        llm.FunctionCallOutput(
            call_id="orphan", output="?", is_error=False, created_at=EPOCH0 + 14
        ),
        llm.ChatMessage(
            id="a3",
            role="assistant",
            content=["It's open every day from 5 pm."],
            created_at=EPOCH0 + 15,
        ),
    ]


def make_record(bundle: PromptBundle, items: list[llm.ChatItem] | None = None) -> dict[str, Any]:
    return build_call_record(
        synthetic_history() if items is None else items,
        call_id="room-abc-0123456789ab",
        room="room-abc",
        agent_name="gpt-live-agent",
        started_at=T0,
        ended_at=T0 + dt.timedelta(seconds=42.5),
        end_reason="participant_disconnected",
        bundle=bundle,
        settings=make_settings(),
        usage=[{"type": "llm_usage", "provider": "openai", "model": "gpt-live-1"}],
    )


# --- build_call_record ---------------------------------------------------------------------


def test_record_matches_call_record_v1(bundle: PromptBundle) -> None:
    record = make_record(bundle)

    assert record["schema_version"] == 1
    assert record["call_id"] == "room-abc-0123456789ab"
    assert record["started_at"] == "2026-09-25T19:00:00Z"
    assert record["ended_at"] == "2026-09-25T19:00:42.500000Z"
    assert record["duration_s"] == 42.5
    assert record["end_reason"] == "participant_disconnected"
    assert record["prompt"] == {
        "profile": "concierge",
        "fingerprint": bundle.fingerprint,
        "version": bundle.version,
        "voice": bundle.fingerprint_for("voice")[:12],
        "backend": bundle.fingerprint_for("backend")[:12],
    }
    assert record["models"] == {
        "voice_model": "gpt-live-1",
        "voice": "marin",
        "backend_model": "gpt-5.6-luna",
    }
    assert record["usage"] == [{"type": "llm_usage", "provider": "openai", "model": "gpt-live-1"}]
    json.dumps(record)  # plain JSON, no datetimes or pydantic objects


def test_turns_skip_instructions_and_empty_messages(bundle: PromptBundle) -> None:
    turns = make_record(bundle)["turns"]

    assert [(t["id"], t["role"]) for t in turns] == [
        ("a1", "assistant"),
        ("u1", "user"),
        ("a2", "assistant"),
        ("u2", "user"),
        ("a3", "assistant"),
    ]
    greeting, request, interrupted, followup, _ = turns
    assert greeting["text"] == "Hi, this is the concierge. How can I help?"  # <expr/> stripped
    # Speaking times come from the message metrics when present...
    assert greeting["started_at"] == "2026-09-25T19:00:01Z"
    assert greeting["ended_at"] == "2026-09-25T19:00:03.500000Z"
    # ...and fall back to the creation time (with no end) when not.
    assert interrupted["started_at"] == "2026-09-25T19:00:09Z"
    assert interrupted["ended_at"] is None
    assert interrupted["interrupted"] is True
    assert request["interrupted"] is False
    assert request["transcript_confidence"] == 0.93
    assert followup["transcript_confidence"] is None
    assert greeting["transcript_confidence"] is None


def test_tool_calls_are_paired_with_outputs(bundle: PromptBundle) -> None:
    calls = make_record(bundle)["tool_calls"]

    assert calls == [
        {
            "id": "call_avail",
            "name": "check_restaurant_availability",
            "arguments": '{"restaurant":"Nopa","party_size":4,"date":"2026-09-25","time":"19:00"}',
            "output": "The reservation service is temporarily unavailable.",
            "is_error": True,
            "created_at": "2026-09-25T19:00:07Z",
        },
        {
            "id": "call_web",
            "name": "web_search",
            "arguments": '{"query":"Nopa San Francisco Monday hours"}',
            "output": "Open daily from 5 pm.",
            "is_error": False,
            "created_at": "2026-09-25T19:00:13Z",
        },
    ]


def test_tool_call_without_output_is_recorded_as_pending(bundle: PromptBundle) -> None:
    items: list[llm.ChatItem] = [
        llm.FunctionCall(call_id="c1", name="web_search", arguments="{}", created_at=EPOCH0)
    ]
    (call,) = make_record(bundle, items)["tool_calls"]
    assert call["output"] is None
    assert call["is_error"] is False


def test_repeated_message_id_yields_one_turn(bundle: PromptBundle) -> None:
    # The analyzer rejects a record with duplicate turn ids, so a repeat must collapse.
    items: list[llm.ChatItem] = [
        llm.ChatMessage(id="u1", role="user", content=["A table"], created_at=EPOCH0),
        llm.ChatMessage(id="a1", role="assistant", content=["Sure."], created_at=EPOCH0 + 1),
        llm.ChatMessage(id="u1", role="user", content=["A table for two"], created_at=EPOCH0 + 2),
    ]
    turns = make_record(bundle, items)["turns"]
    assert [(t["id"], t["text"]) for t in turns] == [("u1", "A table for two"), ("a1", "Sure.")]


def test_ids_and_duration_are_capped_to_the_analyzer_limits(bundle: PromptBundle) -> None:
    long_id = "x" * 300
    items: list[llm.ChatItem] = [
        llm.ChatMessage(id=long_id, role="user", content=["Hi"], created_at=EPOCH0),
        llm.FunctionCall(call_id=long_id, name="n" * 300, arguments="{}", created_at=EPOCH0),
        llm.FunctionCallOutput(call_id=long_id, output="ok", is_error=False, created_at=EPOCH0),
    ]
    record = build_call_record(
        items,
        call_id="c",
        room="r",
        agent_name="a",
        started_at=T0,
        ended_at=T0 + dt.timedelta(days=2),
        end_reason=None,
        bundle=bundle,
        settings=make_settings(),
    )
    ((turn,), (call,)) = record["turns"], record["tool_calls"]
    assert turn["id"] == call["id"] == "x" * recording.MAX_ID_CHARS
    assert len(call["name"]) == recording.MAX_ID_CHARS
    assert call["output"] == "ok"  # still paired with its output after capping
    assert record["duration_s"] == recording.MAX_CALL_DURATION_S


def test_long_turn_is_truncated_to_the_analyzer_limit(bundle: PromptBundle) -> None:
    items: list[llm.ChatItem] = [
        llm.ChatMessage(id="long", role="user", content=["word " * 10_000], created_at=EPOCH0)
    ]
    (turn,) = make_record(bundle, items)["turns"]
    assert len(turn["text"]) == MAX_TURN_CHARS
    assert turn["text"].endswith(TRUNCATION_MARKER)


def test_turn_count_is_capped_keeping_the_start(
    bundle: PromptBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "MAX_TURNS", 3)
    items: list[llm.ChatItem] = [
        llm.ChatMessage(id=f"m{i}", role="user", content=[f"turn {i}"], created_at=EPOCH0 + i)
        for i in range(5)
    ]
    turns = make_record(bundle, items)["turns"]
    assert [t["id"] for t in turns] == ["m0", "m1", "m2"]


def test_clock_step_never_yields_negative_duration(bundle: PromptBundle) -> None:
    record = build_call_record(
        [],
        call_id="c",
        room="r",
        agent_name="a",
        started_at=T0,
        ended_at=T0 - dt.timedelta(seconds=1),
        end_reason=None,
        bundle=bundle,
        settings=make_settings(),
    )
    assert record["duration_s"] == 0.0
    assert record["ended_at"] == record["started_at"]
    assert record["end_reason"] is None


@pytest.mark.parametrize("call_id", ["", "../escape", ".hidden", "a/b", "x" * 201, "a b"])
def test_unsafe_call_id_is_rejected(bundle: PromptBundle, call_id: str) -> None:
    with pytest.raises(ValueError, match="call_id"):
        build_call_record(
            [],
            call_id=call_id,
            room="r",
            agent_name="a",
            started_at=T0,
            ended_at=T0,
            end_reason=None,
            bundle=bundle,
            settings=make_settings(),
        )


def test_naive_datetimes_are_rejected(bundle: PromptBundle) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_call_record(
            [],
            call_id="c",
            room="r",
            agent_name="a",
            started_at=dt.datetime(2026, 9, 25, 19, 0),
            ended_at=dt.datetime(2026, 9, 25, 19, 1),
            end_reason=None,
            bundle=bundle,
            settings=make_settings(),
        )


@pytest.mark.parametrize(
    ("room", "prefix"),
    [
        ("playground-3f9c", "playground-3f9c-"),
        ("../../etc/passwd", "etc-passwd-"),
        ("sip:+1 (415) 555-0100", "sip-1-415-555-0100-"),
        ("", "call-"),
        ("...", "call-"),
    ],
)
def test_new_call_id_is_safe_and_unique(room: str, prefix: str) -> None:
    first, second = new_call_id(room), new_call_id(room)
    assert first.startswith(prefix)
    assert first != second
    assert recording.CALL_ID_PATTERN.fullmatch(first)


# --- CallRecordExporter ------------------------------------------------------------------


def exporter(
    tmp_path: Path, handler: Any = None, *, url: str | None = ANALYZER, **kwargs: Any
) -> CallRecordExporter:
    return CallRecordExporter(
        tmp_path / "records",
        analyzer_url=url,
        token=TOKEN if url else None,
        retry_delay_s=0,
        transport=httpx.MockTransport(handler) if handler else None,
        **kwargs,
    )


async def test_export_posts_to_analyzer(tmp_path: Path, bundle: PromptBundle) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"call_id": "room-abc-0123456789ab", "status": "pending"})

    record = make_record(bundle)
    result = await exporter(tmp_path, handler).export(record)

    assert result.destination == "analyzer"
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{ANALYZER}/v1/calls"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == record
    assert not (tmp_path / "records").exists()


async def test_5xx_is_retried_once_then_saved_to_disk(tmp_path: Path, bundle: PromptBundle) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    record = make_record(bundle)
    result = await exporter(tmp_path, handler).export(record)

    assert attempts == 2
    assert result.destination == "file"
    assert result.detail == "analyzer returned HTTP 503"
    assert result.path == tmp_path / "records" / "room-abc-0123456789ab.json"
    assert json.loads(result.path.read_text(encoding="utf-8")) == record


async def test_connection_error_is_retried(tmp_path: Path, bundle: PromptBundle) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(202)

    result = await exporter(tmp_path, handler).export(make_record(bundle))
    assert (attempts, result.destination) == (2, "analyzer")


async def test_undecodable_response_falls_back_to_disk(
    tmp_path: Path, bundle: PromptBundle
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("bad gzip", request=request)

    result = await exporter(tmp_path, handler).export(make_record(bundle))
    assert result.destination == "file"  # not "failed": the transcript is still kept
    assert result.detail == "DecodingError"


async def test_429_is_retried_after_retry_after(tmp_path: Path, bundle: PromptBundle) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(202)

    result = await exporter(tmp_path, handler).export(make_record(bundle))
    assert (attempts, result.destination) == (2, "analyzer")


async def test_429_beyond_the_budget_goes_straight_to_disk(
    tmp_path: Path, bundle: PromptBundle
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    slow = exporter(tmp_path, handler, total_timeout_s=5)
    result = await asyncio.wait_for(slow.export(make_record(bundle)), timeout=1)
    assert attempts == 1  # didn't sleep 120 s, or wait out the 5 s budget
    assert (result.destination, result.detail) == ("file", "analyzer returned HTTP 429")


async def test_lone_surrogate_does_not_lose_the_record(
    tmp_path: Path, bundle: PromptBundle
) -> None:
    items: list[llm.ChatItem] = [
        llm.ChatMessage(id="u1", role="user", content=["caf\ud800e"], created_at=EPOCH0)
    ]
    result = await exporter(tmp_path, url=None).export(make_record(bundle, items))
    assert result.destination == "file" and result.path is not None
    saved = json.loads(result.path.read_text(encoding="utf-8"))
    assert saved["turns"][0]["text"] == "caf?e"


async def test_4xx_is_not_retried_but_record_is_kept(tmp_path: Path, bundle: PromptBundle) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401)

    result = await exporter(tmp_path, handler).export(make_record(bundle))
    assert attempts == 1
    assert result.destination == "file"
    assert result.detail == "analyzer returned HTTP 401"


async def test_slow_analyzer_is_bounded_by_total_timeout(
    tmp_path: Path, bundle: PromptBundle
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(202)

    slow = exporter(tmp_path, handler, total_timeout_s=0.05)
    result = await asyncio.wait_for(slow.export(make_record(bundle)), timeout=2)
    assert result.destination == "file"
    assert result.detail is not None and "did not answer" in result.detail


async def test_without_analyzer_url_record_goes_to_disk(
    tmp_path: Path, bundle: PromptBundle
) -> None:
    result = await exporter(tmp_path, url=None).export(make_record(bundle))
    assert result.destination == "file"
    assert result.path is not None and result.path.is_file()
    # Only the finished file remains: the temp file was renamed into place.
    assert [p.name for p in (tmp_path / "records").iterdir()] == ["room-abc-0123456789ab.json"]


async def test_oversized_record_skips_the_analyzer(
    tmp_path: Path, bundle: PromptBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "MAX_BODY_BYTES", 100)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an over-limit record must not be posted")

    result = await exporter(tmp_path, handler).export(make_record(bundle))
    assert result.destination == "file"


@pytest.mark.parametrize("call_id", ["../../outside", "", None])
async def test_unsafe_call_id_never_touches_disk(tmp_path: Path, call_id: str | None) -> None:
    result = await exporter(tmp_path, url=None).export({"call_id": call_id, "turns": []})
    assert result.destination == "failed"
    assert not (tmp_path / "records").exists()
    assert not (tmp_path / "outside.json").exists()


async def test_failed_write_leaves_no_partial_file(
    tmp_path: Path, bundle: PromptBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "records" / "room-abc-0123456789ab.json"
    target.parent.mkdir()
    target.write_text("previous", encoding="utf-8")

    def broken_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(recording.os, "replace", broken_replace)
    result = await exporter(tmp_path, url=None).export(make_record(bundle))

    assert result.destination == "failed"
    assert target.read_text(encoding="utf-8") == "previous"
    assert [p.name for p in target.parent.iterdir()] == [target.name]


async def test_export_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(recording.json, "dumps", boom)
    result = await exporter(tmp_path, url=None).export({"call_id": "ok", "turns": []})
    assert result.destination == "failed"


async def test_export_logs_no_transcript_text(
    tmp_path: Path, bundle: PromptBundle, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="voice_agent")
    await exporter(tmp_path, url=None).export(make_record(bundle))
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "room-abc-0123456789ab" in logged
    assert "Nopa" not in logged


# --- main.py glue ------------------------------------------------------------------------


async def test_session_shutdown_writes_record(tmp_path: Path, bundle: PromptBundle) -> None:
    ctx = SimpleNamespace(
        job=SimpleNamespace(room=SimpleNamespace(name="demo-room"), agent_name="")
    )
    session = SimpleNamespace(
        history=llm.ChatContext(synthetic_history()),
        usage=SimpleNamespace(model_usage=[]),
    )
    await main._record_call(
        ctx,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        exporter(tmp_path, url=None),
        bundle=bundle,
        settings=make_settings(),
        started_at=T0,
        end_reason="participant_disconnected",
    )

    (path,) = (tmp_path / "records").iterdir()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == f"{record['call_id']}.json"
    assert record["call_id"].startswith("demo-room-")
    assert record["room"] == "demo-room"
    assert len(record["turns"]) == 5


async def test_session_without_turns_is_not_recorded(tmp_path: Path, bundle: PromptBundle) -> None:
    ctx = SimpleNamespace(job=SimpleNamespace(room=SimpleNamespace(name="r"), agent_name="a"))
    session = SimpleNamespace(history=llm.ChatContext(), usage=SimpleNamespace(model_usage=[]))
    await main._record_call(
        ctx,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        exporter(tmp_path, url=None),
        bundle=bundle,
        settings=make_settings(),
        started_at=T0,
        end_reason=None,
    )
    assert not (tmp_path / "records").exists()
