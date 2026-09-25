"""Logging setup: one line per event, with structured ``extra={...}`` fields appended.

The code logs identifiers and counts (call_id, attempt, delay), never transcript text or
secrets, so these lines are safe to ship to a log aggregator.

Exceptions are the one place content could sneak in: a pydantic ``ValidationError`` prints the
offending ``input_value`` (caller speech, tool output), and any other exception message may quote
its input. So exceptions are only ever logged through :func:`describe_exception` and
:func:`safe_traceback`, which keep the exception *type*, the stack frames (file, line, code) and,
for validation errors, each error's ``loc``/``msg``, but never the message or input. The
formatter applies the same rule to any ``exc_info`` a library logs (e.g. uvicorn).
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

# Attributes every LogRecord has; anything else was passed via `extra=`.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI-coloured duplicate of the message
}
_MAX_ERROR_DETAILS = 10


def describe_exception(exc: BaseException) -> str:
    """``TypeName`` plus, for validation errors, ``[loc: msg, ...]``; never the message."""
    name = type(exc).__name__
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return name
    try:
        # include_input=False where supported (pydantic); loc/msg never carry the input.
        try:
            items = errors(include_input=False, include_url=False)
        except TypeError:
            items = errors()
        details = [
            f"{'.'.join(str(part) for part in item.get('loc', ()))}: {item.get('msg', '')}"
            for item in list(items)[:_MAX_ERROR_DETAILS]
        ]
    except Exception:  # a broken errors() must not break logging
        return name
    return f"{name} [{'; '.join(details)}]" if details else name


def safe_traceback(exc: BaseException) -> str:
    """The traceback of ``exc`` and its causes, with every exception message withheld."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        suppressed = current.__suppress_context__
        current = current.__cause__ or (None if suppressed else current.__context__)
    parts: list[str] = []
    for item in reversed(chain):
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_tb(item.__traceback__))
        parts.append(f"{describe_exception(item)} (message withheld: may contain call content)\n")
        # Exception groups (anyio/Starlette): include the members the same way.
        for member in getattr(item, "exceptions", ()) or ():
            if isinstance(member, BaseException):
                parts.append(safe_traceback(member))
    return "".join(parts).rstrip("\n")


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # logging caches the formatted traceback on the record; another handler may have put
        # the unredacted one there, so always format our own.
        cached, record.exc_text = record.exc_text, None
        try:
            line = super().format(record)
        finally:
            record.exc_text = cached
        extras = {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}
        if extras:
            line += " " + " ".join(f"{k}={v!r}" for k, v in extras.items())
        return line

    def formatException(self, ei: Any) -> str:
        _, exc, _ = ei
        return safe_traceback(exc) if exc is not None else ""


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(KeyValueFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
