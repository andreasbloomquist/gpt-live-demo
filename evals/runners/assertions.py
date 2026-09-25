"""Deterministic checks shared by both tiers.

Both runners reduce a conversation to a :class:`Transcript` so every case is graded by the
same code regardless of how it was produced (Responses API loop vs. live GPT-Live session).
Deterministic checks run *before* the LLM judge: they are free, never flaky, and a judge
should not be asked to excuse a wrong tool call.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

from evals.schema import Expect, ExpectedToolCall


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    output: str | None = None
    is_error: bool = False


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    """A tool call's JSON arguments as a dict. Malformed or non-object JSON (the model's
    mistake, worth seeing in the transcript) is kept under ``__raw__`` / ``__value__``."""
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__raw__": raw}
    return value if isinstance(value, dict) else {"__value__": value}


@dataclass
class Transcript:
    """A graded conversation. ``messages`` are ``(role, text)`` in order."""

    messages: list[tuple[str, str]] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    unobservable_tools: frozenset[str] = frozenset()
    """Tools that run where this tier cannot see them (e.g. provider-side ``web_search`` inside a
    GPT-Live session). Expectations about them are skipped, never silently passed."""

    @property
    def final_reply(self) -> str:
        """All assistant speech after the last user message: in voice, one answer is often
        split into a filler ("let me check…") and the result."""
        out: list[str] = []
        for role, text in reversed(self.messages):
            if role == "user":
                break
            if role == "assistant":
                out.append(text)
        return " ".join(reversed(out)).strip()

    @property
    def assistant_text(self) -> str:
        """Assistant speech after the first user message (guardrails hold for the whole call;
        the greeting is excluded because it is not a response to the caller)."""
        seen_user = False
        out = []
        for role, text in self.messages:
            seen_user = seen_user or role == "user"
            if seen_user and role == "assistant":
                out.append(text)
        return "\n".join(out)

    def render(self) -> str:
        lines = [f"{role.upper()}: {text}" for role, text in self.messages]
        for call in self.tool_calls:
            lines.append(f"[tool {call.name}({call.arguments}) -> {(call.output or '')[:300]}]")
        return "\n".join(lines)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False


# --------------------------------------------------------------------------------------------


def _norm(value: Any) -> Any:
    return " ".join(value.lower().split()) if isinstance(value, str) else value


def match_value(expected: Any, actual: Any, *, today: dt.date | None = None) -> bool:
    """Lenient argument matching.

    * plain strings: case- and whitespace-insensitive equality;
    * numbers: numeric equality, accepting numeric strings (``"4"`` == ``4``);
    * ``{"$regex": p}`` (search, case-insensitive), ``{"$in": [...]}``,
      ``{"$contains": s}``, ``{"$exists": bool}``;
    * ``{"$days_from_today": n}``: ISO date equal to today + n in the agent's timezone, which
      checks relative-date resolution ("tomorrow") without hard-coding a calendar date.
    """
    if isinstance(expected, dict) and len(expected) == 1 and next(iter(expected)).startswith("$"):
        op, arg = next(iter(expected.items()))
        if op == "$exists":
            return (actual is not None) == bool(arg)
        if actual is None:
            return False
        if op == "$regex":
            return re.search(str(arg), str(actual), re.IGNORECASE) is not None
        if op == "$in":
            return any(match_value(option, actual, today=today) for option in arg)
        if op == "$contains":
            return _norm(str(arg)) in _norm(str(actual))
        if op == "$days_from_today":
            base = today or dt.date.today()
            return str(actual).strip() == (base + dt.timedelta(days=int(arg))).isoformat()
        raise ValueError(f"unknown matcher {op}")
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, (int, float)):
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    return _norm(expected) == _norm(actual)


def _call_matches(exp: ExpectedToolCall, call: ToolCall, today: dt.date | None) -> str | None:
    """``None`` if ``call`` satisfies ``exp``; otherwise why not."""
    if call.name != exp.name:
        return f"name {call.name}"
    for key, want in exp.args_subset.items():
        got = call.arguments.get(key)
        if not match_value(want, got, today=today):
            return f"{key}={got!r} (want {want!r})"
    return None


def check_expectations(
    expect: Expect, transcript: Transcript, *, today: dt.date | None = None
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    observable_calls = transcript.tool_calls
    unobservable = transcript.unobservable_tools

    cursor = 0
    for exp in expect.tool_calls:
        name = f"tool_call:{exp.name}"
        if exp.name in unobservable:
            checks.append(CheckResult(name, True, "not observable in this tier", skipped=True))
            continue
        candidates = observable_calls[cursor:] if expect.ordered else observable_calls
        mismatches = []
        hit = None
        for idx, call in enumerate(candidates):
            why = _call_matches(exp, call, today)
            if why is None:
                hit = idx
                break
            if call.name == exp.name:
                mismatches.append(why)
        if hit is not None:
            if expect.ordered:
                cursor += hit + 1
            checks.append(CheckResult(name, True))
        else:
            seen = [c.name for c in observable_calls] or "none"
            detail = f"not called (calls: {seen})"
            if mismatches:
                detail = "argument mismatch: " + "; ".join(mismatches)
            checks.append(CheckResult(name, False, detail))

    if expect.no_tool_calls:
        names = [c.name for c in observable_calls]
        if names:
            checks.append(CheckResult("no_tool_calls", False, f"unexpected calls: {names}"))
        elif unobservable:
            # No visible calls, but a call to an unobservable tool could still have happened.
            detail = f"cannot observe {sorted(unobservable)} in this tier"
            checks.append(CheckResult("no_tool_calls", True, detail, skipped=True))
        else:
            checks.append(CheckResult("no_tool_calls", True))

    for tool in expect.forbidden_tools:
        if tool in unobservable:
            checks.append(CheckResult(f"forbidden:{tool}", True, "not observable", skipped=True))
            continue
        called = any(c.name == tool for c in observable_calls)
        checks.append(CheckResult(f"forbidden:{tool}", not called, "called" if called else ""))

    if expect.max_words is not None:
        words = len(transcript.final_reply.split())
        checks.append(
            CheckResult(
                "max_words",
                0 < words <= expect.max_words,
                f"{words} words (max {expect.max_words})" if words else "no reply",
            )
        )

    for pattern in expect.must_not_match:
        found = re.search(pattern, transcript.assistant_text, re.IGNORECASE | re.MULTILINE)
        checks.append(
            CheckResult(
                f"must_not_match:{pattern}",
                found is None,
                f"matched {found.group(0)!r}" if found else "",
            )
        )
    return checks
