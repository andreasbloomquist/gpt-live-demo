"""End-to-end HTTP tests: real app, real SQLite, heuristic provider, background worker running."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from call_analyzer.api import create_app
from call_analyzer.config import ConfigurationError, Settings
from call_analyzer.models import CallRecord
from tests.factories import demo_files, record_dict

TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "api_token": TOKEN,
        "db_path": tmp_path / "calls.db",
        "provider": "heuristic",
        "api_key": None,
        "poll_interval_s": 0.05,
        **overrides,
    }
    return Settings(**values)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(settings_for(tmp_path))) as test_client:
        yield test_client


def wait_until_analyzed(client: TestClient, call_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/v1/calls/{call_id}", headers=AUTH).json()
        if body["analysis"]["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"{call_id} was not analyzed within {timeout}s")


def test_healthz_needs_no_auth(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
    ],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/calls"),
        ("GET", "/v1/calls/x"),
        ("POST", "/v1/calls"),
        ("POST", "/v1/calls/x/analyze"),
    ],
)
def test_every_v1_route_requires_the_token(
    client: TestClient, headers: dict, method: str, path: str
) -> None:
    response = client.request(method, path, headers=headers, content=b"{}")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Missing or invalid bearer token."}
    }


def test_ingest_analyze_and_read_back(client: TestClient) -> None:
    record = json.loads(demo_files()[0].read_text())
    response = client.post("/v1/calls", headers=AUTH, json=record)
    assert response.status_code == 202
    assert response.json() == {"call_id": record["call_id"], "status": "pending"}

    body = wait_until_analyzed(client, record["call_id"])
    assert body["record"]["call_id"] == record["call_id"]
    assert body["record"]["turns"][0]["text"] == record["turns"][0]["text"]
    analysis = body["analysis"]
    assert analysis["status"] == "done"
    assert analysis["analyzer"] == {"provider": "heuristic", "model": None, "rubric_version": "1"}
    assert set(analysis["scores"]) == {
        "customer_satisfaction",
        "customer_frustration",
        "resolution",
        "agent_helpfulness",
        "accuracy_groundedness",
        "conversation_flow",
        "efficiency",
        "tone_empathy",
        "policy_adherence",
    }
    assert set(analysis["metrics"]) == {
        "duration_s",
        "turns",
        "user_turns",
        "agent_turns",
        "talk_ratio_agent",
        "interruptions",
        "tool_calls",
        "tool_errors",
        "avg_agent_words_per_turn",
        "mean_transcript_confidence",
        "low_confidence_turns",
    }
    assert analysis["created_at"].endswith("Z")


def test_repost_is_idempotent_and_conflicts_are_rejected(client: TestClient) -> None:
    record = record_dict()
    assert client.post("/v1/calls", headers=AUTH, json=record).status_code == 202
    wait_until_analyzed(client, record["call_id"])

    again = client.post("/v1/calls", headers=AUTH, json=record)
    assert again.status_code == 200
    assert again.json() == {"call_id": record["call_id"], "status": "done"}

    changed = client.post("/v1/calls", headers=AUTH, json={**record, "end_reason": "other"})
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "conflict"


def test_invalid_records_get_422_without_echoing_input(client: TestClient) -> None:
    record = record_dict()
    record["turns"][0]["text"] = "secret-caller-words " * 2000  # > 20k chars
    response = client.post("/v1/calls", headers=AUTH, json=record)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == ["turns", "0", "text"]
    assert "secret-caller-words" not in response.text

    assert client.post("/v1/calls", headers=AUTH, content=b"{not json").status_code == 422


def test_oversized_bodies_are_rejected_with_413(client: TestClient) -> None:
    huge = b"x" * (2 * 1024 * 1024 + 1)
    response = client.post("/v1/calls", headers=AUTH, content=huge)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"

    # Same without a Content-Length header (chunked upload): the stream is cut off.
    def chunks() -> Iterator[bytes]:
        for _ in range(3):
            yield b"x" * (1024 * 1024)

    response = client.post("/v1/calls", headers=AUTH, content=chunks())
    assert response.status_code == 413


def test_list_is_paginated_newest_first(client: TestClient) -> None:
    for path in demo_files():
        assert client.post("/v1/calls", headers=AUTH, content=path.read_bytes()).status_code == 202

    first = client.get("/v1/calls", params={"limit": 4}, headers=AUTH).json()
    second = client.get(
        "/v1/calls", params={"limit": 4, "cursor": first["next_cursor"]}, headers=AUTH
    ).json()
    items = first["items"] + second["items"]
    assert len(first["items"]) == 4 and len(second["items"]) == 2
    assert second["next_cursor"] is None
    starts = [item["started_at"] for item in items]
    assert starts == sorted(starts, reverse=True)
    assert set(items[0]) == {
        "call_id",
        "started_at",
        "duration_s",
        "turns",
        "status",
        "caller_intent",
        "summary",
        "outcome",
        "overall_score",
        "analyzer",
    }
    assert isinstance(items[0]["turns"], int)

    bad = client.get("/v1/calls", params={"cursor": "nope"}, headers=AUTH)
    assert bad.status_code == 400
    assert client.get("/v1/calls", params={"limit": 1000}, headers=AUTH).status_code == 422


def test_reanalyze(client: TestClient) -> None:
    record = record_dict()
    client.post("/v1/calls", headers=AUTH, json=record)
    first = wait_until_analyzed(client, record["call_id"])["analysis"]

    response = client.post(f"/v1/calls/{record['call_id']}/analyze", headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"call_id": record["call_id"], "status": "pending"}
    second = wait_until_analyzed(client, record["call_id"])["analysis"]
    assert second["created_at"] > first["created_at"]
    assert second["overall_score"] == first["overall_score"]

    missing = client.post("/v1/calls/nope/analyze", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_unknown_and_malformed_call_ids(client: TestClient) -> None:
    assert client.get("/v1/calls/does-not-exist", headers=AUTH).status_code == 404
    assert client.get("/v1/calls/bad%20id", headers=AUTH).status_code == 422


def test_unexpected_errors_return_clean_json(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))

    async def explode(*args: Any) -> None:
        raise RuntimeError("internal detail that must not leak")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        app.state.repo.list_calls = explode
        response = test_client.get("/v1/calls", headers=AUTH)
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Internal server error."}
    }
    assert "internal detail" not in response.text


@pytest.mark.parametrize("token", [None, "short"])
def test_startup_fails_fast_without_a_usable_token(tmp_path: Path, token: str | None) -> None:
    with pytest.raises(ConfigurationError, match="CALL_ANALYZER_TOKEN"):
        create_app(settings_for(tmp_path, api_token=token))


@pytest.mark.parametrize("enabled", [False, True])
def test_schema_docs_are_opt_in(tmp_path: Path, enabled: bool) -> None:
    app = create_app(settings_for(tmp_path, enable_docs=enabled))
    with TestClient(app) as test_client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert test_client.get(path).status_code == (200 if enabled else 404)


def test_stored_record_is_served_without_revalidation(client: TestClient) -> None:
    record = record_dict()
    client.post("/v1/calls", headers=AUTH, json=record)
    wait_until_analyzed(client, record["call_id"])
    body = client.get(f"/v1/calls/{record['call_id']}", headers=AUTH).json()
    # Same shape as before: the normalized record, as the CallRecord model serializes it.
    assert body["record"] == CallRecord.model_validate(record).model_dump(mode="json")
    assert body["analysis"]["status"] == "done"

    # A row that a (hypothetically) stricter model would reject still reads back fine.
    repo = client.app.state.repo  # type: ignore[attr-defined]
    legacy = json.dumps({**body["record"], "started_at": "0999-01-01T00:00:00Z"})

    async def rewrite() -> None:
        await repo._run(lambda conn: conn.execute("UPDATE calls SET record_json = ?", (legacy,)))

    client.portal.call(rewrite)  # type: ignore[union-attr]
    response = client.get(f"/v1/calls/{record['call_id']}", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["record"]["started_at"] == "0999-01-01T00:00:00Z"


async def test_client_disconnect_mid_body_is_not_a_server_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(settings_for(tmp_path))
    incoming = [
        {"type": "http.request", "body": b'{"schema_version": 1', "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return incoming.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/calls",
        "raw_path": b"/v1/calls",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    with caplog.at_level("INFO"):
        await app(scope, receive, send)  # must not raise
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 400
    assert "unhandled error" not in caplog.text
