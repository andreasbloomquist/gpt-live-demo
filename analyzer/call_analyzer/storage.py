"""Persistence behind a small repository interface, with a SQLite implementation.

The database is also the work queue: an analysis row in ``pending`` state *is* the job. That
makes jobs durable for free (a restart loses nothing) and means ingest never has to reject a
call because an in-memory queue is full; a backlog is just rows waiting their turn.

Two tables, because they have different lifecycles: ``calls`` holds the immutable record the
agent sent; ``analyses`` holds the current, re-computable verdict and its job state.

Concurrency: SQLite allows one writer at a time, so one connection guarded by a lock is both
correct and fast enough for this workload (tens of writes per call). Every blocking call runs in
a worker thread (``asyncio.to_thread``) so the event loop never waits on disk. Re-analysis races
are handled with a ``generation`` counter: each request bumps it, and a worker may only write
results for the generation it claimed, so a stale run can never overwrite a newer one.

Swapping in Postgres means implementing :class:`CallRepository`; the claim query becomes
``SELECT ... FOR UPDATE SKIP LOCKED``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from .models import Analysis, AnalysisStatus, CallRecord

T = TypeVar("T")

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id      TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    started_at   TEXT NOT NULL,          -- UTC, fixed-width ISO-8601, so it sorts as text
    duration_s   REAL NOT NULL,
    turn_count   INTEGER NOT NULL,
    record_json  TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_newest_first ON calls (started_at DESC, call_id DESC);

CREATE TABLE IF NOT EXISTS analyses (
    call_id         TEXT PRIMARY KEY REFERENCES calls (call_id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
    generation      INTEGER NOT NULL,     -- bumped on every (re)analysis request
    attempts        INTEGER NOT NULL,     -- attempts within the current generation
    next_attempt_at TEXT NOT NULL,        -- pending rows become claimable at this time
    requested_at    TEXT NOT NULL,
    error           TEXT,
    analysis_json   TEXT                  -- set when status = 'done'
);
CREATE INDEX IF NOT EXISTS analyses_due ON analyses (status, next_attempt_at);
"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ts(value: dt.datetime) -> str:
    """Fixed-width UTC timestamp: lexicographic order == chronological order."""
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)


class InvalidCursorError(ValueError):
    """The pagination cursor wasn't produced by this service."""


