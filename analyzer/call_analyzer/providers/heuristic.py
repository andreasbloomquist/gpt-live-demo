"""A deterministic, offline "judge" built from metrics, tool results, and small phrase lexicons.

It exists so the whole pipeline (ingest -> analyze -> UI) works with zero API keys, in CI, and in
demos, and it doubles as a sanity baseline for the LLM judge. It is deliberately simple and says
so: every rationale starts with "Heuristic:", and ``analyzer.provider`` is ``"heuristic"`` so the
UI can label it. What it can't do is understand meaning: it won't notice a made-up fact, sarcasm,
or a polite caller who leaves unsatisfied. That's what the LLM judge is for.

How it reads, top to bottom:

1. :class:`HeuristicProvider` is the whole public surface; ``assess`` shows the pipeline.
2. **Lexicons**: the phrase patterns everything else matches against.
3. **Signals**: one pass over the record collects every fact the judgements need.
4. **Judgements**: intent, outcome, one scorer per rubric dimension, flags, sentiment.
5. **Summary**: a one- or two-sentence story of the call.
6. **Helpers**: quoting, pattern hits, and spoken-time formatting.

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

# Tool names as the agent registers them (see the agent's tool registry).
RESTAURANT_TOOL = "check_restaurant_availability"
WEB_SEARCH_TOOL = "web_search"

# Longest evidence quote, and how much context to keep before a match when a sentence is longer.
_MAX_QUOTE_CHARS = 160
_QUOTE_LEAD_CHARS = 40
# A caller turn that shares this much of its vocabulary (Jaccard overlap) with an earlier one,
# and has at least this many words, counts as the caller repeating themselves.
_REPEAT_SIMILARITY = 0.6
_MIN_REPEAT_WORDS = 3
# Display limits for text lifted from the call.
_MAX_INTENT_WORDS = 10
_MAX_QUERY_CHARS = 80
# Efficiency: (max average agent words per turn, score), first match wins; wordier scores lower.
_EFFICIENCY_BANDS = ((25, 5), (35, 4), (50, 3))
_WORDIEST_EFFICIENCY = 2
_MONOLOGUE_WORDS = 60  # one agent turn this long costs a point on its own
_CITE_LONG_TURN_WORDS = 35  # cite the longest agent turn as evidence past this length
# Caller sentiment: per-phrase contributions, before clamping to [-1, 1].
_GRATITUDE_WEIGHT = 0.5
_FRUSTRATION_WEIGHT = -0.6
_ESCALATION_WEIGHT = -0.4

_Hit = tuple[Turn, re.Match[str]]
# (tool call, parsed arguments, parsed output) for one availability check.
_Check = tuple[ToolCall, dict[str, object], dict[str, object]]


# --- Provider -----------------------------------------------------------------------------------


class HeuristicProvider:
    """Offline, deterministic assessment. See the module docstring for what it can't do."""

    name: ProviderName = "heuristic"
    model: str | None = None

    async def assess(self, record: CallRecord, metrics: Metrics, rubric: Rubric) -> AssessmentDraft:
        signals = _signals(record)
        status, reason = _outcome(signals)
        return AssessmentDraft(
            summary=_summary(signals, metrics),
            caller_intent=_intent(signals),
            outcome=OutcomeDraft(status=status, reason=reason),
            scores=_scores(signals, metrics, status),
            flags=_flags(signals),
            sentiment=_sentiment(signals),
        )

    async def aclose(self) -> None:
        return None


# --- Lexicons -----------------------------------------------------------------------------------


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
RESULT = _lexicon(r"open", r"available", r"taken", r"closed", r"directly", r"can't check")
NEXT_STEP = _lexicon(r"call (?:them|the restaurant|\w+ directly)", r"website", r"app", r"I can")
_SAYS_REPEATING = re.compile(r"\b(?:already|again|twice)\b", re.IGNORECASE)


# --- Signals ------------------------------------------------------------------------------------


