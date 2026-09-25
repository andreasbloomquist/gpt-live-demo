"""The HTTP API (FastAPI). See README.md for the full reference with curl examples.

Security posture: every ``/v1`` route requires ``Authorization: Bearer <CALL_ANALYZER_TOKEN>``,
compared in constant time. Authentication runs *before* the body is read, and the body is read
with a hard byte cap, so an unauthenticated or oversized request costs almost nothing. Error
responses are always ``{"error": {"code", "message"}}`` JSON: no stack traces, and validation
errors never echo the submitted values (they may contain caller speech). Unexpected exceptions
are caught here and logged without their message (see :mod:`call_analyzer.logs`), so they never
reach the server's own traceback logging. The schema docs (``/docs``, ``/redoc``,
``/openapi.json``) are off unless ``ANALYZER_ENABLE_DOCS=true``.

The service is meant to sit behind the agent and the frontend's server routes, never a browser,
so there is deliberately no CORS configuration.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .analysis import CallAnalyzer
from .config import PLACEHOLDER_TOKEN, Settings
from .logs import describe_exception, safe_traceback
from .models import (
    CALL_ID_PATTERN,
    Analysis,
    AnalysisStatus,
    AnalyzerInfo,
    CallRecord,
    Outcome,
)
from .providers import build_provider
from .rubric import default_rubric
from .storage import CallRepository, InvalidCursorError, SQLiteCallRepository
from .worker import AnalysisWorker

logger = logging.getLogger(__name__)

MAX_ERROR_DETAILS = 20

_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
}


# --- Response models (also document the API in /docs) ------------------------------------------


class Accepted(BaseModel):
    call_id: str
    status: AnalysisStatus


class CallListItem(BaseModel):
    call_id: str
    started_at: dt.datetime
    duration_s: float
    turns: int
    status: AnalysisStatus
    caller_intent: str | None
    summary: str | None
    outcome: Outcome | None
    overall_score: int | None
    analyzer: AnalyzerInfo | None


class CallList(BaseModel):
    items: list[CallListItem]
    next_cursor: str | None


class CallDetail(BaseModel):
    record: CallRecord
    analysis: Analysis | None


# --- Errors -------------------------------------------------------------------------------------


def error_response(
    status: int,
    message: str,
    *,
    details: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": _ERROR_CODES.get(status, "error"), "message": message}
    if details:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status, headers=headers)


def _validation_details(errors: list[Any]) -> list[dict[str, Any]]:
    # Only location and message: pydantic's "input"/"ctx" would echo (possibly huge, possibly
    # personal) submitted data back to the client and into any logging proxy.
    return [
        {"loc": [str(part) for part in err.get("loc", ())], "msg": str(err.get("msg", ""))}
        for err in errors[:MAX_ERROR_DETAILS]
    ]


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(exc.status_code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            422, "Request validation failed.", details=_validation_details(list(exc.errors()))
        )


class CatchAllMiddleware:
    """Turn any unexpected exception into a generic 500 and log it *safely*.

    A plain ``exception_handler(Exception)`` isn't enough: Starlette re-raises after calling it,
    and uvicorn then logs the full exception, message included, which for a pydantic
    ``ValidationError`` means transcript text. Catching here means nothing reaches uvicorn.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            logger.error(
                "unhandled error",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "error": describe_exception(exc),
                    "stack": safe_traceback(exc),
                },
            )
            if not started:  # otherwise the response is half-sent; nothing sensible to add
                response = JSONResponse(
                    {"error": {"code": "internal_error", "message": "Internal server error."}},
                    status_code=500,
                )
                await response(scope, receive, send)


# --- Dependencies -------------------------------------------------------------------------------


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


async def require_token(request: Request) -> None:
    """Bearer auth. Both sides are hashed first so the comparison is constant-time and doesn't
    leak the token's length either. ``async`` because FastAPI runs sync dependencies in its
    threadpool, which would cost a thread hop on every request for no benefit."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    expected: bytes = request.app.state.token_digest
    if scheme.lower() != "bearer" or not secrets.compare_digest(_digest(token.strip()), expected):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def read_limited_body(request: Request, limit: int) -> bytes:
    """Read the request body, refusing more than ``limit`` bytes (declared *or* streamed)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length header.") from None
        if declared_bytes > limit:
            raise HTTPException(413, f"Request body exceeds {limit} bytes.")
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"Request body exceeds {limit} bytes.")
            chunks.append(chunk)
    except ClientDisconnect:
        # The client hung up mid-upload (the agent's timeout, a dropped link). Not a server
        # error: answer 400 (nobody will read it) and let the client retry.
        logger.info("client disconnected during upload", extra={"received_bytes": size})
        raise HTTPException(400, "Client disconnected before the body was complete.") from None
    return b"".join(chunks)


CallIdPath = Annotated[str, Path(pattern=CALL_ID_PATTERN)]


