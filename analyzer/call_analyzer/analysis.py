"""Runs a provider on a call and turns its (untrusted) draft into a validated :class:`Analysis`.

Division of labour:

* **Code** computes metrics, deterministic flags (tool failures, long silences), and the overall
  score. These are facts or arithmetic, so they're exact and reproducible.
* **The provider** (LLM or heuristic) supplies judgement: summary, intent, outcome, 1-5 scores
  with rationale and evidence, caller sentiment, and qualitative flags.
* **Validation** here decides what of the provider's answer to keep. Policy (documented in the
  README and asserted in tests):

  - A score outside [1, 5] (or not a number) is *rejected*: that dimension is omitted and the
    overall score renormalizes over the rest. Clamping would turn "the model returned 7" into a
    confident 5 that nobody actually judged. In-range fractions are rounded half-up.
  - If fewer than half the dimensions survive, the whole answer is treated as malformed
    (retryable), because the overall score would no longer mean much.
  - Evidence must cite an existing turn and its quote must appear in that turn's text (after
    normalizing case, whitespace, and quote characters). Anything else is dropped: a quote that
    isn't in the transcript is exactly the hallucination the evidence is meant to rule out.
  - Sentiment values are clamped to [-1, 1] (it's a continuous signal, clamping is harmless)
    and kept only for existing caller turns.
  - Flags citing an unknown turn keep their detail but lose the turn reference.
    ``tool_failure``/``long_silence`` flags from the provider are dropped in favor of the
    deterministic ones.
  - Free text is trimmed to bounded lengths so one verbose answer can't bloat storage or the UI.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
import unicodedata

from .metrics import compute_metrics, find_long_silences
from .models import (
    DIMENSIONS,
    Analysis,
    AnalyzerInfo,
    AssessmentDraft,
    CallRecord,
    Dimension,
    DimensionDraft,
    DimensionScore,
    Evidence,
    Flag,
    Outcome,
    SentimentPoint,
)
from .providers.base import AnalysisProvider, ProviderError
from .rubric import Rubric

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 1_000
MAX_INTENT_CHARS = 120
MAX_REASON_CHARS = 500
MAX_RATIONALE_CHARS = 600
MAX_QUOTE_CHARS = 400
MAX_EVIDENCE_PER_DIMENSION = 3
MAX_FLAGS = 20
MAX_FLAG_DETAIL_CHARS = 500
MIN_VALID_DIMENSIONS = math.ceil(len(DIMENSIONS) / 2)
DETERMINISTIC_FLAG_TYPES = frozenset({"tool_failure", "long_silence"})

_QUOTE_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
# Models like to wrap quotes in quotation marks or add ellipses; none of that is evidence.
_QUOTE_WRAPPING = " \t\n\"'.,;:!?\u2026\u201c\u201d\u2018\u2019"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def normalize_for_match(text: str) -> str:
    """Case-, whitespace-, and quote-style-insensitive form used to check evidence quotes."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_CHARS).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _valid_score(draft: DimensionDraft) -> int | None:
    raw = draft.score
    if not isinstance(raw, (int, float)) or not math.isfinite(raw) or not 1 <= raw <= 5:
        return None
    return math.floor(raw + 0.5)


def _valid_evidence(draft: DimensionDraft, texts: dict[str, str]) -> list[Evidence]:
    kept: list[Evidence] = []
    for item in draft.evidence:
        quote = item.quote.strip(_QUOTE_WRAPPING)
        turn_text = texts.get(item.turn_id)
        if turn_text is None or len(quote) < 2 or len(quote) > MAX_QUOTE_CHARS:
            continue
        if normalize_for_match(quote) not in normalize_for_match(turn_text):
            continue
        kept.append(Evidence(turn_id=item.turn_id, quote=quote))
        if len(kept) == MAX_EVIDENCE_PER_DIMENSION:
            break
    return kept


