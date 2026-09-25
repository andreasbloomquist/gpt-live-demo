"""Result types, pass-rate statistics, pricing and the hard budget guard."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from evals.runners.assertions import CheckResult

# --------------------------------------------------------------------------------------------
# Pricing. ESTIMATES for budget enforcement only — check current OpenAI pricing and override
# with EVALS_PRICING_JSON='{"model": {"input": usd_per_1m, "output": usd_per_1m}}'.
# Unknown models fall back to the most expensive known text model, so the budget guard errs
# toward stopping early rather than overspending.
# --------------------------------------------------------------------------------------------

_DEFAULT_TEXT_PRICES: dict[str, dict[str, float]] = {
    "gpt-5.6-luna": {"input": 1.25, "output": 10.0},
    "gpt-5.6-mini": {"input": 0.25, "output": 2.0},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
}
WEB_SEARCH_CALL_USD = 0.01
"""Per hosted web-search call (estimate)."""
VOICE_USD_PER_MINUTE = 0.05
"""GPT-Live voice session, billed per second (public list price at time of writing)."""
TTS_USD_PER_1M_CHARS = 15.0
"""Synthesizing user turns (estimate; cached on disk, so usually paid once)."""


def _text_prices() -> dict[str, dict[str, float]]:
    prices = dict(_DEFAULT_TEXT_PRICES)
    override = os.environ.get("EVALS_PRICING_JSON")
    if override:
        prices.update(json.loads(override))
    return prices


def text_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = _text_prices()
    rate = prices.get(model) or max(prices.values(), key=lambda p: p["output"])
    return (input_tokens * rate["input"] + output_tokens * rate["output"]) / 1_000_000


# Static per-trial estimates. The budget reserves them before a trial starts (see ``Budget``)
# and ``--dry-run`` sums them, so they deliberately round up.
_BRAIN_INPUT_TOKENS_PER_TURN = 8_000
"""~2 backend calls per turn, each re-sending a few thousand tokens of instructions."""
_BRAIN_OUTPUT_TOKENS_PER_TURN = 800
_JUDGE_CALL_USD = 0.01
_VOICE_BASE_SECONDS = 20
"""Session setup and the agent's greeting."""
_VOICE_SECONDS_PER_TURN = 25
_VOICE_EXTRAS_USD_PER_TURN = 0.02
"""TTS for the scripted turn plus backend tokens GPT-Live spends on it."""


def estimate_brain_trial_usd(backend_model: str, turns: int) -> float:
    """Upper-bound cost of one brain-tier trial of ``turns`` user turns, judge included."""
    tokens = text_cost(
        backend_model, _BRAIN_INPUT_TOKENS_PER_TURN * turns, _BRAIN_OUTPUT_TOKENS_PER_TURN * turns
    )
    return tokens + WEB_SEARCH_CALL_USD * turns + _JUDGE_CALL_USD


def estimate_voice_trial_usd(turns: int) -> float:
    """Upper-bound cost of one voice-tier trial of ``turns`` user turns, judge included."""
    minutes = (_VOICE_BASE_SECONDS + _VOICE_SECONDS_PER_TURN * turns) / 60
    return minutes * VOICE_USD_PER_MINUTE + _VOICE_EXTRAS_USD_PER_TURN * turns + _JUDGE_CALL_USD


# --------------------------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------------------------


class BudgetExceeded(RuntimeError):
    """Raised *before* starting work that would exceed ``--max-cost-usd``."""


