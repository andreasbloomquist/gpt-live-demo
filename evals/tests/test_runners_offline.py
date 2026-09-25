"""Exercise the real runner code paths offline.

The brain runner is driven by a scripted fake of the OpenAI client, but everything else is
real: prompt composition, tool registry, schema conversion and *actual* tool execution through
LiveKit's argument validation against the deterministic mock reservation provider. The voice
runner's audio plumbing is tested against LiveKit's real ``AudioInput``/``AudioOutput`` bases.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("voice_agent.model")

from evals.runners.assertions import check_expectations
from evals.runners.brain import BrainRunner
from evals.schema import load_suites

IN_TWO_DAYS = (dt.date.today() + dt.timedelta(days=2)).isoformat()  # safely in the future in any tz


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=1000, output_tokens=100)


class _ScriptedResponses:
    """Stands in for ``AsyncOpenAI().responses``: first a tool call, then a final answer."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            call = SimpleNamespace(
                type="function_call",
                name="check_restaurant_availability",
                call_id="call_1",
                arguments=json.dumps(
                    {"restaurant": "Nopa", "date": IN_TWO_DAYS, "time": "19:00", "party_size": 4}
                ),
            )
            search = SimpleNamespace(
                type="web_search_call", action=SimpleNamespace(query="nopa hours")
            )
            return SimpleNamespace(id="r1", output=[search, call], usage=_usage(), output_text="")
        return SimpleNamespace(
            id="r2", output=[], usage=_usage(), output_text="Seven works for four at Nopa."
        )


def test_brain_runner_tool_loop_executes_real_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    suite = load_suites(names=["restaurant_availability"])["restaurant_availability"]
    case = next(c for c in suite.cases if c.id == "explicit_request")
    responses = _ScriptedResponses()
    runner = BrainRunner(client=SimpleNamespace(responses=responses))

    async def go() -> Any:
        info = await runner.setup(suite)
        conv = await runner.converse(suite, case, 1)
        return info, conv

    info, conv = asyncio.run(go())
    # What GPT-Live would send its backend: composed instructions, options, provider tool dict
    first = responses.requests[0]
    assert first["model"] == info["backend_model"]
    # runtime variables are injected exactly like production (no placeholders left)
    assert first["instructions"] and "<runtime:" not in first["instructions"]
    assert {"type": "web_search"}.items() <= next(
        t for t in first["tools"] if t.get("type") == "web_search"
    ).items()
    # the second request chains the first and returns the real tool output
    second = responses.requests[1]
    assert second["previous_response_id"] == "r1"
    (output_item,) = second["input"]
    assert output_item["type"] == "function_call_output" and output_item["call_id"] == "call_1"
    tool_call = next(c for c in conv.transcript.tool_calls if c.name != "web_search")
    assert not tool_call.is_error, tool_call.output
    assert json.loads(tool_call.output)  # the mock provider's JSON
    assert conv.transcript.final_reply == "Seven works for four at Nopa."
    assert conv.cost_usd > 0


def test_brain_runner_surfaces_tool_errors_to_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    suite = load_suites(names=["restaurant_availability"])["restaurant_availability"]
    runner = BrainRunner(client=None)
    asyncio.run(runner.setup(suite))
    bad = json.dumps({"restaurant": "Nopa", "date": "2001-01-01", "time": "19:00", "party_size": 2})
    output, is_error = asyncio.run(runner._execute("check_restaurant_availability", bad))
    assert is_error and "past" in output
    output, is_error = asyncio.run(runner._execute("nope", "{}"))
    assert is_error and "Unknown function" in output


def test_voice_audio_plumbing_and_history_conversion() -> None:
    from livekit.agents import llm

    from evals.runners.voice import (
        FRAME_BYTES,
        SAMPLE_RATE,
        _transcript_from_history,
        build_microphone,
        build_speaker,
    )

    async def go() -> None:
        mic = build_microphone()
        mic.say(b"\x01\x00" * (SAMPLE_RATE // 10))  # 100 ms of "speech"
        frames = [await mic.__anext__() for _ in range(40)]
        assert all(
            f.sample_rate == SAMPLE_RATE and len(bytes(f.data)) == FRAME_BYTES for f in frames
        )
        await asyncio.wait_for(mic.wait_spoken(), 1)
        mic.close()
        with pytest.raises(StopAsyncIteration):
            await mic.__anext__()

        speaker = build_speaker()
        await speaker.capture_frame(frames[0])
        assert speaker.playing
        speaker.flush()
        ev = await asyncio.wait_for(speaker.wait_for_playout(), 1)
        assert not ev.interrupted and not speaker.playing

    asyncio.run(go())

    ctx = llm.ChatContext.empty()
    ctx.add_message(role="assistant", content="Hi, this is Ava.")
    ctx.add_message(role="user", content="Table for two tomorrow at Nopa?")
    ctx.insert(
        llm.FunctionCall(
            call_id="c1",
            name="check_restaurant_availability",
            arguments='{"restaurant": "Nopa", "party_size": 2}',
        )
    )
    ctx.insert(
        llm.FunctionCallOutput(
            call_id="c1",
            name="check_restaurant_availability",
            output='{"status": "available"}',
            is_error=False,
        )
    )
    ctx.add_message(role="assistant", content="Yes, seven is open.")
    transcript = _transcript_from_history(ctx, frozenset({"web_search"}))
    assert transcript.final_reply == "Yes, seven is open."
    assert transcript.tool_calls[0].arguments["party_size"] == 2
    assert transcript.tool_calls[0].output == '{"status": "available"}'

    suite = load_suites(names=["web_search"])["web_search"]
    case = next(c for c in suite.cases if c.id == "fresh_news_uses_search")
    checks = check_expectations(case.expect, transcript)
    assert any(c.skipped for c in checks)  # web_search is provider-side: skipped, not failed