@dataclass
class _Signals:
    """Everything the judgements look at, computed once per call."""

    user: list[Turn]  # caller turns with words in them
    agent: list[Turn]  # agent turns with words in them
    checks: list[_Check]  # availability checks, in call order
    searches: list[ToolCall]
    lookups: list[ToolCall]  # successful checks and searches, in call order
    frustration: list[_Hit]
    escalation: list[_Hit]
    repeats: list[Turn]
    positive_close: _Hit | None  # gratitude in one of the caller's last two turns
    tool_errors: int
    failed_last_check: bool
    booking_claims: list[_Hit]
    sensitive_requests: list[_Hit]
    no_booking_disclosure: _Hit | None  # the agent saying nothing was booked
    unchecked_availability_claim: _Hit | None  # availability stated with no check at all


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
        lookups=[
            c
            for c in record.tool_calls
            if c.name in (RESTAURANT_TOOL, WEB_SEARCH_TOOL) and not c.is_error
        ],
        frustration=_all_hits(user, FRUSTRATION),
        escalation=_all_hits(user, ESCALATION),
        repeats=_repeated_turns(user),
        positive_close=_first_hit(list(reversed(closing)), GRATITUDE),
        tool_errors=sum(1 for c in record.tool_calls if c.is_error),
        failed_last_check=bool(checks) and checks[-1][0].is_error,
        booking_claims=_all_hits(agent, BOOKING_CLAIM),
        sensitive_requests=_all_hits(agent, SENSITIVE_REQUEST),
        no_booking_disclosure=_first_hit(agent, NO_BOOKING_DISCLOSURE),
        unchecked_availability_claim=None if checks else _first_hit(agent, AVAILABILITY_CLAIM),
    )


def _repeated_turns(user: list[Turn]) -> list[Turn]:
    """Caller turns that restate an earlier one (word overlap), or say they're repeating."""
    repeats: list[Turn] = []
    seen: list[set[str]] = []
    for turn in user:
        words = _words(turn.text)
        similar = any(
            len(words & prev) / len(words | prev) >= _REPEAT_SIMILARITY for prev in seen if words
        )
        says_so = _SAYS_REPEATING.search(turn.text)
        if (len(words) >= _MIN_REPEAT_WORDS and similar) or (
            says_so and FRUSTRATION.search(turn.text)
        ):
            repeats.append(turn)
        if words:
            seen.append(words)
    return repeats


def _found_a_table(s: _Signals) -> bool:
    """True if any check returned open times, i.e. the moment a "booked?" misunderstanding can
    happen and the agent should say nothing was booked."""
    return any(
        not call.is_error and out.get("status") in ("available", "alternatives")
        for call, _, out in s.checks
    )


# --- Judgements: intent and outcome -------------------------------------------------------------


def _intent(s: _Signals) -> str:
    if s.checks:
        args = s.checks[0][1]
        party, restaurant = _field(args, "party_size"), _field(args, "restaurant")
        date, time = _field(args, "date"), _field(args, "time")
        when = ", ".join(
            part for part in (date and _spoken_date(date), time and _spoken_time(time)) if part
        )
        return f"Table{f' for {party}' if party else ''} at {restaurant or 'a restaurant'}" + (
            f" ({when})" if when else ""
        )
    if s.searches:
        query = _search_query(s.searches[0])
        if query:
            return f"Information: {query[:_MAX_QUERY_CHARS]}"
    if s.user:
        words = s.user[0].text.split()
        ellipsis = "..." if len(words) > _MAX_INTENT_WORDS else ""
        return " ".join(words[:_MAX_INTENT_WORDS]) + ellipsis
    return "Unknown (caller never spoke)"


def _outcome(s: _Signals) -> tuple[OutcomeStatus, str]:
    if not s.user:
        return "not_applicable", "The caller never spoke."
    successful = [(c, out) for c, _, out in s.checks if not c.is_error and out]
    if s.failed_last_check and not successful:
        return "unresolved", "Every availability check failed, so the caller got no answer."
    if successful:
        return _check_outcome(s, successful[-1][1])
    if s.searches:
        if all(c.is_error for c in s.searches):
            return "unresolved", "The web search failed, so the question went unanswered."
        if s.positive_close:
            return "resolved", "Answered from a web search; the caller was satisfied."
        return "partially_resolved", "A web search was made; the caller's reaction was unclear."
    if s.frustration or s.escalation:
        return "unresolved", "No lookup was made and the caller was frustrated."
    if s.positive_close:
        return "resolved", "The caller ended the call satisfied."
    return "partially_resolved", "No lookup was made; outcome unclear from keywords."


def _check_outcome(s: _Signals, out: dict[str, object]) -> tuple[OutcomeStatus, str]:
    """Outcome from the last successful availability check's output."""
    status, note = out.get("status"), str(out.get("note") or "")
    if status == "available":
        return "resolved", "The requested time was open; the caller was told to finish booking."
    if status == "alternatives":
        if s.positive_close:
            return "resolved", "Requested time taken; the caller accepted an alternative."
        return "partially_resolved", "Alternatives were offered but no choice was confirmed."
    if "directly with the restaurant" in note:
        return (
            "partially_resolved",
            "Party too large to book online; caller sent to the restaurant.",
        )
    return "unresolved", f"No table was available ({status})."


