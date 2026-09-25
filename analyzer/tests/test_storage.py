from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from call_analyzer.models import Analysis
from call_analyzer.storage import InvalidCursorError, SQLiteCallRepository, utc_now
from tests.factories import make_record


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[SQLiteCallRepository]:
    repository = SQLiteCallRepository(tmp_path / "nested" / "calls.db")
    yield repository
    await repository.close()


def _done(call_id: str) -> Analysis:
    return Analysis(call_id=call_id, status="done", created_at=utc_now(), summary="ok")


async def test_insert_is_idempotent_by_content(repo: SQLiteCallRepository) -> None:
    record = make_record()
    assert await repo.insert_call(record) == ("created", "pending")
    assert await repo.insert_call(make_record()) == ("duplicate", "pending")
    assert await repo.insert_call(make_record(end_reason="changed")) == ("conflict", "pending")

    stored = await repo.get_call(record.call_id)
    assert stored is not None
    assert stored.record_json == record.model_dump_json()
    assert stored.analysis.status == "pending"
    assert await repo.get_call("missing") is None


async def test_list_is_newest_first_with_stable_cursor(repo: SQLiteCallRepository) -> None:
    for i in range(5):
        await repo.insert_call(
            make_record(
                call_id=f"call-{i}",
                started_at=f"2026-09-2{i}T10:00:00Z",
                ended_at=f"2026-09-2{i}T10:01:00Z",
            )
        )
    # Two calls starting at the same instant are ordered by call_id, never skipped.
    await repo.insert_call(
        make_record(
            call_id="call-4b", started_at="2026-09-24T10:00:00Z", ended_at="2026-09-24T10:01:00Z"
        )
    )

    page1, cursor = await repo.list_calls(limit=4, cursor=None)
    assert [c.call_id for c in page1] == ["call-4b", "call-4", "call-3", "call-2"]
    assert cursor is not None
    # A call inserted between page requests doesn't shift the next page.
    await repo.insert_call(
        make_record(
            call_id="call-new", started_at="2026-09-30T10:00:00Z", ended_at="2026-09-30T10:01:00Z"
        )
    )
    page2, cursor2 = await repo.list_calls(limit=4, cursor=cursor)
    assert [c.call_id for c in page2] == ["call-1", "call-0"]
    assert cursor2 is None
    assert page2[0].turns == 4 and page2[0].duration_s == 60.0


@pytest.mark.parametrize("cursor", ["garbage", "WyJ4Il0", "", "eyJhIjoxfQ"])
async def test_invalid_cursor_is_rejected(repo: SQLiteCallRepository, cursor: str) -> None:
    with pytest.raises(InvalidCursorError):
        await repo.list_calls(limit=5, cursor=cursor)


async def test_job_lifecycle(repo: SQLiteCallRepository) -> None:
    await repo.insert_call(make_record(call_id="a"))
    job = await repo.claim_next()
    assert job is not None and (job.call_id, job.generation, job.attempts) == ("a", 1, 1)
    assert await repo.claim_next() is None  # already running

    assert await repo.retry_later(job, "rate limited", delay_s=3600)
    assert await repo.claim_next() is None  # not due yet
    stored = await repo.get_call("a")
    assert stored is not None
    assert (stored.analysis.status, stored.analysis.error) == ("pending", "rate limited")

    assert await repo.retry_later(job, "x", delay_s=0) is False  # job no longer running


async def test_reanalysis_supersedes_an_in_flight_run(repo: SQLiteCallRepository) -> None:
    await repo.insert_call(make_record(call_id="a"))
    stale = await repo.claim_next()
    assert stale is not None

    assert await repo.request_analysis("a")
    assert await repo.request_analysis("missing") is False
    # The stale run finishing late must not overwrite the new generation.
    assert await repo.complete(stale, _done("a")) is False

    fresh = await repo.claim_next()
    assert fresh is not None and fresh.generation == stale.generation + 1
    assert fresh.attempts == 1
    assert await repo.complete(fresh, _done("a"))
    stored = await repo.get_call("a")
    assert stored is not None
    analysis = stored.analysis.to_analysis()
    assert (analysis.status, analysis.summary) == ("done", "ok")


async def test_recover_interrupted_requeues_until_attempts_run_out(
    repo: SQLiteCallRepository,
) -> None:
    await repo.insert_call(make_record(call_id="a"))
    # The process "dies" mid-job three times in a row (e.g. this call triggers a crash).
    for attempt in (1, 2, 3):
        job = await repo.claim_next()
        assert job is not None and job.attempts == attempt
        requeued, failed = await repo.recover_interrupted(max_attempts=3)
        stored = await repo.get_call("a")
        assert stored is not None
        if attempt < 3:
            assert (requeued, failed) == (1, 0)
            assert stored.analysis.status == "pending"
        else:
            assert (requeued, failed) == (0, 1)
            assert stored.analysis.status == "failed"
            assert "interrupted" in (stored.analysis.error or "")


async def test_release_does_not_charge_an_attempt(repo: SQLiteCallRepository) -> None:
    await repo.insert_call(make_record(call_id="a"))
    job = await repo.claim_next()
    assert job is not None
    assert await repo.release(job)
    again = await repo.claim_next()
    assert again is not None and again.attempts == 1


async def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    first = SQLiteCallRepository(path)
    await first.insert_call(make_record())
    await first.close()

    second = SQLiteCallRepository(path)
    items, _ = await second.list_calls(limit=10, cursor=None)
    await second.close()
    assert [i.call_id for i in items] == [make_record().call_id]
