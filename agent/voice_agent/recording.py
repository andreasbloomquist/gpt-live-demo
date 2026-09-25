"""Call recording: turn a finished session into a ``CallRecord`` and hand it to the analyzer.

At session end ``main.py`` calls :func:`build_call_record` on ``session.history`` and passes the
result to :class:`CallRecordExporter`. The record is the *CallRecord v1* wire format owned by
the Call Analyzer service (``analyzer/call_analyzer/models.py``); the agent deliberately sends
plain JSON rather than importing the analyzer's models, so the two stay separately deployable.

Two rules shape this module:

* **Recording must never hurt the call.** The export runs in a job shutdown callback, after the
  caller has gone. It is bounded in time, never raises, and falls back to a local JSON file
  when the analyzer isn't configured or doesn't accept the record, so a transcript is not lost
  to a transient outage.
* **Records must be accepted as-is.** The analyzer rejects over-limit bodies outright, so the
  builder enforces the same limits up front (truncating long turns, capping turn counts)
  instead of producing a record that would bounce.

Transcripts are PII: log lines here carry the call id, counts, and destination, never text.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
from livekit.agents import llm

from .config import Settings
from .prompts import PromptBundle

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Limits mirrored from the analyzer's CallRecord model (it rejects records that exceed them).
MAX_TURNS = 2000
MAX_TURN_CHARS = 20_000
MAX_TOOL_CALLS = 500
MAX_TOOL_ARGUMENT_CHARS = 20_000
MAX_TOOL_OUTPUT_CHARS = 50_000
MAX_USAGE_ENTRIES = 200
MAX_SHORT_TEXT = 256
MAX_ID_CHARS = 128  # turn ids, tool call ids, and tool names
MAX_CALL_DURATION_S = 24 * 3600
MAX_BODY_BYTES = 2 * 1024 * 1024

TRUNCATION_MARKER = " […truncated]"

# Call ids name files in CALL_RECORDS_DIR and appear in analyzer URLs, so they are restricted
# to a portable filename alphabet (a strict subset of what the analyzer accepts). The leading
# character can't be "." so an id can never be "." / ".." or a hidden file.
CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,199}$")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def new_call_id(room_name: str) -> str:
    """A unique, filename-safe call id: ``<sanitized room name>-<12 hex chars>``.

    Room names are chosen by whoever creates the room (the frontend, a SIP trunk), so they are
    sanitized rather than trusted; the random suffix keeps ids unique when a room is reused.
    """
    room_part = _UNSAFE_CHARS.sub("-", room_name).strip(".-")[:120] or "call"
    return f"{room_part}-{uuid.uuid4().hex[:12]}"


def build_call_record(
    items: Iterable[llm.ChatItem],
    *,
    call_id: str,
    room: str,
    agent_name: str,
    started_at: dt.datetime,
    ended_at: dt.datetime,
    end_reason: str | None,
    bundle: PromptBundle,
    settings: Settings,
    usage: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a CallRecord v1 dict from a session's chat history and metadata.

    ``items`` is ``session.history.items``. System/developer messages (instructions), agent
    handoffs, config updates, and messages without text are skipped; the remaining messages
    become ``turns`` in history order. Function calls are paired with their outputs by
    ``call_id``; an output whose call isn't in the history is dropped because the record can't
    say what was called.

    Deterministic (no I/O, no clock), so tests can pin every field. Raises :class:`ValueError`
    for an invalid ``call_id`` or naive datetimes: those are programming errors, not bad data.
    """
    if not CALL_ID_PATTERN.fullmatch(call_id):
        raise ValueError(f"call_id {call_id!r} is not filename-safe ({CALL_ID_PATTERN.pattern})")

    # Keyed by message id because the analyzer rejects duplicate turn ids. Session history
    # inserts messages without de-duplicating, so if an id ever repeats, the later (more
    # complete) version wins but keeps the position where the turn first appeared.
    turns_by_id: dict[str, dict[str, Any]] = {}
    calls: dict[str, dict[str, Any]] = {}  # insertion-ordered by call_id
    outputs: dict[str, llm.FunctionCallOutput] = {}
    for item in items:
        if isinstance(item, llm.ChatMessage):
            turn = _turn(item)
            if turn is not None:
                turns_by_id[turn["id"]] = turn
        # Keyed by the id as it will be sent (capped), so capping can't create duplicates.
        elif isinstance(item, llm.FunctionCall):
            calls.setdefault(item.call_id[:MAX_ID_CHARS], _tool_call(item))
        elif isinstance(item, llm.FunctionCallOutput):
            outputs.setdefault(item.call_id[:MAX_ID_CHARS], item)

    for tool_call_id, call in calls.items():
        if (output := outputs.get(tool_call_id)) is not None:
            call["output"] = _truncate(output.output, MAX_TOOL_OUTPUT_CHARS)
            call["is_error"] = output.is_error

    turns = list(turns_by_id.values())
    if len(turns) > MAX_TURNS:
        logger.warning(
            "call record turns capped",
            extra={"call_id": call_id, "turns": len(turns), "kept": MAX_TURNS},
        )
    ended_at = max(ended_at, started_at)  # clock steps must not produce an invalid record
    # A worker that outlives the analyzer's duration cap (e.g. a stuck room) still gets its
    # record accepted; the timestamps keep the true span.
    duration_s = min(round((ended_at - started_at).total_seconds(), 3), MAX_CALL_DURATION_S)
    return {
        "schema_version": SCHEMA_VERSION,
        "call_id": call_id,
        "room": room[:MAX_SHORT_TEXT],
        "agent_name": agent_name[:MAX_SHORT_TEXT],
        "started_at": _iso(started_at),
        "ended_at": _iso(ended_at),
        "duration_s": duration_s,
        "end_reason": end_reason[:MAX_SHORT_TEXT] if end_reason else None,
        "prompt": {
            "profile": bundle.profile,
            "fingerprint": bundle.fingerprint,
            "version": bundle.version,
            "voice": bundle.fingerprint_for("voice")[:12],
            "backend": bundle.fingerprint_for("backend")[:12],
        },
        "models": {
            "voice_model": settings.gpt_live_model,
            "voice": settings.gpt_live_voice,
            "backend_model": settings.gpt_live_backend_model,
        },
        # Keep the *first* turns when capping: the opening states the caller's intent.
        "turns": turns[:MAX_TURNS],
        "tool_calls": list(calls.values())[:MAX_TOOL_CALLS],
        "usage": [dict(entry) for entry in usage[:MAX_USAGE_ENTRIES]],
    }