def encode_cursor(started_at: str, call_id: str) -> str:
    raw = json.dumps([started_at, call_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        started_at, call_id = json.loads(raw)
        _parse_ts(started_at)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise InvalidCursorError("invalid cursor") from exc
    if not isinstance(call_id, str):
        raise InvalidCursorError("invalid cursor")
    return started_at, call_id


@dataclass(frozen=True)
class AnalysisState:
    """The current analysis row, without the (possibly large) record."""

    call_id: str
    status: AnalysisStatus
    generation: int
    attempts: int
    requested_at: dt.datetime
    error: str | None
    analysis_json: str | None

    def to_analysis(self) -> Analysis:
        """The API view: the stored result when done, else a status-only placeholder."""
        if self.status == "done" and self.analysis_json:
            return Analysis.model_validate_json(self.analysis_json)
        return Analysis(
            call_id=self.call_id,
            status=self.status,
            error=self.error,
            created_at=self.requested_at,
        )


@dataclass(frozen=True)
class StoredCall:
    record_json: str
    analysis: AnalysisState


@dataclass(frozen=True)
class CallSummary:
    call_id: str
    started_at: dt.datetime
    duration_s: float
    turns: int
    analysis: AnalysisState


@dataclass(frozen=True)
class Job:
    call_id: str
    generation: int
    attempts: int


InsertOutcome = Literal["created", "duplicate", "conflict"]


class CallRepository(Protocol):
    async def insert_call(self, record: CallRecord) -> tuple[InsertOutcome, AnalysisStatus]: ...
    async def get_call(self, call_id: str) -> StoredCall | None: ...
    async def list_calls(
        self, limit: int, cursor: str | None
    ) -> tuple[list[CallSummary], str | None]: ...
    async def request_analysis(self, call_id: str) -> bool: ...
    async def claim_next(self) -> Job | None: ...
    async def complete(self, job: Job, analysis: Analysis) -> bool: ...
    async def retry_later(self, job: Job, error: str, delay_s: float) -> bool: ...
    async def fail(self, job: Job, error: str) -> bool: ...
    async def release(self, job: Job) -> bool: ...
    async def recover_interrupted(self, max_attempts: int) -> tuple[int, int]: ...
    async def close(self) -> None: ...


class SQLiteCallRepository:
    """SQLite implementation of :class:`CallRepository` (stdlib ``sqlite3`` in a thread)."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # One connection, used from worker threads one at a time (serialized by the lock).
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn
            conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            conn.execute("PRAGMA synchronous=NORMAL")  # durable enough with WAL, much faster
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema v{version} is newer than this build (v{SCHEMA_VERSION})"
                )
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            with self._lock:
                return fn(self._conn)

        return await asyncio.to_thread(locked)

    async def _write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn`` inside one IMMEDIATE transaction (takes the write lock up front)."""

        def tx(conn: sqlite3.Connection) -> T:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.execute("COMMIT")
            except BaseException:
                # Also covers a failed COMMIT (e.g. SQLITE_BUSY), which leaves the transaction
                # open and would make every later BEGIN fail.
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return result

        return await self._run(tx)

    # --- calls --------------------------------------------------------------------------------

    async def insert_call(self, record: CallRecord) -> tuple[InsertOutcome, AnalysisStatus]:
        content_hash = record.content_hash()
        record_json = record.model_dump_json()
        now = _ts(utc_now())

        def insert(conn: sqlite3.Connection) -> tuple[InsertOutcome, AnalysisStatus]:
            row = conn.execute(
                "SELECT c.content_hash, a.status FROM calls c JOIN analyses a USING (call_id) "
                "WHERE c.call_id = ?",
                (record.call_id,),
            ).fetchone()
            if row is not None:
                return ("duplicate" if row[0] == content_hash else "conflict"), row[1]
            conn.execute(
                "INSERT INTO calls (call_id, content_hash, started_at, duration_s, turn_count, "
                "record_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.call_id,
                    content_hash,
                    _ts(record.started_at),
                    record.duration_s,
                    len(record.turns),
                    record_json,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO analyses (call_id, status, generation, attempts, next_attempt_at, "
                "requested_at) VALUES (?, 'pending', 1, 0, ?, ?)",
                (record.call_id, now, now),
            )
            return "created", "pending"

        return await self._write(insert)

    async def get_call(self, call_id: str) -> StoredCall | None:
        def get(conn: sqlite3.Connection) -> StoredCall | None:
            row = conn.execute(
                f"SELECT c.record_json, {_ANALYSIS_COLUMNS} FROM calls c "
                "JOIN analyses a USING (call_id) WHERE c.call_id = ?",
                (call_id,),
            ).fetchone()
            if row is None:
                return None
            return StoredCall(record_json=row[0], analysis=_analysis_state(row[1:]))

        return await self._run(get)

    async def list_calls(
        self, limit: int, cursor: str | None
    ) -> tuple[list[CallSummary], str | None]:
        """Newest first (by call start), keyset-paginated so pages stay stable under inserts."""
        where, params = "", []
        if cursor is not None:
            started_at, call_id = decode_cursor(cursor)
            where = "WHERE c.started_at < ? OR (c.started_at = ? AND c.call_id < ?)"
            params = [started_at, started_at, call_id]

        def query(conn: sqlite3.Connection) -> list[tuple]:
            return conn.execute(
                f"SELECT c.started_at, c.duration_s, c.turn_count, {_ANALYSIS_COLUMNS} "
                f"FROM calls c JOIN analyses a USING (call_id) {where} "
                "ORDER BY c.started_at DESC, c.call_id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()

        rows = await self._run(query)
        items = [
            CallSummary(
                call_id=row[3],
                started_at=_parse_ts(row[0]),
                duration_s=row[1],
                turns=row[2],
                analysis=_analysis_state(row[3:]),
            )
            for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(last[0], last[3])
        return items, next_cursor

    # --- analysis jobs ------------------------------------------------------------------------

    async def request_analysis(self, call_id: str) -> bool:
        """(Re)queue analysis of an existing call. Returns False if the call doesn't exist."""
        now = _ts(utc_now())

        def request(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE analyses SET status = 'pending', generation = generation + 1, "
                "attempts = 0, next_attempt_at = ?, requested_at = ?, error = NULL, "
                "analysis_json = NULL WHERE call_id = ?",
                (now, now, call_id),
            )
            return cur.rowcount == 1

        return await self._write(request)

    async def claim_next(self) -> Job | None:
        """Atomically move the oldest due pending job to ``running`` and return it."""
        now = _ts(utc_now())

        def claim(conn: sqlite3.Connection) -> Job | None:
            row = conn.execute(
                "SELECT call_id, generation, attempts FROM analyses "
                "WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, call_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE analyses SET status = 'running', attempts = attempts + 1 "
                "WHERE call_id = ? AND generation = ?",
                (row[0], row[1]),
            )
            return Job(call_id=row[0], generation=row[1], attempts=row[2] + 1)

        return await self._write(claim)

    async def _finish(self, job: Job, sql: str, params: tuple) -> bool:
        """Apply a state change only if the job is still the current, running generation."""

        def finish(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                f"UPDATE analyses SET {sql} "
                "WHERE call_id = ? AND generation = ? AND status = 'running'",
                (*params, job.call_id, job.generation),
            )
            return cur.rowcount == 1

        return await self._write(finish)

    async def complete(self, job: Job, analysis: Analysis) -> bool:
        return await self._finish(
            job, "status = 'done', error = NULL, analysis_json = ?", (analysis.model_dump_json(),)
        )

    async def retry_later(self, job: Job, error: str, delay_s: float) -> bool:
        next_at = _ts(utc_now() + dt.timedelta(seconds=delay_s))
        return await self._finish(
            job, "status = 'pending', error = ?, next_attempt_at = ?", (error, next_at)
        )

    async def fail(self, job: Job, error: str) -> bool:
        return await self._finish(job, "status = 'failed', error = ?", (error,))

    async def release(self, job: Job) -> bool:
        """Hand a job back untouched (graceful shutdown): pending again, attempt not charged."""
        return await self._finish(
            job,
            "status = 'pending', attempts = attempts - 1, next_attempt_at = ?",
            (_ts(utc_now()),),
        )

    async def recover_interrupted(self, max_attempts: int) -> tuple[int, int]:
        """On startup, rows left ``running`` belong to a process that died mid-job. Requeue
        them, unless they've used up their attempts (a job that keeps crashing the process
        must not crash-loop it forever). Returns ``(requeued, failed)``."""
        now = _ts(utc_now())

        def recover(conn: sqlite3.Connection) -> tuple[int, int]:
            failed = conn.execute(
                "UPDATE analyses SET status = 'failed', "
                "error = 'interrupted too many times (service restarted mid-analysis)' "
                "WHERE status = 'running' AND attempts >= ?",
                (max_attempts,),
            ).rowcount
            requeued = conn.execute(
                "UPDATE analyses SET status = 'pending', next_attempt_at = ? "
                "WHERE status = 'running'",
                (now,),
            ).rowcount
            return requeued, failed

        return await self._write(recover)

    async def close(self) -> None:
        await self._run(lambda conn: conn.close())


_ANALYSIS_COLUMNS = (
    "a.call_id, a.status, a.generation, a.attempts, a.requested_at, a.error, a.analysis_json"
)


def _analysis_state(row: tuple) -> AnalysisState:
    call_id, status, generation, attempts, requested_at, error, analysis_json = row
    return AnalysisState(
        call_id=call_id,
        status=status,
        generation=generation,
        attempts=attempts,
        requested_at=_parse_ts(requested_at),
        error=error,
        analysis_json=analysis_json,
    )