# --- Judgements: one scorer per rubric dimension ------------------------------------------------


def _scores(s: _Signals, metrics: Metrics, outcome: OutcomeStatus) -> ScoresDraft:
    answer_evidence = _answer_evidence(s)
    return ScoresDraft(
        customer_satisfaction=_score_satisfaction(s),
        customer_frustration=_score_frustration(s),
        resolution=_score_resolution(s, outcome, answer_evidence),
        agent_helpfulness=_score_helpfulness(s, outcome, answer_evidence),
        accuracy_groundedness=_score_groundedness(s, answer_evidence),
        conversation_flow=_score_flow(s),
        efficiency=_score_efficiency(s, metrics),
        tone_empathy=_score_tone(s),
        policy_adherence=_score_policy(s, answer_evidence),
    )


def _answer_evidence(s: _Signals) -> list[EvidenceDraft]:
    """Where the agent relayed its last lookup's result, else where it said nothing was booked.
    Several dimensions cite this as "the answer the caller got"."""
    answer = _answer_to(s.lookups[-1], s.agent) if s.lookups else None
    if answer is not None:
        return [_evidence(answer, RESULT.search(answer.text))]
    if s.no_booking_disclosure:
        return [_evidence(*s.no_booking_disclosure)]
    return []


def _score_satisfaction(s: _Signals) -> DimensionDraft:
    score = 3 + (2 if s.positive_close else 0) - min(2, len(s.frustration))
    if s.escalation:
        score -= 1
    evidence = [_evidence(*hit) for hit in s.frustration[:2]]
    if s.positive_close:
        evidence.append(_evidence(*s.positive_close))
    closing = "positive" if s.positive_close else "neutral"
    return _dim(score, f"closing {closing}, {len(s.frustration)} frustrated turn(s).", evidence)


def _score_frustration(s: _Signals) -> DimensionDraft:
    """Inverted dimension: 1 = no frustration, 5 = severe."""
    frustrated_turns = {t.id for t, _ in s.frustration}
    score = 1 + len(frustrated_turns) + (1 if s.escalation else 0)
    evidence = [_evidence(*hit) for hit in (s.frustration + s.escalation)[:3]]
    rationale = f"{len(frustrated_turns)} caller turn(s) with frustration phrases" + (
        ", asked for a human." if s.escalation else "."
    )
    return _dim(score, rationale, evidence)


_RESOLUTION_BY_OUTCOME: dict[OutcomeStatus, int] = {
    "resolved": 5,
    "partially_resolved": 3,
    "unresolved": 1,
    "not_applicable": 3,
}


def _score_resolution(
    s: _Signals, outcome: OutcomeStatus, answer_evidence: list[EvidenceDraft]
) -> DimensionDraft:
    score = _RESOLUTION_BY_OUTCOME[outcome]
    if outcome == "resolved" and s.tool_errors:
        score -= 1
    return _dim(score, f"outcome classified as {outcome}.", answer_evidence)


def _score_helpfulness(
    s: _Signals, outcome: OutcomeStatus, answer_evidence: list[EvidenceDraft]
) -> DimensionDraft:
    # The last next-step offer is the one the caller left with.
    next_step = _first_hit(s.agent[::-1], NEXT_STEP)
    if outcome == "resolved":
        score = 5 if not (s.tool_errors or s.frustration) else 4
    elif outcome == "partially_resolved":
        score = 4 if next_step else 3
    else:
        score = 2 if next_step else 1
    evidence = [_evidence(*next_step)] if next_step else answer_evidence
    offered = "a" if next_step else "no"
    return _dim(score, f"outcome {outcome}; {offered} next step offered.", evidence)


def _score_groundedness(s: _Signals, answer_evidence: list[EvidenceDraft]) -> DimensionDraft:
    # Groundedness can't really be checked with keywords; stay near the middle and say why.
    if s.booking_claims:
        return _dim(
            1,
            "the agent claimed a booking, which no tool can make.",
            [_evidence(*s.booking_claims[0])],
        )
    if s.unchecked_availability_claim:
        return _dim(
            2,
            "availability was stated without any availability check.",
            [_evidence(*s.unchecked_availability_claim)],
        )
    if s.lookups:
        return _dim(
            4,
            "answers followed successful lookups; exact claims were not verified.",
            answer_evidence,
        )
    return _dim(3, "no lookups to ground answers in; claims were not verified.", [])


