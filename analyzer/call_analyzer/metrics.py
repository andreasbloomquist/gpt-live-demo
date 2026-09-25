"""Deterministic call metrics, computed in code so they are exact and free.

LLMs are bad at counting; these numbers are facts about the record, so they are never asked of a
model. They also feed the heuristic provider and the deterministic flags.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .models import CallRecord, Metrics, Turn

# Below this ASR confidence a caller turn is likely misheard. 0.6 is a common cut-off for
# streaming ASR; tune it for your recognizer.
LOW_CONFIDENCE_THRESHOLD = 0.6
# A gap between consecutive turns longer than this is dead air a caller notices on a phone line.
LONG_SILENCE_S = 6.0

_WORD = re.compile(r"[\w']+")


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def _speaking_seconds(turn: Turn) -> float | None:
    if turn.started_at is None or turn.ended_at is None:
        return None
    return max(0.0, (turn.ended_at - turn.started_at).total_seconds())


def _talk_ratio_agent(turns: list[Turn]) -> float:
    """Share of talk that was the agent's: by speaking time when every turn is timed (the true
    measure), else by word count (a close proxy when the agent didn't record timestamps)."""
    durations = [_speaking_seconds(t) for t in turns]
    if turns and all(d is not None for d in durations):
        agent = sum(d for t, d in zip(turns, durations, strict=True) if t.role == "assistant" and d)
        total = sum(d for d in durations if d)
        if total > 0:
            return round(agent / total, 3)
    agent_words = sum(word_count(t.text) for t in turns if t.role == "assistant")
    total_words = sum(word_count(t.text) for t in turns)
    return round(agent_words / total_words, 3) if total_words else 0.0


def compute_metrics(record: CallRecord) -> Metrics:
    turns = record.turns
    agent_turns = [t for t in turns if t.role == "assistant"]
    confidences = [t.transcript_confidence for t in turns if t.transcript_confidence is not None]
    agent_words = [word_count(t.text) for t in agent_turns]
    return Metrics(
        duration_s=round(record.duration_s, 1),
        turns=len(turns),
        user_turns=len(turns) - len(agent_turns),
        agent_turns=len(agent_turns),
        talk_ratio_agent=_talk_ratio_agent(turns),
        interruptions=sum(1 for t in turns if t.interrupted),
        tool_calls=len(record.tool_calls),
        tool_errors=sum(1 for c in record.tool_calls if c.is_error),
        avg_agent_words_per_turn=round(sum(agent_words) / len(agent_words), 1)
        if agent_words
        else 0.0,
        mean_transcript_confidence=(
            round(sum(confidences) / len(confidences), 3) if confidences else None
        ),
        low_confidence_turns=sum(1 for c in confidences if c < LOW_CONFIDENCE_THRESHOLD),
    )


@dataclass(frozen=True)
class Silence:
    after_turn_id: str
    before_turn_id: str
    seconds: float


def find_long_silences(record: CallRecord, threshold_s: float = LONG_SILENCE_S) -> list[Silence]:
    """Gaps between the end of one turn and the start of the next (needs turn timestamps).

    Turns are ordered by start time because full-duplex speech overlaps and the recorded order
    may not be strictly chronological.
    """
    timed = sorted(
        (t.started_at, t.ended_at, t.id)
        for t in record.turns
        if t.started_at is not None and t.ended_at is not None
    )
    silences: list[Silence] = []
    latest_end: dt.datetime | None = None
    latest_id = ""
    for started, ended, turn_id in timed:
        if latest_end is not None:
            gap = (started - latest_end).total_seconds()
            if gap > threshold_s:
                silences.append(Silence(latest_id, turn_id, round(gap, 1)))
        if latest_end is None or ended > latest_end:
            latest_end, latest_id = ended, turn_id
    return silences
