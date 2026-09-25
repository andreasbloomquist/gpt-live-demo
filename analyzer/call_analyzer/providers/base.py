"""The provider interface: anything that can turn a call record into an assessment draft."""

from __future__ import annotations

from typing import Protocol

from ..models import AssessmentDraft, CallRecord, Metrics, ProviderName
from ..rubric import Rubric


class ProviderError(Exception):
    """An assessment attempt failed.

    ``retryable`` tells the worker whether trying again later could help (rate limits, 5xx,
    timeouts, a malformed answer from a non-deterministic model) or not (bad API key, unknown
    model). The message is stored and shown in the UI, so it must never contain secrets or
    transcript content.
    """

    def __init__(
        self, message: str, *, retryable: bool, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_s = retry_after_s


class AnalysisProvider(Protocol):
    """What :class:`~call_analyzer.analysis.CallAnalyzer` needs from a judge (LLM or offline)."""

    # Read-only properties rather than plain attributes so implementations can narrow the
    # types (e.g. ``model: str``) and still satisfy the protocol.
    @property
    def name(self) -> ProviderName:
        """Recorded as ``analyzer.provider`` on every analysis."""
        ...

    @property
    def model(self) -> str | None:
        """Recorded as ``analyzer.model``; ``None`` when no model is involved."""
        ...

    async def assess(self, record: CallRecord, metrics: Metrics, rubric: Rubric) -> AssessmentDraft:
        """Judge one call. Raises :class:`ProviderError` on failure."""
        ...

    async def aclose(self) -> None:
        """Release network resources (HTTP connection pools)."""
        ...