def _turn(message: llm.ChatMessage) -> dict[str, Any] | None:
    if message.role not in ("user", "assistant"):
        return None  # system/developer messages are instructions, not conversation
    # text_content strips LiveKit's expressive <expr/> tags from assistant messages.
    text = (message.text_content or "").strip()
    if not text:
        return None
    metrics = message.metrics
    started = metrics.get("started_speaking_at", message.created_at)
    stopped = metrics.get("stopped_speaking_at")
    confidence = message.transcript_confidence
    return {
        "id": message.id[:MAX_ID_CHARS],
        "role": message.role,
        "text": _truncate(text, MAX_TURN_CHARS),
        "started_at": _iso_from_epoch(started),
        "ended_at": _iso_from_epoch(stopped) if stopped is not None else None,
        "interrupted": message.interrupted,
        # The analyzer rejects out-of-range values; an implausible confidence is no confidence.
        "transcript_confidence": (
            confidence if confidence is not None and 0.0 <= confidence <= 1.0 else None
        ),
    }


def _tool_call(call: llm.FunctionCall) -> dict[str, Any]:
    return {
        "id": call.call_id[:MAX_ID_CHARS],
        "name": call.name[:MAX_ID_CHARS],
        "arguments": _truncate(call.arguments, MAX_TOOL_ARGUMENT_CHARS),
        "output": None,  # filled in from the matching FunctionCallOutput, if any
        "is_error": False,
        "created_at": _iso_from_epoch(call.created_at),
    }


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("call record timestamps must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_from_epoch(seconds: float) -> str:
    return _iso(dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc))


# =============================================================================================
# Export
# =============================================================================================

Destination = Literal["analyzer", "file", "failed"]


@dataclasses.dataclass(frozen=True)
class ExportResult:
    destination: Destination
    path: Path | None = None
    detail: str | None = None  # why the analyzer was skipped or failed (no transcript content)