def _score_flow(s: _Signals) -> DimensionDraft:
    interrupted = [t for t in s.agent if t.interrupted]
    score = 5 - min(2, len(interrupted)) - (1 if s.repeats else 0)
    # An interrupted turn is cut off at the end, so quote where the caller cut in.
    evidence = [_evidence(t, last=True) for t in interrupted[:2]]
    repeated = "a" if s.repeats else "no"
    return _dim(
        score,
        f"{len(interrupted)} interrupted agent turn(s), {repeated} repeated caller request.",
        evidence,
    )


def _score_efficiency(s: _Signals, metrics: Metrics) -> DimensionDraft:
    avg = metrics.avg_agent_words_per_turn
    score = next((band for limit, band in _EFFICIENCY_BANDS if avg <= limit), _WORDIEST_EFFICIENCY)
    longest = max(s.agent, key=lambda t: word_count(t.text), default=None)
    longest_words = word_count(longest.text) if longest is not None else 0
    if longest_words > _MONOLOGUE_WORDS:
        score -= 1
    if s.repeats:
        score -= 1
    evidence = (
        [_evidence(longest, last=True)]
        if longest is not None and longest_words > _CITE_LONG_TURN_WORDS
        else []
    )
    return _dim(
        score,
        f"agent averaged {avg:g} words per turn over {metrics.agent_turns} turns.",
        evidence,
    )


def _score_tone(s: _Signals) -> DimensionDraft:
    empathy = _first_hit(s.agent, EMPATHY)
    evidence = [_evidence(*empathy)] if empathy else []
    if s.frustration and empathy:
        return _dim(4, "the agent acknowledged the caller's frustration.", evidence)
    if s.frustration:
        return _dim(2, "the caller was frustrated and the agent never acknowledged it.", evidence)
    return _dim(4, "no rudeness or frustration detected (warmth can't be heard in text).", evidence)


def _score_policy(s: _Signals, answer_evidence: list[EvidenceDraft]) -> DimensionDraft:
    violations = s.booking_claims + s.sensitive_requests
    if violations:
        return _dim(
            1,
            "the agent claimed a booking or asked for sensitive data.",
            [_evidence(*v) for v in violations[:2]],
        )
    if _found_a_table(s) and not s.no_booking_disclosure:
        return _dim(4, "a table was found but the agent never said nothing was booked.", [])
    return _dim(5, "no booking claims or sensitive-data requests detected.", answer_evidence)


def _dim(score: int, rationale: str, evidence: list[EvidenceDraft]) -> DimensionDraft:
    """A dimension draft with the score kept on the 1-5 scale and the rationale labeled."""
    return DimensionDraft(
        score=max(1, min(5, score)), rationale=f"Heuristic: {rationale}", evidence=evidence
    )


# --- Judgements: flags and sentiment ------------------------------------------------------------


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
    if s.unchecked_availability_claim:
        flags.append(
            FlagDraft(
                type="hallucination_risk",
                turn_id=s.unchecked_availability_claim[0].id,
                detail="Availability stated without an availability check.",
            )
        )
    return flags


def _sentiment(s: _Signals) -> list[SentimentDraft]:
    """One point per caller turn: gratitude pulls up, frustration and escalation pull down."""
    points = []
    for turn in s.user:
        value = _GRATITUDE_WEIGHT * len(GRATITUDE.findall(turn.text)) + (
            _FRUSTRATION_WEIGHT * len(FRUSTRATION.findall(turn.text))
        )
        if ESCALATION.search(turn.text):
            value += _ESCALATION_WEIGHT
        points.append(SentimentDraft(turn_id=turn.id, value=round(max(-1.0, min(1.0, value)), 2)))
    return points


# --- Summary ------------------------------------------------------------------------------------


def _summary(s: _Signals, metrics: Metrics) -> str:
    """One or two plain sentences about what happened. It deliberately doesn't restate
    ``caller_intent`` (the UI shows that as the title) or list every issue (flags do that)."""
    if not s.user:
        return "The caller never spoke, so there was nothing to resolve."
    if s.checks:
        story = _check_story(s)
    elif s.searches:
        story = _search_story(s)
    elif s.positive_close:
        story = "The agent answered without a lookup and the caller ended on a positive note"
    else:
        story = "No lookup was made during the call"
    mood = _mood(s, metrics)
    return f"{story}." + (f" {mood}." if mood else "")