# --- App factory --------------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    *,
    repository: CallRepository | None = None,
    analyzer: CallAnalyzer | None = None,
) -> FastAPI:
    """Build the app. Configuration problems raise :class:`ConfigurationError` here, before the
    server binds a port. ``repository``/``analyzer`` are injectable for tests."""
    settings = settings or Settings()
    token = settings.require_api_token()
    if token == PLACEHOLDER_TOKEN:
        logger.warning("CALL_ANALYZER_TOKEN is the public placeholder; never use it when deployed")
    call_analyzer = analyzer or CallAnalyzer(build_provider(settings), default_rubric())
    repo = repository or SQLiteCallRepository(settings.db_path)
    worker = AnalysisWorker(
        repo,
        call_analyzer,
        concurrency=settings.concurrency,
        max_attempts=settings.max_attempts,
        retry_base_s=settings.retry_base_s,
        job_timeout_s=settings.job_timeout_s,
        poll_interval_s=settings.poll_interval_s,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        info = call_analyzer.info
        logger.info(
            "call analyzer starting",
            extra={
                "provider": info.provider,
                "model": info.model,
                "rubric_version": info.rubric_version,
            },
        )
        await worker.start()
        try:
            yield
        finally:
            await worker.stop()
            await call_analyzer.aclose()
            await repo.close()

    docs = settings.enable_docs
    app = FastAPI(
        title="Call Analyzer",
        version="1.0.0",
        summary="Stores voice-agent calls and grades them against a versioned rubric.",
        lifespan=lifespan,
        # Off by default: the schema is a map of the API for anyone who can reach the port.
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.token_digest = _digest(token)
    app.state.repo = repo
    app.state.worker = worker
    _install_error_handlers(app)
    app.add_middleware(CatchAllMiddleware)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

    @v1.post(
        "/calls",
        status_code=202,
        response_model=Accepted,
        # The body is read by hand (to enforce the byte cap before parsing), so describe it here.
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/CallRecord"}}
                },
            }
        },
    )
    async def ingest_call(request: Request) -> Any:
        """Store a CallRecord and queue its analysis.

        Idempotent by ``call_id``: re-posting an identical record returns 200 with the current
        status (nothing is re-analyzed); a *different* record under an existing id is a 409.
        """
        body = await read_limited_body(request, settings.max_body_bytes)
        try:
            record = CallRecord.model_validate_json(body)
        except ValidationError as exc:
            return error_response(
                422, "Invalid call record.", details=_validation_details(exc.errors())
            )
        outcome, status = await repo.insert_call(record)
        if outcome == "conflict":
            raise HTTPException(409, "A different call record with this call_id already exists.")
        if outcome == "duplicate":
            return JSONResponse(Accepted(call_id=record.call_id, status=status).model_dump())
        worker.notify()
        logger.info("call stored", extra={"call_id": record.call_id, "turns": len(record.turns)})
        return Accepted(call_id=record.call_id, status=status)

    @v1.get("/calls", response_model=CallList)
    async def list_calls(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> CallList:
        """Calls newest first (by call start time), keyset-paginated via ``next_cursor``."""
        try:
            rows, next_cursor = await repo.list_calls(limit, cursor)
        except InvalidCursorError:
            raise HTTPException(400, "Invalid cursor.") from None
        items = []
        for row in rows:
            analysis = row.analysis.to_analysis()
            items.append(
                CallListItem(
                    call_id=row.call_id,
                    started_at=row.started_at,
                    duration_s=row.duration_s,
                    turns=row.turns,
                    status=analysis.status,
                    caller_intent=analysis.caller_intent,
                    summary=analysis.summary,
                    outcome=analysis.outcome,
                    overall_score=analysis.overall_score,
                    analyzer=analysis.analyzer,
                )
            )
        return CallList(items=items, next_cursor=next_cursor)

    @v1.get("/calls/{call_id}", response_model=CallDetail)
    async def get_call(call_id: CallIdPath) -> Response:
        stored = await repo.get_call(call_id)
        if stored is None:
            raise HTTPException(404, "Call not found.")
        # The record is served exactly as stored (it was validated and normalized at ingest):
        # re-validating would turn every model tightening into a 500 for older rows.
        analysis = stored.analysis.to_analysis().model_dump_json()
        body = f'{{"record":{stored.record_json},"analysis":{analysis}}}'
        return Response(body, media_type="application/json")

    @v1.post("/calls/{call_id}/analyze", status_code=202, response_model=Accepted)
    async def reanalyze(call_id: CallIdPath) -> Accepted:
        """Force a fresh analysis (e.g. after a rubric or model change). Any in-flight run for
        this call is superseded: its result will be discarded."""
        if not await repo.request_analysis(call_id):
            raise HTTPException(404, "Call not found.")
        worker.notify()
        return Accepted(call_id=call_id, status="pending")

    app.include_router(v1)
    return app
