"""Wire contracts: the ``CallRecord`` the agent sends and the ``Analysis`` this service returns.

Everything that crosses a trust boundary is validated here, with explicit size limits, because
a call record is untrusted input: it carries caller speech and tool output (web content) that
later ends up in an LLM prompt. Timestamps must be timezone-aware and are normalized to UTC so
the API never has to guess what a naive ``2026-09-25T19:00:00`` meant.

The ``*Draft`` models at the bottom are the *provider* output schema (what the LLM is asked to
fill in). They are deliberately looser than :class:`Analysis`: range checks and evidence
verification happen in code (:mod:`call_analyzer.analysis`), so a slightly-off answer can be
repaired or trimmed instead of failing the whole analysis.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

# --- Limits (documented in README.md; the HTTP layer adds the 2 MiB body cap) ----------------
MAX_TURNS = 2000
MAX_TURN_CHARS = 20_000
MAX_TOOL_CALLS = 500
MAX_TOOL_ARGUMENT_CHARS = 20_000
MAX_TOOL_OUTPUT_CHARS = 50_000
MAX_USAGE_ENTRIES = 200
MAX_CALL_DURATION_S = 24 * 3600

# Call IDs appear in URLs and log lines, so keep them to a boring, URL-safe alphabet.
CALL_ID_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9._:@=+-]{0,199}$"
CallId = Annotated[str, StringConstraints(pattern=CALL_ID_PATTERN)]
ShortText = Annotated[str, StringConstraints(max_length=256)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]


def _to_utc(value: dt.datetime) -> dt.datetime:
    return value.astimezone(dt.timezone.utc)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class _Strict(BaseModel):
    # Unknown fields are rejected rather than silently dropped: contract drift between the agent
    # and the analyzer should fail loudly (the agent falls back to writing the record to disk).
    model_config = ConfigDict(extra="forbid", frozen=True)


# =============================================================================================
# CallRecord v1
# =============================================================================================


class PromptInfo(_Strict):
    """Which composed prompt the call ran with (fingerprints from the agent's PromptBundle)."""

    profile: ShortText
    fingerprint: ShortText
    version: ShortText
    voice: ShortText
    backend: ShortText


class ModelInfo(_Strict):
    voice_model: ShortText
    voice: ShortText
    backend_model: ShortText


class Turn(_Strict):
    id: Identifier
    role: Literal["user", "assistant"]
    # Empty text is legitimate: a turn can be interrupted before any words were transcribed.
    text: str = Field(max_length=MAX_TURN_CHARS)
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    interrupted: bool = False
    transcript_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ToolCall(_Strict):
    id: Identifier
    name: Identifier
    arguments: str = Field(max_length=MAX_TOOL_ARGUMENT_CHARS, description="JSON-encoded string")
    output: str | None = Field(default=None, max_length=MAX_TOOL_OUTPUT_CHARS)
    is_error: bool = False
    created_at: UtcDatetime | None = None


class CallRecord(_Strict):
    """One finished call, as posted by the agent to ``POST /v1/calls``."""

    schema_version: Literal[1]
    call_id: CallId
    room: ShortText
    agent_name: ShortText
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_s: float = Field(ge=0, le=MAX_CALL_DURATION_S)
    end_reason: ShortText | None = None
    prompt: PromptInfo
    models: ModelInfo
    turns: list[Turn] = Field(max_length=MAX_TURNS)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=MAX_TOOL_CALLS)
    # Free-form model usage summaries (token counts etc.); only bounded by the body size limit.
    usage: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_USAGE_ENTRIES)

    @model_validator(mode="after")
    def _check_consistency(self) -> CallRecord:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not be before started_at")
        turn_ids = [t.id for t in self.turns]
        if len(set(turn_ids)) != len(turn_ids):
            raise ValueError("turn ids must be unique within a call")
        tool_ids = [c.id for c in self.tool_calls]
        if len(set(tool_ids)) != len(tool_ids):
            raise ValueError("tool call ids must be unique within a call")
        return self

    def content_hash(self) -> str:
        """SHA-256 of the canonical JSON form, used for idempotent re-posts.

        Canonical means: after validation (so timestamps are normalized) and with sorted keys (so
        the free-form ``usage`` dicts hash the same regardless of key order).
        """
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def turn_by_id(self) -> dict[str, Turn]:
        return {t.id: t for t in self.turns}