def _check_story(s: _Signals) -> str:
    """What the availability checks found and what the agent did with it. Every variant starts
    with the restaurant's name so a prefix never has to re-case it."""
    successful = [(args, out) for call, args, out in s.checks if not call.is_error and out]
    if not successful:
        return _unreadable_check_story(s)
    args, out = successful[-1]
    restaurant = _field(out, "restaurant") or _field(args, "restaurant") or "The restaurant"
    requested_raw = _field(out, "requested_time") or _field(args, "time")
    requested = _clock(requested_raw) if requested_raw else "the requested time"
    party = _field(out, "party_size") or _field(args, "party_size")
    date = _field(out, "date") or _field(args, "date")
    day = _field(out, "weekday") or (date and _spoken_date(date))
    on_day = f" on {day}" if day else ""
    # Tool output is data from the record, not a contract: tolerate a missing or odd list.
    raw_times = out.get("nearest_available_times")
    times = [t for t in raw_times if isinstance(t, str)] if isinstance(raw_times, list) else []
    status, note = out.get("status"), str(out.get("note") or "")

    if status == "available":
        story = f"{restaurant} had {requested} open{on_day}{f' for {party}' if party else ''}; "
        story += (
            "the agent made clear nothing was booked and pointed the caller to the restaurant "
            "or the app"
            if s.no_booking_disclosure
            else "the agent relayed it"
        )
    elif status == "alternatives":
        offered = _join([_clock(t) for t in times])
        story = f"{restaurant} was full at {requested}, so the agent offered {offered}"
        chosen = _chosen_time(times, s.user)
        if chosen:
            story += f", and the caller took {_clock(chosen)}"
        elif s.positive_close:
            story += ", and the caller accepted one"
        else:
            story += ", but the caller didn't pick one"
    elif "directly with the restaurant" in note:
        size = f"parties of {party}" if party else "a party that size"
        story = (
            f"{restaurant} doesn't take {size} online, so the agent sent the caller "
            "to the restaurant directly"
        )
    elif status == "closed":
        story = f"{restaurant} is closed{on_day}, so there was nothing to offer"
    else:
        story = f"{restaurant} had nothing open near {requested}{on_day}"
    if any(call.is_error for call, _, _ in s.checks):
        story = f"After a failed first check, {story}"
    return story


def _unreadable_check_story(s: _Signals) -> str:
    """The story when no availability check produced a usable result."""
    restaurant = _field(s.checks[-1][1], "restaurant") or "the restaurant"
    if all(call.is_error for call, _, _ in s.checks):
        return f"Every availability check for {restaurant} failed, so the caller got no answer"
    # A check that didn't error but whose output isn't a JSON object: we can't tell what it
    # found, so say that rather than calling it a failure.
    return f"The agent checked availability at {restaurant}; the result couldn't be read"


def _search_story(s: _Signals) -> str:
    query = _search_query(s.searches[-1])
    looked_up = f'"{query[:_MAX_QUERY_CHARS]}"' if query else "the question"
    if all(c.is_error for c in s.searches):
        return f"The agent tried to look up {looked_up}, but the search failed"
    closing = "; the caller thanked it" if s.positive_close else ""
    return f"The agent looked up {looked_up} on the web and relayed the answer{closing}"


def _mood(s: _Signals, metrics: Metrics) -> str | None:
    """One short clause about how the caller felt, only when it went wrong."""
    if s.escalation:
        return "The caller asked to speak to a human"
    if not s.frustration:
        return None
    if metrics.interruptions:
        n = metrics.interruptions
        return f"The caller grew frustrated after {n} interruption{'s' if n > 1 else ''}"
    return "The caller grew frustrated along the way"


def _chosen_time(times: list[str], user: list[Turn]) -> str | None:
    """The offered time the caller named last, if any."""
    for turn in reversed(user):
        text = turn.text.lower()
        for hhmm in times:
            if any(re.search(rf"\b{re.escape(v)}\b", text) for v in _spoken_variants(hhmm)):
                return hhmm
    return None


def _join(items: list[str]) -> str:
    """``["a", "b", "c"]`` -> ``"a, b or c"``."""
    if len(items) <= 1:
        return items[0] if items else "other times"
    return f"{', '.join(items[:-1])} or {items[-1]}"


# --- Helpers: turns, patterns, quotes, tool data ------------------------------------------------

_SENTENCE = re.compile(r"[^.!?]+[.!?]*")