class CallRecordExporter:
    """Deliver call records to the Call Analyzer, falling back to a JSON file on disk.

    ``export`` POSTs to ``{analyzer_url}/v1/calls`` with a bearer token. Connection errors,
    5xx, and 429 get one retry; everything is bounded by ``total_timeout_s`` so the job's
    shutdown isn't held up by a slow analyzer. Other 4xx responses aren't retried (the record
    won't change) but are still saved to disk so nothing is lost. ``export`` never raises.
    """

    def __init__(
        self,
        records_dir: Path,
        *,
        analyzer_url: str | None = None,
        token: str | None = None,
        total_timeout_s: float = 5.0,
        attempt_timeout_s: float = 2.0,
        retry_delay_s: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if analyzer_url is not None and not token:
            raise ValueError("an analyzer_url needs a token")
        self._records_dir = records_dir
        self._url = f"{analyzer_url.rstrip('/')}/v1/calls" if analyzer_url else None
        self._token = token
        self._total_timeout_s = total_timeout_s
        self._attempt_timeout_s = attempt_timeout_s
        self._retry_delay_s = retry_delay_s
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> CallRecordExporter:
        """Raises :class:`~voice_agent.config.ConfigurationError` for a URL without a token."""
        endpoint = settings.call_analyzer_endpoint()
        url, token = endpoint if endpoint else (None, None)
        return cls(settings.call_records_dir, analyzer_url=url, token=token)

    async def export(self, record: Mapping[str, Any]) -> ExportResult:
        raw_id = record.get("call_id")
        call_id = raw_id if isinstance(raw_id, str) else ""  # str(None) would be a valid id
        turns = record.get("turns")
        log_fields = {"call_id": call_id, "turns": len(turns) if isinstance(turns, list) else 0}
        try:
            result = await self._export(call_id, record)
        except Exception:  # last line of defence: recording must never break job shutdown
            logger.exception("call record export failed", extra=log_fields)
            return ExportResult("failed", detail="unexpected error")
        if result.destination == "failed":
            level = logging.ERROR
        elif result.destination == "file" and self._url is not None:
            level = logging.WARNING  # the analyzer is configured but didn't take the record
        else:
            level = logging.INFO
        logger.log(
            level,
            "call record lost" if result.destination == "failed" else "call record exported",
            extra={
                **log_fields,
                "destination": str(result.path or result.destination),
                "detail": result.detail,
            },
        )
        return result

    async def _export(self, call_id: str, record: Mapping[str, Any]) -> ExportResult:
        if not CALL_ID_PATTERN.fullmatch(call_id):
            # Never build a file path from an unvalidated id.
            return ExportResult("failed", detail="invalid call_id")
        # errors="replace": a lone surrogate in a transcript (possible in decoded provider
        # JSON) becomes "?" instead of raising and losing the whole record.
        body = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", errors="replace"
        )

        detail = "CALL_ANALYZER_URL not set"
        if self._url is not None and self._token is not None:
            if len(body) > MAX_BODY_BYTES:
                detail = f"record is {len(body)} bytes, over the analyzer's limit"
            else:
                failure = await self._post(self._url, self._token, body)
                if failure is None:
                    return ExportResult("analyzer")
                detail = failure

        path = self._records_dir / f"{call_id}.json"
        try:
            await asyncio.to_thread(_write_atomic, path, body)
        except OSError as exc:
            return ExportResult("failed", detail=f"{detail}; file write failed: {exc}")
        return ExportResult("file", path=path, detail=detail)

    async def _post(self, url: str, token: str, body: bytes) -> str | None:
        """POST with one retry; ``None`` on success, else a short reason.

        Retried: transport errors, 5xx, and 429 (after its ``Retry-After``, if that still fits
        in the time budget). Other 4xx are final: the same record would be rejected again.
        """
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._total_timeout_s
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._attempt_timeout_s
        ) as client:

            async def attempts() -> str | None:
                reason = "no attempt made"
                delay = self._retry_delay_s
                for attempt in range(2):
                    if attempt:
                        await asyncio.sleep(delay)
                    try:
                        response = await client.post(url, content=body, headers=headers)
                    # Transport errors (connect/read/timeouts) and a response that can't be
                    # decoded: either way the analyzer may not have the record, so try again
                    # and then fall back to disk instead of losing it to export's catch-all.
                    except httpx.RequestError as exc:
                        reason = type(exc).__name__
                        delay = self._retry_delay_s
                        continue
                    if response.is_success:
                        return None
                    reason = f"analyzer returned HTTP {response.status_code}"
                    if response.status_code == 429:
                        retry_after = _retry_after_s(response)
                        delay = self._retry_delay_s if retry_after is None else retry_after
                        if delay >= deadline - loop.time():
                            break  # waiting would only run out the budget: save to disk now
                    elif response.status_code < 500:
                        break
                    else:
                        delay = self._retry_delay_s
                return reason

            try:
                return await asyncio.wait_for(attempts(), timeout=self._total_timeout_s)
            except asyncio.TimeoutError:
                return f"analyzer did not answer within {self._total_timeout_s:g}s"


def _retry_after_s(response: httpx.Response) -> float | None:
    """``Retry-After`` in seconds; ``None`` if absent or an HTTP date (not worth parsing here)."""
    try:
        value = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return value if 0 <= value < float("inf") else None


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so readers never see a partial file.

    The temp file lives in the target directory (``os.replace`` is only atomic within one
    filesystem) and is created ``0600`` by :mod:`tempfile`, which suits transcript contents.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
