"""The in-process background worker that turns pending rows into analyses.

``concurrency`` loops each claim one due job at a time from the repository (the database is the
queue; see :mod:`call_analyzer.storage`). Ingest calls :meth:`AnalysisWorker.notify` so a new
call starts within milliseconds; otherwise loops poll every ``poll_interval_s``, which also
picks up retries whose backoff has elapsed.

Failure handling:

* Retryable provider errors (429, 5xx, timeouts, malformed answers) are re-queued with
  exponential backoff plus jitter, up to ``max_attempts``; then the row is ``failed``.
* Non-retryable errors (bad key, unknown model, refusals) fail immediately.
* A job that overruns ``job_timeout_s`` is cancelled and counts as a retryable failure.
* Unexpected exceptions are bugs: logged with a traceback, stored as a generic message (never
  the exception text, which could echo transcript content), and not retried.
* On shutdown, in-flight jobs are cancelled and their rows stay ``running``; the next startup's
  :meth:`start` requeues them (restart recovery).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from .analysis import CallAnalyzer
from .models import CallRecord
from .providers.base import ProviderError
from .storage import CallRepository, Job

logger = logging.getLogger(__name__)

MAX_BACKOFF_S = 30 * 60


def backoff_delay(attempt: int, base_s: float, retry_after_s: float | None = None) -> float:
    """Exponential backoff with +/-20% jitter; never earlier than a server's Retry-After."""
    delay = min(MAX_BACKOFF_S, base_s * 2 ** max(0, attempt - 1)) * random.uniform(0.8, 1.2)
    if retry_after_s is not None:
        delay = max(delay, retry_after_s)
    return delay


class AnalysisWorker:
    def __init__(
        self,
        repo: CallRepository,
        analyzer: CallAnalyzer,
        *,
        concurrency: int = 2,
        max_attempts: int = 3,
        retry_base_s: float = 30.0,
        job_timeout_s: float = 300.0,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._repo = repo
        self._analyzer = analyzer
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        self._retry_base_s = retry_base_s
        self._job_timeout_s = job_timeout_s
        self._poll_interval_s = poll_interval_s
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        requeued, failed = await self._repo.recover_interrupted(self._max_attempts)
        if requeued or failed:
            logger.info(
                "recovered interrupted analyses", extra={"requeued": requeued, "failed": failed}
            )
        self._tasks = [
            asyncio.create_task(self._loop(i), name=f"analysis-worker-{i}")
            for i in range(self._concurrency)
        ]

    def notify(self) -> None:
        """Wake idle loops: there may be new work."""
        self._wake.set()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def run_until_idle(self) -> None:
        """Process due jobs in the current task until none are left (CLI ``seed``, tests)."""
        while (job := await self._repo.claim_next()) is not None:
            await self._process(job)

    async def _loop(self, index: int) -> None:
        while True:
            try:
                job = await self._repo.claim_next()
                if job is not None:
                    await self._process(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # A storage hiccup (disk full, locked DB) must not kill the loop for good.
                logger.exception("analysis worker loop error", extra={"worker": index})
            # A notify() that lands between claim_next() and here is not lost: the event
            # stays set, so wait() returns immediately and we claim again.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval_s)
            self._wake.clear()

    async def _process(self, job: Job) -> None:
        log_extra = {"call_id": job.call_id, "attempt": job.attempts}
        stored = await self._repo.get_call(job.call_id)
        if stored is None:  # deleted between claim and load
            return
        record = CallRecord.model_validate_json(stored.record_json)
        try:
            analysis = await asyncio.wait_for(
                self._analyzer.analyze(record), timeout=self._job_timeout_s
            )
        except asyncio.TimeoutError:
            await self._handle_failure(
                job, ProviderError(f"analysis timed out after {self._job_timeout_s:g}s", retryable=True)
            )
        except ProviderError as exc:
            await self._handle_failure(job, exc)
        except Exception:
            logger.exception("unexpected error analyzing call", extra=log_extra)
            await self._repo.fail(job, "internal error while analyzing (see service logs)")
        else:
            if await self._repo.complete(job, analysis):
                logger.info(
                    "analysis done",
                    extra={**log_extra, "overall_score": analysis.overall_score},
                )
            else:
                logger.info("discarded stale analysis (call was re-queued)", extra=log_extra)

    async def _handle_failure(self, job: Job, exc: ProviderError) -> None:
        message = str(exc)
        log_extra = {"call_id": job.call_id, "attempt": job.attempts, "error": message}
        if exc.retryable and job.attempts < self._max_attempts:
            delay = backoff_delay(job.attempts, self._retry_base_s, exc.retry_after_s)
            logger.warning("analysis failed; will retry", extra={**log_extra, "delay_s": delay})
            await self._repo.retry_later(job, message, delay)
        else:
            logger.warning("analysis failed permanently", extra=log_extra)
            await self._repo.fail(job, message)