def _first_hit(turns: list[Turn], pattern: re.Pattern[str]) -> _Hit | None:
    for turn in turns:
        match = pattern.search(turn.text)
        if match:
            return turn, match
    return None


def _all_hits(turns: list[Turn], pattern: re.Pattern[str]) -> list[_Hit]:
    return [(t, m) for t in turns if (m := pattern.search(t.text))]


def _answer_to(call: ToolCall, agent: list[Turn]) -> Turn | None:
    """The first agent turn after a tool call, i.e. where the result was relayed (needs times)."""
    if call.created_at is None:
        return None
    return next((t for t in agent if t.started_at and t.started_at > call.created_at), None)


def _evidence(
    turn: Turn, match: re.Match[str] | None = None, *, last: bool = False
) -> EvidenceDraft:
    return EvidenceDraft(turn_id=turn.id, quote=_quote(turn.text, match, last=last))


def _quote(text: str, match: re.Match[str] | None = None, *, last: bool = False) -> str:
    """An exact substring of ``text`` worth citing.

    Short turns are quoted whole. Otherwise: the sentence containing ``match`` (else the first,
    or with ``last`` the final sentence, e.g. where an interrupted turn was cut off), trimmed to
    a window around the match at word boundaries.
    """
    if len(text.strip()) <= _MAX_QUOTE_CHARS:
        return text.strip()
    sentences = [m for m in _SENTENCE.finditer(text) if m.group().strip()]
    if not sentences:  # nothing but punctuation and spaces: any window is as good as another
        return text.strip()[:_MAX_QUOTE_CHARS]
    chosen = sentences[-1] if last else sentences[0]
    if match is not None:
        chosen = next((m for m in sentences if m.start() <= match.start() < m.end()), chosen)
    start, end = chosen.span()
    if end - start > _MAX_QUOTE_CHARS:
        if match is not None:
            start = max(start, match.start() - _QUOTE_LEAD_CHARS)
        elif last:
            start = end - _MAX_QUOTE_CHARS
        end = min(end, start + _MAX_QUOTE_CHARS)
        # Snap inwards to whole words so the quote doesn't start or end mid-word.
        if start > chosen.start() and (space := text.find(" ", start, end)) != -1:
            start = space + 1
        if end < chosen.end() and (space := text.rfind(" ", start, end)) > start:
            end = space
    return text[start:end].strip()


def _words(text: str) -> set[str]:
    """Distinct lower-case words longer than two letters (drops "a", "to", "at"...)."""
    return {w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 2}


def _json_object(text: str | None) -> dict[str, object]:
    """Tool arguments/output parsed as a JSON object; ``{}`` for anything else."""
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _field(data: dict[str, object], key: str) -> str | None:
    """A tool argument/output field as display text, or None if absent (never ``"None"``)."""
    value = data.get(key)
    return None if value is None or value == "" else str(value)


def _search_query(call: ToolCall) -> str:
    return str(_json_object(call.arguments).get("query", "")).strip()


# --- Helpers: spoken times and dates ------------------------------------------------------------

# Index = hour on a 12-hour clock (hour % 12).
_HOUR_WORDS = (
    "twelve", "one", "two", "three", "four", "five",
    "six", "seven", "eight", "nine", "ten", "eleven",
)  # fmt: skip
_MINUTE_WORDS = {
    0: ["o'clock", ""],
    15: ["fifteen"],
    30: ["thirty"],
    45: ["forty-five", "forty five"],
}


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


def _clock(hhmm: object) -> str:
    """``"19:15"`` -> ``"7:15"`` (dinner context; the meridiem is obvious)."""
    return _spoken_time(hhmm).split(" ")[0]


def _spoken_variants(hhmm: str) -> list[str]:
    """How a caller might say a time: ``"19:15"`` -> ``["7:15", "seven fifteen",
    "quarter past seven"]``. Only quarter hours, which is what the reservation tool returns."""
    try:
        t = dt.datetime.strptime(hhmm, "%H:%M")
    except ValueError:
        return []
    hour, next_hour = _HOUR_WORDS[t.hour % 12], _HOUR_WORDS[(t.hour + 1) % 12]
    variants = [_clock(hhmm)]
    variants += [f"{hour} {m}".strip() for m in _MINUTE_WORDS.get(t.minute, [])]
    variants += {
        15: [f"quarter past {hour}"],
        30: [f"half past {hour}"],
        45: [f"quarter to {next_hour}"],
    }.get(t.minute, [])
    return [v for v in variants if v != hour]  # a bare "seven" is too ambiguous
