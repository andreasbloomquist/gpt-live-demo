"""Tier-agnostic trial loop: budget, repetition, deterministic checks, judge, early stop.

A tier runner only has to turn a case into a :class:`Conversation`; everything about *how
results are graded and aggregated* lives here, so both tiers apply identical policy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from evals.runners.assertions import Transcript, check_expectations
from evals.runners.common import (
    Budget,
    BudgetExceeded,
    CaseResult,
    SuiteResult,
    TrialResult,
)
from evals.runners.judge import judge_transcript
from evals.schema import Case, Suite, Tier

logger = logging.getLogger("evals")


@dataclass
class Conversation:
    transcript: Transcript
    cost_usd: float = 0.0
    latency_s: float = 0.0
    first_response_latency_s: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class TierRunner(Protocol):
    tier: Tier
    concurrency: int

    async def setup(self, suite: Suite) -> dict[str, Any]:
        """Prepare per-suite state; returns model/config info recorded in the results."""
        ...

    def estimate_trial_usd(self, suite: Suite, case: Case) -> float: ...

    async def converse(self, suite: Suite, case: Case, trial: int) -> Conversation: ...

    async def aclose(self) -> None: ...


@dataclass
class HarnessOptions:
    trials_override: int | None = None
    early_stop: bool = True
    """Stop a case once its threshold is mathematically unreachable (saves money on clear
    failures). Never stops early on success: the remaining trials feed pass^k."""
    judge: bool = True


async def run_suite(
    runner: TierRunner,
    suite: Suite,
    *,
    judge_client: Any,
    budget: Budget,
    today: dt.date | None,
    options: HarnessOptions,
) -> SuiteResult:
    result = SuiteResult(suite=suite.name, tier=runner.tier, profile=suite.profile)
    started = time.monotonic()
    result.model_info = await runner.setup(suite)
    semaphore = asyncio.Semaphore(runner.concurrency)
    budget_error: list[str] = []

    async def run_case(case: Case) -> CaseResult:
        n = options.trials_override or suite.trials_for(case)
        case_result = CaseResult(case.id, [], suite.pass_threshold, case.description)
        for trial in range(1, n + 1):
            if budget_error:
                break
            failures = len(case_result.trials) - case_result.passes
            if options.early_stop and (n - failures) / n < suite.pass_threshold:
                logger.info("%s/%s: threshold unreachable, stopping early", suite.name, case.id)
                break
            try:
                reserved = budget.reserve(runner.estimate_trial_usd(suite, case))
            except BudgetExceeded as exc:
                budget_error.append(str(exc))
                break
            async with semaphore:
                trial_result = await _run_trial(
                    runner, suite, case, trial, judge_client, today, options
                )
            budget.settle(reserved, trial_result.cost_usd)
            case_result.trials.append(trial_result)
            logger.info(
                "%s/%s/%s trial %d: %s%s",
                runner.tier,
                suite.name,
                case.id,
                trial,
                "PASS" if trial_result.passed else "FAIL",
                "" if trial_result.passed else f" — {trial_result.failure_summary}",
            )
        return case_result

    try:
        result.cases = list(
            await asyncio.gather(*(run_case(c) for c in suite.cases_for(runner.tier)))
        )
    finally:
        await runner.aclose()
    if budget_error:
        result.status, result.status_detail = "budget_exceeded", budget_error[0]
    result.cost_usd = sum(t.cost_usd for c in result.cases for t in c.trials)
    result.duration_s = time.monotonic() - started
    return result


async def _run_trial(
    runner: TierRunner,
    suite: Suite,
    case: Case,
    trial: int,
    judge_client: Any,
    today: dt.date | None,
    options: HarnessOptions,
) -> TrialResult:
    try:
        conv = await runner.converse(suite, case, trial)
    except Exception as exc:  # a crashed trial is a failed trial, not a crashed run
        logger.exception("trial crashed")
        return TrialResult(trial=trial, passed=False, error=f"{type(exc).__name__}: {exc}")

    checks = check_expectations(case.expect, conv.transcript, today=today)
    result = TrialResult(
        trial=trial,
        passed=all(c.passed for c in checks),
        checks=checks,
        transcript=conv.transcript.render(),
        latency_s=round(conv.latency_s, 3),
        first_response_latency_s=conv.first_response_latency_s,
        cost_usd=conv.cost_usd,
        meta=conv.meta,
    )
    # Judge only when deterministic checks pass: cheaper, and it keeps judge verdicts about
    # the rubric instead of about an already-failed trial.
    if result.passed and case.expect.judge and options.judge:
        try:
            verdict, judge_cost = await judge_transcript(
                judge_client, rubric=case.expect.judge, transcript=result.transcript
            )
        except Exception as exc:
            result.passed = False
            result.error = f"judge failed: {type(exc).__name__}: {exc}"
        else:
            result.judge = verdict
            result.cost_usd += judge_cost
            result.passed = verdict.passed
    return result
