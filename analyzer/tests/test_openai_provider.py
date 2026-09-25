"""The OpenAI provider against a mocked HTTP transport (the real SDK, no network).

The SDK (openai>=3) is built on ``httpx2``, so the mock transport comes from there.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import openai
import pytest
from pydantic import SecretStr

from call_analyzer.analysis import CallAnalyzer
from call_analyzer.config import ConfigurationError, Settings
from call_analyzer.metrics import compute_metrics
from call_analyzer.models import DIMENSIONS
from call_analyzer.providers import build_provider
from call_analyzer.providers.base import ProviderError
from call_analyzer.providers.heuristic import HeuristicProvider
from call_analyzer.providers.openai_provider import OpenAIProvider
from call_analyzer.rubric import Rubric
from tests.factories import make_record

Handler = Callable[[httpx2.Request], httpx2.Response]


def assessment_json(**overrides: Any) -> str:
    evidence = [{"turn_id": "t3", "quote": "Seven on Friday is open for two."}]
    data: dict[str, Any] = {
        "summary": "The caller checked Nopa for Friday; seven was open.",
        "caller_intent": "Table for 2 at Nopa on Friday",
        "outcome": {"status": "resolved", "reason": "The requested time was open."},
        "scores": {
            dim: {
                "score": 5 if dim != "customer_frustration" else 1,
                "rationale": "ok",
                "evidence": evidence,
            }
            for dim in DIMENSIONS
        },
        "flags": [],
        "sentiment": [{"turn_id": "t2", "value": 0.2}, {"turn_id": "t4", "value": 0.9}],
    }
    data.update(overrides)
    return json.dumps(data)


def completion(content: str | None, *, finish_reason: str = "stop", refusal: str | None = None):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_790_000_000,
        "model": "gpt-5.4-mini",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300},
    }


def provider_with(handler: Handler, *, max_retries: int = 0, **kwargs: Any) -> OpenAIProvider:
    client = openai.AsyncOpenAI(
        api_key="sk-test",
        base_url="https://llm.test/v1",
        max_retries=max_retries,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return OpenAIProvider(
        client, model="gpt-5.4-mini", max_prompt_chars=20_000, max_output_tokens=4000, **kwargs
    )


async def assess(provider: OpenAIProvider, rubric: Rubric):
    record = make_record()
    return await provider.assess(record, compute_metrics(record), rubric)


async def test_sends_a_strict_schema_request_and_parses_the_answer(rubric: Rubric) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-test"
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=completion(assessment_json()))

    provider = provider_with(handler, reasoning_effort="low")
    analysis = await CallAnalyzer(provider, rubric).analyze(make_record())

    body = seen[0]
    assert body["model"] == "gpt-5.4-mini"
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] == 4000
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    system, user = body["messages"]
    assert system["role"] == "system" and "Security" in system["content"]
    assert "Is Nopa open Friday at seven for two?" in user["content"]

    assert analysis.overall_score == 100
    assert analysis.analyzer is not None and analysis.analyzer.model == "gpt-5.4-mini"
    assert analysis.scores["resolution"].evidence[0].quote == "Seven on Friday is open for two"


async def test_reasoning_effort_is_omitted_when_unset(rubric: Rubric) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=completion(assessment_json()))

    await assess(provider_with(handler, reasoning_effort=None), rubric)
    assert "reasoning_effort" not in bodies[0]


async def test_max_tokens_param_is_configurable(rubric: Rubric) -> None:
    # For OpenAI-compatible servers that only know the legacy `max_tokens` name.
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=completion(assessment_json()))

    await assess(provider_with(handler, max_tokens_param="max_tokens"), rubric)
    assert bodies[0]["max_tokens"] == 4000
    assert "max_completion_tokens" not in bodies[0]


async def test_rate_limit_is_retried_by_the_sdk_then_succeeds(rubric: Rubric) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            # retry-after-ms keeps the SDK's backoff to a millisecond in tests.
            return httpx2.Response(
                429, headers={"retry-after-ms": "1"}, json={"error": {"message": "slow down"}}
            )
        return httpx2.Response(200, json=completion(assessment_json()))

    draft = await assess(provider_with(handler, max_retries=2), rubric)
    assert calls == 2
    assert draft.outcome.status == "resolved"


async def test_persistent_rate_limit_is_a_retryable_error_with_retry_after(rubric: Rubric) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            429, headers={"retry-after": "7"}, json={"error": {"message": "rate limited"}}
        )

    with pytest.raises(ProviderError) as info:
        await assess(provider_with(handler), rubric)
    assert info.value.retryable
    assert info.value.retry_after_s == 7
    assert "HTTP 429" in str(info.value)


@pytest.mark.parametrize(
    ("status", "retryable"), [(500, True), (503, True), (401, False), (400, False), (404, False)]
)
async def test_http_errors_are_classified(rubric: Rubric, status: int, retryable: bool) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(ProviderError) as info:
        await assess(provider_with(handler), rubric)
    assert info.value.retryable is retryable
    assert "sk-test" not in str(info.value)


async def test_connection_errors_are_retryable(rubric: Rubric) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom", request=request)

    with pytest.raises(ProviderError, match="could not connect") as info:
        await assess(provider_with(handler), rubric)
    assert info.value.retryable


@pytest.mark.parametrize(
    "content",
    [
        "this is not json",
        json.dumps({"summary": "missing everything else"}),
        assessment_json(outcome={"status": "great", "reason": "not an allowed status"}),
    ],
)
async def test_malformed_output_is_a_retryable_error(rubric: Rubric, content: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=completion(content))

    with pytest.raises(ProviderError, match="doesn't match the schema") as info:
        await assess(provider_with(handler), rubric)
    assert info.value.retryable


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, text="<html>login page</html>", headers={"content-type": "text/html"}),
        httpx2.Response(200, json={"not": "a completion"}),
        httpx2.Response(200, text="{broken", headers={"content-type": "application/json"}),
    ],
)
async def test_non_completion_responses_point_at_the_base_url(
    rubric: Rubric, response: httpx2.Response
) -> None:
    # The SDK raises raw AttributeError/TypeError/JSONDecodeError for these; without mapping,
    # the worker could only store a generic "internal error".
    with pytest.raises(ProviderError, match="ANALYZER_BASE_URL") as info:
        await assess(provider_with(lambda request: response), rubric)
    assert not info.value.retryable


async def test_truncated_output_is_not_retried(rubric: Rubric) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=completion('{"summary": "cut', finish_reason="length"))

    with pytest.raises(ProviderError, match="ANALYZER_MAX_OUTPUT_TOKENS") as info:
        await assess(provider_with(handler), rubric)
    assert not info.value.retryable


async def test_refusal_is_not_retried(rubric: Rubric) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=completion(None, refusal="I can't help with that."))

    with pytest.raises(ProviderError, match="refused") as info:
        await assess(provider_with(handler), rubric)
    assert not info.value.retryable


async def test_out_of_range_scores_from_the_llm_are_dropped(rubric: Rubric) -> None:
    scores = json.loads(assessment_json())["scores"]
    scores["efficiency"]["score"] = 11
    scores["resolution"]["evidence"] = [{"turn_id": "t3", "quote": "Your table is booked!"}]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=completion(assessment_json(scores=scores)))

    analysis = await CallAnalyzer(provider_with(handler), rubric).analyze(make_record())
    assert "efficiency" not in analysis.scores
    assert analysis.scores["resolution"].evidence == []  # fabricated quote dropped
    assert analysis.overall_score == 100


def test_factory_picks_provider_from_settings() -> None:
    assert isinstance(build_provider(Settings(provider="heuristic")), HeuristicProvider)
    assert isinstance(build_provider(Settings(provider="auto", api_key=None)), HeuristicProvider)
    provider = build_provider(
        Settings(provider="auto", api_key=SecretStr("sk-x"), model="gpt-5.4-nano")
    )
    assert isinstance(provider, OpenAIProvider) and provider.model == "gpt-5.4-nano"
    with pytest.raises(ConfigurationError, match="ANALYZER_API_KEY"):
        build_provider(Settings(provider="openai", api_key=None))
