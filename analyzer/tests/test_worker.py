from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from call_analyzer.analysis import CallAnalyzer
from call_analyzer.models import AssessmentDraft, CallRecord, Metrics
from call_analyzer.providers.base import ProviderError
from call_analyzer.providers.heuristic import HeuristicProvider
from call_analyzer.rubric import Rubric
from call_analyzer.storage import SQLiteCallRepository
from call_analyzer.worker import AnalysisWorker, backoff_delay
from tests.factories import make_record


class ScriptedProvider:
    """Fails according to a script, then behaves like the heuristic provider."""

    name = "openai"
    model = "scripted"

    def __init__(self, *failures: BaseException, hang: bool = False) -> None:
        self.failures = list(failures)
        self.calls = 0
        self.hang = hang
        self._fallback = HeuristicProvider()

    async def assess(self, record: CallRecord, metrics: Metrics, rubric: Rubric) -> AssessmentDraft:
        self.calls += 1
        if self.hang:
            await asyncio.sleep(3600)
        if self.failures:
            raise self.failures.pop(0)
        return await self._fallback.assess(record, metrics, rubric)

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[SQLiteCallRepository]:
    repository = SQLiteCallRepository(tmp_path / "calls.db")
    yield repository
    await repository.close()


def worker_for(repo, provider, rubric: Rubric, **kwargs) -> AnalysisWorker:
    options = {"retry_base_s": 0.0, "poll_interval_s": 0.01, **kwargs}
    return AnalysisWorker(repo, CallAnalyzer(provider, rubric), **options)


async def status_of(repo: SQLiteCallRepository, call_id: str):
    stored = await repo.get_call(call_id)
    assert stored is not None
    return stored.analysis


async def wait_for_status(repo, call_id: str, *statuses: str, timeout: float = 5.0):
    async def poll():
        while (state := await status_of(repo, call_id)).status not in statuses:
            await asyncio.sleep(0.01)
        return state

    return await asyncio.wait_for(poll(), timeout)


async def test_retryable_errors_are_retried_then_succeed(repo, rubric: Rubric) -> None:
    provider = ScriptedProvider(ProviderError("429", retryable=True))
    await repo.insert_call(make_record(call_id="a"))
    await worker_for(repo, provider, rubric).run_until_idle()

    state = await status_of(repo, "a")
    assert state.status == "done" and provider.calls == 2
    assert state.error is None


async def test_retries_stop_at_max_attempts(repo, rubric: Rubric) -> None:
    errors = [ProviderError("503", retryable=True) for _ in range(5)]
    provider = ScriptedProvider(*errors)
    await repo.insert_call(make_record(call_id="a"))
    await worker_for(repo, provider, rubric, max_attempts=3).run_until_idle()

    state = await status_of(repo, "a")
    assert (state.status, state.error, provider.calls) == ("failed", "503", 3)


async def test_non_retryable_errors_fail_immediately(repo, rubric: Rubric) -> None:
    provider = ScriptedProvider(ProviderError("bad key", retryable=False))
    await repo.insert_call(make_record(call_id="a"))
    await worker_for(repo, provider, rubric).run_until_idle()
    state = await status_of(repo, "a")
    assert (state.status, state.error, provider.calls) == ("failed", "bad key", 1)


async def test_unexpected_errors_fail_without_leaking_details(repo, rubric: Rubric) -> None:
    provider = ScriptedProvider(RuntimeError("caller said: my card is 4111..."))
    await repo.insert_call(make_record(call_id="a"))
    await worker_for(repo, provider, rubric).run_until_idle()
    state = await status_of(repo, "a")
    assert state.status == "failed"
    assert "4111" not in (state.error or "")


async def test_unloadable_record_fails_instead_of_staying_running(repo, rubric: Rubric) -> None:
    # E.g. a stored record that a newer build's CallRecord no longer accepts.
    await repo.insert_call(make_record(call_id="a"))
    await repo._run(lambda conn: conn.execute("UPDATE calls SET record_json = '{}'"))
    await worker_for(repo, HeuristicProvider(), rubric).run_until_idle()
    state = await status_of(repo, "a")
    assert state.status == "failed"


async def test_job_timeout_counts_as_retryable_failure(repo, rubric: Rubric) -> None:
    provider = ScriptedProvider(hang=True)
    await repo.insert_call(make_record(call_id="a"))
    await worker_for(repo, provider, rubric, job_timeout_s=0.05, max_attempts=1).run_until_idle()
    state = await status_of(repo, "a")
    assert state.status == "failed" and "timed out" in (state.error or "")


async def test_background_loops_pick_up_new_work(repo, rubric: Rubric) -> None:
    worker = worker_for(repo, HeuristicProvider(), rubric, concurrency=2, poll_interval_s=10)
    await worker.start()
    try:
        for i in range(3):
            await repo.insert_call(make_record(call_id=f"c{i}"))
            worker.notify()  # without this the loops would sleep for the 10 s poll interval
        for i in range(3):
            await wait_for_status(repo, f"c{i}", "done")
    finally:
        await worker.stop()


async def test_graceful_stop_hands_jobs_back_uncharged(repo, rubric: Rubric) -> None:
    await repo.insert_call(make_record(call_id="a"))
    hung = worker_for(repo, ScriptedProvider(hang=True), rubric)
    await hung.start()
    await wait_for_status(repo, "a", "running")
    await hung.stop()

    state = await status_of(repo, "a")
    assert (state.status, state.attempts) == ("pending", 0)


async def test_restart_recovers_interrupted_jobs(tmp_path: Path, rubric: Rubric) -> None:
    path = tmp_path / "calls.db"
    # Process 1 claims a job and dies mid-analysis (no shutdown): the row is left `running`.
    first = SQLiteCallRepository(path)
    await first.insert_call(make_record(call_id="a"))
    assert await first.claim_next() is not None
    await first.close()

    # Process 2 starts, recovers the row, and finishes it.
    second = SQLiteCallRepository(path)
    worker = worker_for(second, HeuristicProvider(), rubric)
    await worker.start()
    try:
        state = await wait_for_status(second, "a", "done")
        assert state.to_analysis().overall_score is not None
    finally:
        await worker.stop()
        await second.close()


def test_backoff_grows_and_honours_retry_after() -> None:
    assert 24 <= backoff_delay(1, 30) <= 36
    assert 96 <= backoff_delay(3, 30) <= 144
    assert backoff_delay(20, 30) <= 30 * 60 * 1.2
    assert backoff_delay(1, 1, retry_after_s=90) == 90