# =============================================================================================
# Analysis v1
# =============================================================================================

Dimension = Literal[
    "customer_satisfaction",
    "customer_frustration",  # inverted: 1 = none, 5 = severe
    "resolution",
    "agent_helpfulness",
    "accuracy_groundedness",
    "conversation_flow",
    "efficiency",
    "tone_empathy",
    "policy_adherence",
]
DIMENSIONS: tuple[Dimension, ...] = Dimension.__args__  # type: ignore[attr-defined]

AnalysisStatus = Literal["pending", "running", "done", "failed"]
OutcomeStatus = Literal["resolved", "partially_resolved", "unresolved", "not_applicable"]
FlagType = Literal[
    "escalation_needed",
    "hallucination_risk",
    "policy_violation",
    "tool_failure",
    "caller_repeated",
    "long_silence",
    "other",
]
ProviderName = Literal["openai", "heuristic"]


class Evidence(BaseModel):
    turn_id: str
    quote: str


class DimensionScore(BaseModel):
    score: int = Field(ge=1, le=5)
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)


class Outcome(BaseModel):
    status: OutcomeStatus
    reason: str


class Flag(BaseModel):
    type: FlagType
    turn_id: str | None = None
    detail: str


class SentimentPoint(BaseModel):
    turn_id: str
    value: float = Field(ge=-1.0, le=1.0)


class AnalyzerInfo(BaseModel):
    provider: ProviderName
    model: str | None
    rubric_version: str


class Metrics(BaseModel):
    """Deterministic facts computed in code (never by the LLM)."""

    duration_s: float
    turns: int
    user_turns: int
    agent_turns: int
    talk_ratio_agent: float
    interruptions: int
    tool_calls: int
    tool_errors: int
    avg_agent_words_per_turn: float
    mean_transcript_confidence: float | None
    low_confidence_turns: int


class Analysis(BaseModel):
    """The analyzer's verdict on one call. Fields other than the status block are null until
    ``status == "done"``."""

    call_id: str
    status: AnalysisStatus
    error: str | None = None
    analyzer: AnalyzerInfo | None = None
    created_at: dt.datetime
    summary: str | None = None
    caller_intent: str | None = None
    outcome: Outcome | None = None
    overall_score: int | None = Field(default=None, ge=0, le=100)
    scores: dict[Dimension, DimensionScore] = Field(default_factory=dict)
    flags: list[Flag] = Field(default_factory=list)
    sentiment: list[SentimentPoint] = Field(default_factory=list)
    metrics: Metrics | None = None


# =============================================================================================
# Provider output schema ("drafts"): what the LLM / heuristic fills in, validated afterwards.
# Kept compatible with OpenAI strict structured outputs: every field required, no numeric
# bounds (not every OpenAI-compatible server enforces them; we check ranges in code instead).
# =============================================================================================


class EvidenceDraft(BaseModel):
    turn_id: str = Field(description="id of the turn the quote comes from")
    quote: str = Field(description="exact, verbatim substring of that turn's text (short)")


class DimensionDraft(BaseModel):
    score: float = Field(description="integer 1-5 per the rubric anchors")
    rationale: str = Field(description="one or two sentences")
    evidence: list[EvidenceDraft]


class ScoresDraft(BaseModel):
    customer_satisfaction: DimensionDraft
    customer_frustration: DimensionDraft
    resolution: DimensionDraft
    agent_helpfulness: DimensionDraft
    accuracy_groundedness: DimensionDraft
    conversation_flow: DimensionDraft
    efficiency: DimensionDraft
    tone_empathy: DimensionDraft
    policy_adherence: DimensionDraft


class OutcomeDraft(BaseModel):
    status: OutcomeStatus
    reason: str


class FlagDraft(BaseModel):
    type: FlagType
    turn_id: str | None
    detail: str


class SentimentDraft(BaseModel):
    turn_id: str
    value: float = Field(description="caller sentiment in this turn, -1.0 (very negative) to 1.0")


class AssessmentDraft(BaseModel):
    """Everything a provider judges. Metrics and the overall score are added by code."""

    summary: str = Field(description="2-3 sentences, plain language")
    caller_intent: str = Field(description="short phrase, e.g. 'Table for 4 at Nopa on Friday'")
    outcome: OutcomeDraft
    scores: ScoresDraft
    flags: list[FlagDraft]
    sentiment: list[SentimentDraft] = Field(description="one entry per caller (user) turn")