@dataclass
class Budget:
    """Hard spend ceiling for one runner invocation.

    Before a trial starts it *reserves* a pessimistic estimate (the larger of the static
    estimate and the most expensive trial seen so far); the reservation is swapped for the
    real cost when the trial ends. Because reservations count against the ceiling, concurrent
    trials cannot collectively overshoot it. Real cost comes from reported token usage, and
    from billed session seconds for voice.
    """

    max_usd: float | None
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    worst_trial_usd: float = 0.0

    def reserve(self, estimate_usd: float) -> float:
        amount = max(estimate_usd, self.worst_trial_usd)
        if self.max_usd is not None and self.spent_usd + self.reserved_usd + amount > self.max_usd:
            raise BudgetExceeded(
                f"budget ${self.max_usd:.2f} would be exceeded (spent ${self.spent_usd:.3f}, "
                f"in flight ${self.reserved_usd:.3f}, next trial ≈ ${amount:.3f})"
            )
        self.reserved_usd += amount
        return amount

    def settle(self, reserved: float, actual_usd: float) -> None:
        self.reserved_usd = max(0.0, self.reserved_usd - reserved)
        self.spent_usd += actual_usd
        self.worst_trial_usd = max(self.worst_trial_usd, actual_usd)


# --------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------


@dataclass
class JudgeVerdict:
    passed: bool
    reason: str
    model: str = ""


@dataclass
class TrialResult:
    trial: int
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    judge: JudgeVerdict | None = None
    transcript: str = ""
    error: str | None = None
    latency_s: float = 0.0
    """Wall time of the trial (conversation only, excluding judging)."""
    first_response_latency_s: float | None = None
    cost_usd: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    crashed: bool = False
    """The conversation raised, so its real cost is unknown: ``cost_usd`` is the reservation
    the budget charged for it."""

    @property
    def failure_summary(self) -> str:
        if self.error:
            return f"error: {self.error}"
        failed = [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]
        if self.judge is not None and not self.judge.passed:
            failed.append(f"judge: {self.judge.reason}")
        return "; ".join(failed)


PASS_HAT_K = 2
"""The fixed k reported as ``pass^k``. Fixed (not the trial count) so the number is comparable
across cases and runs, and informative: with k equal to the trial count it could only ever be
0 or 1. A case with fewer than k trials (trials: 1, or stopped early after an unrecoverable
failure, which means it already failed) reports ``None`` rather than a made-up number."""


def pass_hat_k(n: int, c: int, k: int = PASS_HAT_K) -> float | None:
    """Unbiased estimate of P(k independent attempts ALL pass) from n trials with c passes:
    ``C(c, k) / C(n, k)``. ``None`` when fewer than k trials ran.

    For a voice agent this is the reliability number that matters: every caller gets one
    sample, so "right 2 times out of 3" means "wrong for one caller in three", and pass^2 of
    that case is 1/3, not 2/3.
    """
    if n < k:
        return None
    return math.comb(c, k) / math.comb(n, k)


@dataclass
class CaseResult:
    case_id: str
    trials: list[TrialResult]
    threshold: float
    description: str | None = None

    @property
    def n(self) -> int:
        return len(self.trials)

    @property
    def passes(self) -> int:
        return sum(t.passed for t in self.trials)

    @property
    def pass_rate(self) -> float:
        return self.passes / self.n if self.n else 0.0

    @property
    def passed(self) -> bool:
        return self.n > 0 and self.pass_rate >= self.threshold

    def stats(self) -> dict[str, float | int | None]:
        """``pass_rate`` (= unbiased pass@1) and ``pass_hat_k`` for the fixed ``PASS_HAT_K``."""
        hat = pass_hat_k(self.n, self.passes)
        return {
            "pass_rate": round(self.pass_rate, 3),
            "pass_hat_k": None if hat is None else round(hat, 3),
            "k": PASS_HAT_K,
        }


SuiteStatus = Literal["completed", "budget_exceeded", "skipped"]


@dataclass
class SuiteResult:
    suite: str
    tier: str
    profile: str
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0
    cases: list[CaseResult] = field(default_factory=list)
    status: SuiteStatus = "completed"
    status_detail: str = ""
    cost_usd: float = 0.0
    model_info: dict[str, Any] = field(default_factory=dict)
    fingerprint: str | None = None

    @property
    def passed(self) -> bool:
        if self.status == "skipped":
            return True
        return self.status == "completed" and all(c.passed for c in self.cases)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["passed"] = self.passed
        for case_dict, case in zip(data["cases"], self.cases, strict=True):
            case_dict.update(passed=case.passed, **case.stats())
        return data
