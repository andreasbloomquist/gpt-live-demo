"""A deterministic, offline "judge" built from metrics, tool results, and small phrase lexicons.

It exists so the whole pipeline (ingest -> analyze -> UI) works with zero API keys, in CI, and in
demos, and it doubles as a sanity baseline for the LLM judge. It is deliberately simple and says
so: every rationale starts with "Heuristic:", and ``analyzer.provider`` is ``"heuristic"`` so the
UI can label it. What it can't do is understand meaning: it won't notice a made-up fact, sarcasm,
or a polite caller who leaves unsatisfied. That's what the LLM judge is for.

All evidence quotes are cut from the real turn text, so they always pass validation.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass

from ..metrics import word_count
from ..models import (
    AssessmentDraft,
    CallRecord,
    DimensionDraft,
    EvidenceDraft,
    FlagDraft,
    Metrics,
    OutcomeDraft,
    OutcomeStatus,
    ProviderName,
    ScoresDraft,
    SentimentDraft,
    ToolCall,
    Turn,
)
from ..rubric import Rubric

RESTAURANT_TOOL = "check_restaurant_availability"
WEB_SEARCH_TOOL = "web_search"


def _lexicon(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(rf"\b(?:{p})\b" for p in patterns), re.IGNORECASE)


# Caller-side lexicons. Short and conservative: a false "frustrated" is worse than a miss.
FRUSTRATION = _lexicon(
    r"seriously",
    r"already (?:said|told you|asked)",
    r"(?:said|told you) (?:that|this) (?:already|twice)",
    r"finally",
    r"whatever",
    r"ridiculous",
    r"useless",
    r"not listening",
    r"forget it",
    r"frustrat\w*",
    r"annoy\w*",
    r"ugh",
    r"do it myself",
    r"waste of time",
)
ESCALATION = _lexicon(
    r"(?:speak|talk) to (?:a|an|the) (?:human|person|manager|agent|representative)",
    r"(?:real|actual) (?:person|human)",
)
GRATITUDE = _lexicon(
    r"thanks?",
    r"thank you",
    r"perfect",
    r"great",
    r"awesome",
    r"appreciate(?: it)?",
    r"wonderful",
    r"lovely",
    r"nice",
)
# Agent-side lexicons.
EMPATHY = _lexicon(r"sorry", r"apologi[sz]e", r"I understand")
NO_BOOKING_DISCLOSURE = _lexicon(
    r"(?:haven't|have not|didn't|did not) (?:booked|reserved)",
    r"nothing(?:'s| is| has been) booked",
    r"only checked",
    r"not booked",
)
BOOKING_CLAIM = _lexicon(
    r"I(?:'ve| have)? (?:booked|reserved)",
    r"you(?:'re| are) (?:booked|confirmed)",
    r"(?:table|reservation|booking) (?:is|has been) (?:booked|reserved|confirmed|held)",
    r"confirmed your (?:table|reservation|booking)",
)
SENSITIVE_REQUEST = _lexicon(
    r"card number", r"credit card", r"CVV", r"security code", r"password", r"social security"
)
AVAILABILITY_CLAIM = _lexicon(r"is (?:open|available)", r"have a table", r"(?:is|are) free")
NEXT_STEP = _lexicon(r"call (?:them|the restaurant|\w+ directly)", r"website", r"app", r"I can")

_SENTENCE = re.compile(r"[^.!?]+[.!?]*")
_MAX_QUOTE_CHARS = 160


def _quote(text: str, match: re.Match[str] | None = None) -> str:
    """An exact substring of ``text``: the sentence containing ``match`` (or the first one)."""
    for sentence in _SENTENCE.finditer(text):
        if match is None or sentence.start() <= match.start() < sentence.end():
            quote = sentence.group().strip()
            break
    else:
        quote = text.strip()
    if len(quote) > _MAX_QUOTE_CHARS:
        quote = quote[:_MAX_QUOTE_CHARS].rsplit(" ", 1)[0]
    return quote


def _evidence(turn: Turn, match: re.Match[str] | None = None) -> EvidenceDraft:
    return EvidenceDraft(turn_id=turn.id, quote=_quote(turn.text, match))


def _first_hit(turns: list[Turn], pattern: re.Pattern[str]) -> tuple[Turn, re.Match[str]] | None:
    for turn in turns:
        match = pattern.search(turn.text)
        if match:
            return turn, match
    return None


def _all_hits(turns: list[Turn], pattern: re.Pattern[str]) -> list[tuple[Turn, re.Match[str]]]:
    return [(t, m) for t in turns if (m := pattern.search(t.text))]


def _clamp(score: int) -> int:
    return max(1, min(5, score))


def _dim(score: int, rationale: str, evidence: list[EvidenceDraft]) -> DimensionDraft:
    return DimensionDraft(score=_clamp(score), rationale=f"Heuristic: {rationale}", evidence=evidence)


def _json_object(text: str | None) -> dict[str, object]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 2}


def _spoken_time(hhmm: object) -> str:
    """``"19:30"`` -> ``"7:30 PM"`` (built by hand: ``%-I`` isn't portable to Windows)."""
    try:
        t = dt.datetime.strptime(str(hhmm), "%H:%M")
    except ValueError:
        return str(hhmm)
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _spoken_date(iso: object) -> str:
    """``"2026-09-26"`` -> ``"Sat Sep 26"``."""
    try:
        d = dt.date.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    return f"{d:%a %b} {d.day}"


@dataclass
class _Signals:
    """Everything the scorers look at, computed once."""

    user: list[Turn]
    agent: list[Turn]
    checks: list[tuple[ToolCall, dict[str, object], dict[str, object]]]  # (call, args, output)
    searches: list[ToolCall]
    frustration: list[tuple[Turn, re.Match[str]]]
    escalation: list[tuple[Turn, re.Match[str]]]
    repeats: list[Turn]
    positive_close: tuple[Turn, re.Match[str]] | None
    tool_errors: int
    failed_last_check: bool
    booking_claims: list[tuple[Turn, re.Match[str]]]
    sensitive_requests: list[tuple[Turn, re.Match[str]]]


def _repeated_turns(user: list[Turn]) -> list[Turn]:
    """Caller turns that restate an earlier one (word overlap), or say they're repeating."""
    repeats: list[Turn] = []
    seen: list[set[str]] = []
    for turn in user:
        words = _words(turn.text)
        similar = any(len(words & prev) / len(words | prev) >= 0.6 for prev in seen if words)
        says_so = re.search(r"\b(?:already|again|twice)\b", turn.text, re.IGNORECASE)
        if (len(words) >= 3 and similar) or (says_so and FRUSTRATION.search(turn.text)):
            repeats.append(turn)
        if words:
            seen.append(words)
    return repeats


def _signals(record: CallRecord) -> _Signals:
    user = [t for t in record.turns if t.role == "user" and t.text.strip()]
    agent = [t for t in record.turns if t.role == "assistant" and t.text.strip()]
    checks = [
        (c, _json_object(c.arguments), _json_object(c.output))
        for c in record.tool_calls
        if c.name == RESTAURANT_TOOL
    ]
    closing = user[-2:]
    return _Signals(
        user=user,
        agent=agent,
        checks=checks,
        searches=[c for c in record.tool_calls if c.name == WEB_SEARCH_TOOL],
        frustration=_all_hits(user, FRUSTRATION),
        escalation=_all_hits(user, ESCALATION),
        repeats=_repeated_turns(user),
        positive_close=_first_hit(list(reversed(closing)), GRATITUDE),
        tool_errors=sum(1 for c in record.tool_calls if c.is_error),
        failed_last_check=bool(checks) and checks[-1][0].is_error,
        booking_claims=_all_hits(agent, BOOKING_CLAIM),
        sensitive_requests=_all_hits(agent, SENSITIVE_REQUEST),
    )


def _found_a_table(s: _Signals) -> bool:
    """True if any check returned open times, i.e. the moment a "booked?" misunderstanding can
    happen and the agent should say nothing was booked."""
    return any(
        not call.is_error and out.get("status") in ("available", "alternatives")
        for call, _, out in s.checks
    )


def _intent(s: _Signals) -> str:
    if s.checks:
        args = s.checks[0][1]
        party, restaurant = args.get("party_size", "?"), args.get("restaurant", "a restaurant")
        when = f"{_spoken_date(args.get('date'))}, {_spoken_time(args.get('time'))}"
        return f"Table for {party} at {restaurant} ({when})"
    if s.searches:
        query = str(_json_object(s.searches[0].arguments).get("query", "")).strip()
        if query:
            return f"Information: {query[:80]}"
    if s.user:
        words = s.user[0].text.split()
        return " ".join(words[:10]) + ("..." if len(words) > 10 else "")
    return "Unknown (caller never spoke)"


def _outcome(s: _Signals) -> tuple[OutcomeStatus, str]:
    if not s.user:
        return "not_applicable", "The caller never spoke."
    successful = [(c, out) for c, _, out in s.checks if not c.is_error and out]
    if s.failed_last_check and not successful:
        return "unresolved", "Every availability check failed, so the caller got no answer."
    if successful:
        out = successful[-1][1]
        status, note = out.get("status"), str(out.get("note") or "")
        if status == "available":
            return "resolved", "The requested time was open; the caller was told to finish booking."
        if status == "alternatives":
            if s.positive_close:
                return "resolved", "The requested time was taken; the caller accepted an alternative."
            return "partially_resolved", "Alternatives were offered but no choice was confirmed."
        if "directly with the restaurant" in note:
            return "partially_resolved", "Party too large to check online; caller sent to the restaurant."
        return "unresolved", f"No table was available ({status})."
    if s.searches:
        if all(c.is_error for c in s.searches):
            return "unresolved", "The web search failed, so the question went unanswered."
        if s.positive_close:
            return "resolved", "The question was answered from a web search and the caller was satisfied."
        return "partially_resolved", "A web search was made; the caller's reaction was unclear."
    if s.frustration or s.escalation:
        return "unresolved", "No lookup was made and the caller was frustrated."
    if s.positive_close:
        return "resolved", "The caller ended the call satisfied."
    return "partially_resolved", "No lookup was made; the outcome can't be determined from keywords."


def _scores(s: _Signals, metrics: Metrics, outcome: OutcomeStatus) -> ScoresDraft:
    decisive = next((t for t in s.agent if NO_BOOKING_DISCLOSURE.search(t.text)), None)
    resolution_evidence = [_evidence(decisive)] if decisive else []

    resolution = {"resolved": 5, "partially_resolved": 3, "unresolved": 1, "not_applicable": 3}[
        outcome
    ]
    if outcome == "resolved" and s.tool_errors:
        resolution -= 1

    next_step = _first_hit(s.agent, NEXT_STEP)
    if outcome == "resolved":
        helpful = 5 if not (s.tool_errors or s.frustration) else 4
    elif outcome == "partially_resolved":
        helpful = 4 if next_step else 3
    else:
        helpful = 2 if next_step else 1
    helpful_evidence = [_evidence(*next_step)] if next_step else resolution_evidence

    satisfaction = 3 + (2 if s.positive_close else 0) - min(2, len(s.frustration))
    if s.escalation:
        satisfaction -= 1
    satisfaction_evidence = [_evidence(*hit) for hit in s.frustration[:2]]
    if s.positive_close:
        satisfaction_evidence.append(_evidence(*s.positive_close))

    frustrated_turns = {t.id for t, _ in s.frustration}
    frustration = 1 + len(frustrated_turns) + (1 if s.escalation else 0)
    frustration_evidence = [_evidence(*hit) for hit in (s.frustration + s.escalation)[:3]]

    # Groundedness can't really be checked with keywords; stay near the middle and say why.
    successful_lookups = [c for c in (*(c for c, _, _ in s.checks), *s.searches) if not c.is_error]
    unsupported = _first_hit(s.agent, AVAILABILITY_CLAIM) if not s.checks else None
    if s.booking_claims:
        grounded, grounded_why = 1, "the agent claimed a booking, which no tool can make."
        grounded_evidence = [_evidence(*s.booking_claims[0])]
    elif unsupported:
        grounded, grounded_why = 2, "availability was stated without any availability check."
        grounded_evidence = [_evidence(*unsupported)]
    elif successful_lookups:
        grounded = 4
        grounded_why = "answers followed successful lookups; exact claims were not verified."
        grounded_evidence = resolution_evidence
    else:
        grounded, grounded_why = 3, "no lookups to ground answers in; claims were not verified."
        grounded_evidence = []

    interrupted = [t for t in s.agent if t.interrupted]
    flow = 5 - min(2, len(interrupted)) - (1 if s.repeats else 0)
    flow_evidence = [_evidence(t) for t in interrupted[:2]]

    longest = max(s.agent, key=lambda t: word_count(t.text), default=None)
    avg = metrics.avg_agent_words_per_turn
    efficiency = 5 if avg <= 25 else 4 if avg <= 35 else 3 if avg <= 50 else 2
    if longest is not None and word_count(longest.text) > 60:
        efficiency -= 1
    if s.repeats:
        efficiency -= 1
    efficiency_evidence = (
        [_evidence(longest)] if longest is not None and word_count(longest.text) > 35 else []
    )

    empathy = _first_hit(s.agent, EMPATHY)
    if s.frustration and empathy:
        tone, tone_why = 4, "the agent acknowledged the caller's frustration."
    elif s.frustration:
        tone, tone_why = 2, "the caller was frustrated and the agent never acknowledged it."
    else:
        tone, tone_why = 4, "no rudeness or frustration detected (warmth can't be heard in text)."
    tone_evidence = [_evidence(*empathy)] if empathy else []

    violations = s.booking_claims + s.sensitive_requests
    if violations:
        policy, policy_why = 1, "the agent claimed a booking or asked for sensitive data."
        policy_evidence = [_evidence(*v) for v in violations[:2]]
    elif _found_a_table(s) and not decisive:
        policy, policy_why = 4, "a table was found but the agent never said nothing was booked."
        policy_evidence = []
    else:
        policy, policy_why = 5, "no booking claims or sensitive-data requests detected."
        policy_evidence = resolution_evidence

    return ScoresDraft(
        customer_satisfaction=_dim(
            satisfaction,
            f"closing {'positive' if s.positive_close else 'neutral'}, "
            f"{len(s.frustration)} frustrated turn(s).",
            satisfaction_evidence,
        ),
        customer_frustration=_dim(
            frustration,
            f"{len(frustrated_turns)} caller turn(s) with frustration phrases"
            + (", asked for a human." if s.escalation else "."),
            frustration_evidence,
        ),
        resolution=_dim(resolution, f"outcome classified as {outcome}.", resolution_evidence),
        agent_helpfulness=_dim(
            helpful,
            f"outcome {outcome}; {'a' if next_step else 'no'} next step offered.",
            helpful_evidence,
        ),
        accuracy_groundedness=_dim(grounded, grounded_why, grounded_evidence),
        conversation_flow=_dim(
            flow,
            f"{len(interrupted)} interrupted agent turn(s), "
            f"{'a' if s.repeats else 'no'} repeated caller request.",
            flow_evidence,
        ),
        efficiency=_dim(
            efficiency,
            f"agent averaged {avg:g} words per turn over {metrics.agent_turns} turns.",
            efficiency_evidence,
        ),
        tone_empathy=_dim(tone, tone_why, tone_evidence),
        policy_adherence=_dim(policy, policy_why, policy_evidence),
    )


def _flags(s: _Signals) -> list[FlagDraft]:
    flags = [
        FlagDraft(type="escalation_needed", turn_id=t.id, detail="Caller asked for a human.")
        for t, _ in s.escalation
    ]
    flags += [
        FlagDraft(type="caller_repeated", turn_id=t.id, detail="Caller had to repeat themselves.")
        for t in s.repeats
    ]
    flags += [
        FlagDraft(type="policy_violation", turn_id=t.id, detail=f"Agent said: {_quote(t.text, m)}")
        for t, m in s.booking_claims + s.sensitive_requests
    ]
    if not s.checks and (hit := _first_hit(s.agent, AVAILABILITY_CLAIM)):
        flags.append(
            FlagDraft(
                type="hallucination_risk",
                turn_id=hit[0].id,
                detail="Availability stated without an availability check.",
            )
        )
    return flags


def _sentiment(s: _Signals) -> list[SentimentDraft]:
    points = []
    for turn in s.user:
        value = 0.5 * len(GRATITUDE.findall(turn.text)) - 0.6 * len(FRUSTRATION.findall(turn.text))
        if ESCALATION.search(turn.text):
            value -= 0.4
        points.append(SentimentDraft(turn_id=turn.id, value=round(max(-1.0, min(1.0, value)), 2)))
    return points


def _summary(s: _Signals, intent: str, outcome_reason: str, metrics: Metrics) -> str:
    issues = []
    if metrics.interruptions:
        issues.append(f"{metrics.interruptions} interruption(s)")
    if s.tool_errors:
        issues.append(f"{s.tool_errors} failed tool call(s)")
    if s.frustration:
        issues.append("caller frustration")
    if metrics.low_confidence_turns:
        issues.append(f"{metrics.low_confidence_turns} low-confidence transcript turn(s)")
    notable = f"Notable: {', '.join(issues)}." if issues else "No issues detected by keyword checks."
    return f"Caller intent: {intent}. {outcome_reason} {notable}"


class HeuristicProvider:
    """Offline, deterministic assessment. See the module docstring for what it can't do."""

    name: ProviderName = "heuristic"
    model: str | None = None

    async def assess(self, record: CallRecord, metrics: Metrics, rubric: Rubric) -> AssessmentDraft:
        signals = _signals(record)
        intent = _intent(signals)
        status, reason = _outcome(signals)
        return AssessmentDraft(
            summary=_summary(signals, intent, reason, metrics),
            caller_intent=intent,
            outcome=OutcomeDraft(status=status, reason=reason),
            scores=_scores(signals, metrics, status),
            flags=_flags(signals),
            sentiment=_sentiment(signals),
        )

    async def aclose(self) -> None:
        return None
