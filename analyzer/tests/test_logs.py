"""Transcript text must never reach the logs, even when something fails on it.

Every test plants a sentinel in the data (built at runtime, so it never appears in a source line
a traceback could print) and asserts that the service's formatted log output doesn't contain it.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from call_analyzer.api import create_app
from call_analyzer.logs import KeyValueFormatter
from call_analyzer.models import CallRecord
from call_analyzer.providers.heuristic import HeuristicProvider
from call_analyzer.rubric import Rubric
from call_analyzer.storage import SQLiteCallRepository
from tests.factories import make_record
from tests.test_api import AUTH, settings_for
from tests.test_worker import ScriptedProvider, status_of, worker_for

SENTINEL = "".join(["caller", "-secret-", "4111"])


@pytest.fixture
def service_log() -> Iterator[io.StringIO]:
    """Everything logged anywhere, formatted exactly as `serve` formats it."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(KeyValueFormatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def _validation_error() -> Exception:
    try:
        CallRecord.model_validate({"turns": [{"id": "t1", "role": "user", "text": SENTINEL}]})
    except Exception as exc:
        return exc
    raise AssertionError("expected a validation error")


def test_library_tracebacks_are_redacted(service_log: io.StringIO) -> None:
    # What uvicorn does for an exception that escapes the app: logger.exception(...).
    try:
        raise RuntimeError(f"wrapped: {SENTINEL}") from _validation_error()
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    text = service_log.getvalue()
    assert SENTINEL not in text
    # Still useful: types, the failing field locations, and the stack.
    assert "ValidationError [" in text and "call_id: Field required" in text
    assert "RuntimeError (message withheld" in text and "Traceback" in text


async def test_worker_failures_do_not_log_transcripts(
    tmp_path: Path, rubric: Rubric, service_log: io.StringIO
) -> None:
    repo = SQLiteCallRepository(tmp_path / "calls.db")
    try:
        # 1. A provider bug whose exception message quotes the transcript.
        await repo.insert_call(make_record(call_id="bug"))
        failing = ScriptedProvider(RuntimeError(f"cannot parse {SENTINEL}"))
        await worker_for(repo, failing, rubric).run_until_idle()

        # 2. A stored record that no longer validates (the error would quote the input).
        await repo.insert_call(make_record(call_id="stale"))
        bad_json = json.dumps({"turns": [{"text": SENTINEL}]})
        await repo._run(
            lambda conn: conn.execute(
                "UPDATE calls SET record_json = ? WHERE call_id = 'stale'", (bad_json,)
            )
        )
        await worker_for(repo, HeuristicProvider(), rubric).run_until_idle()

        assert (await status_of(repo, "bug")).status == "failed"
        assert (await status_of(repo, "stale")).status == "failed"
    finally:
        await repo.close()
    text = service_log.getvalue()
    assert "unexpected error analyzing call" in text
    assert SENTINEL not in text


def test_unhandled_api_errors_do_not_log_input_or_reach_the_server(
    tmp_path: Path, service_log: io.StringIO
) -> None:
    app = create_app(settings_for(tmp_path))

    async def explode(*args: Any) -> None:
        raise _validation_error()

    # raise_server_exceptions=True (default): the test fails if the exception escaped the app,
    # i.e. if uvicorn would have had to log it.
    with TestClient(app) as test_client:
        app.state.repo.list_calls = explode
        response = test_client.get("/v1/calls", headers=AUTH)
    assert response.status_code == 500
    text = service_log.getvalue()
    assert "unhandled error" in text and "ValidationError" in text
    assert SENTINEL not in text and SENTINEL not in response.text