def validate_draft(
    draft: AssessmentDraft, record: CallRecord
) -> tuple[dict[Dimension, DimensionScore], list[Flag], list[SentimentPoint]]:
    """Apply the validation policy in the module docstring. Raises ProviderError if unusable."""
    texts = {t.id: t.text for t in record.turns}
    scores: dict[Dimension, DimensionScore] = {}
    dropped_evidence = 0
    for dim in DIMENSIONS:
        dim_draft: DimensionDraft = getattr(draft.scores, dim)
        score = _valid_score(dim_draft)
        if score is None:
            logger.warning(
                "discarding out-of-range score",
                extra={"call_id": record.call_id, "dimension": dim, "score": dim_draft.score},
            )
            continue
        evidence = _valid_evidence(dim_draft, texts)
        dropped_evidence += len(dim_draft.evidence) - len(evidence)
        scores[dim] = DimensionScore(
            score=score,
            rationale=_clip(dim_draft.rationale, MAX_RATIONALE_CHARS),
            evidence=evidence,
        )
    if dropped_evidence:
        logger.info(
            "dropped unverifiable evidence quotes",
            extra={"call_id": record.call_id, "dropped": dropped_evidence},
        )
    if len(scores) < MIN_VALID_DIMENSIONS:
        raise ProviderError(
            f"only {len(scores)} of {len(DIMENSIONS)} scores were in range 1-5", retryable=True
        )

    flags = [
        Flag(
            type=f.type,
            turn_id=f.turn_id if f.turn_id in texts else None,
            detail=_clip(f.detail, MAX_FLAG_DETAIL_CHARS),
        )
        for f in draft.flags
        if f.type not in DETERMINISTIC_FLAG_TYPES
    ][:MAX_FLAGS]

    user_turn_order = {t.id: i for i, t in enumerate(record.turns) if t.role == "user"}
    sentiment: dict[str, SentimentPoint] = {}
    for point in draft.sentiment:
        known = point.turn_id in user_turn_order and point.turn_id not in sentiment
        if known and math.isfinite(point.value):
            value = max(-1.0, min(1.0, point.value))
            sentiment[point.turn_id] = SentimentPoint(turn_id=point.turn_id, value=value)
    ordered = sorted(sentiment.values(), key=lambda p: user_turn_order[p.turn_id])
    return scores, flags, ordered


def deterministic_flags(record: CallRecord) -> list[Flag]:
    """Facts the code can establish on its own, independent of the provider."""
    flags = [
        Flag(
            type="tool_failure",
            turn_id=None,
            detail=_clip(
                f"{call.name} failed: {call.output or 'no output'}", MAX_FLAG_DETAIL_CHARS
            ),
        )
        for call in record.tool_calls
        if call.is_error
    ]
    flags += [
        Flag(
            type="long_silence",
            turn_id=gap.before_turn_id,
            detail=f"{gap.seconds:g} s of silence before this turn.",
        )
        for gap in find_long_silences(record)
    ]
    return flags


class CallAnalyzer:
    """Glue between a provider and the rubric; stateless apart from those two."""

    def __init__(self, provider: AnalysisProvider, rubric: Rubric) -> None:
        self.provider = provider
        self.rubric = rubric

    @property
    def info(self) -> AnalyzerInfo:
        return AnalyzerInfo(
            provider=self.provider.name,
            model=self.provider.model,
            rubric_version=self.rubric.version,
        )

    async def analyze(self, record: CallRecord) -> Analysis:
        """Analyze one call. Raises :class:`ProviderError` if the provider fails or its answer
        is unusable; the worker decides whether to retry."""
        metrics = compute_metrics(record)
        draft = await self.provider.assess(record, metrics, self.rubric)
        scores, flags, sentiment = validate_draft(draft, record)
        return Analysis(
            call_id=record.call_id,
            status="done",
            analyzer=self.info,
            created_at=dt.datetime.now(dt.timezone.utc),
            summary=_clip(draft.summary, MAX_SUMMARY_CHARS),
            caller_intent=_clip(draft.caller_intent, MAX_INTENT_CHARS),
            outcome=Outcome(
                status=draft.outcome.status, reason=_clip(draft.outcome.reason, MAX_REASON_CHARS)
            ),
            overall_score=self.rubric.overall_score(scores),
            scores=scores,
            flags=deterministic_flags(record) + flags,
            sentiment=sentiment,
            metrics=metrics,
        )

    async def aclose(self) -> None:
        await self.provider.aclose()
