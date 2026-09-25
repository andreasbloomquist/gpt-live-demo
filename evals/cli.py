"""``python -m evals`` — run, validate and report on eval suites.

    python -m evals validate
    python -m evals run --tier brain --dry-run
    python -m evals run --tier brain --suite restaurant_availability --trials 5 --max-cost-usd 2
    python -m evals run --tier voice --suite conversation_style --max-cost-usd 5
    python -m evals report --results-dir evals/.results --plan plan.json

Exit codes: 0 all passed (or skipped), 1 eval failures, 2 configuration error,
3 budget exhausted before the run completed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evals.paths import RESULTS_DIR, SUITE_SCHEMA_PATH, SUITES_DIR
from evals.schema import Suite, SuiteLoadError, Tier, load_suites, suite_json_schema

if TYPE_CHECKING:
    from evals.runners.harness import TierRunner

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_BUDGET = 0, 1, 2, 3
OPENAI_TIMEOUT_S = 60.0
OPENAI_MAX_RETRIES = 3
DEFAULT_BACKEND_MODEL = "gpt-5.6-luna"
"""GPT-Live's default backend model: what dry-run prices when the agent cannot be imported."""


def _suite_names(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    names = [n.strip() for v in values for n in v.split(",") if n.strip()]
    return None if not names or names == ["all"] else names


# --------------------------------------------------------------------------------------------
# validate / dry-run (no network, no keys)
# --------------------------------------------------------------------------------------------


def cross_check(suites: dict[str, Suite]) -> list[str]:
    """Check suites against the *agent*: profile exists and renders, declared tools are enabled
    in that profile, and tool schemas build. Catches drift that pydantic alone cannot."""
    from evals.runners import agent_bridge
    from evals.toolschema import schema_tool_name, tools_to_responses_schemas

    errors: list[str] = []
    settings = agent_bridge.load_settings()
    for suite in suites.values():
        try:
            bundle = agent_bridge.compose_bundle(suite.profile, settings, runtime_variables=False)
            schemas = tools_to_responses_schemas(agent_bridge.resolve_tools(bundle, settings))
        except Exception as exc:
            errors.append(f"{suite.name}: profile {suite.profile!r} failed to build: {exc}")
            continue
        missing = sorted(set(suite.tools) - set(bundle.tools))
        if missing:
            errors.append(
                f"{suite.name}: tools {missing} are not enabled in profile {suite.profile!r}"
            )
        if len(schemas) != len(bundle.tools):
            built = [schema_tool_name(s) for s in schemas]
            errors.append(f"{suite.name}: profile tools {bundle.tools} built schemas {built}")
    return errors


def configured_backend_model() -> str:
    """The backend model the agent is configured with (``GPT_LIVE_BACKEND_MODEL`` / Settings
    default), so dry-run estimates price the model that would actually run. Falls back to the
    GPT-Live default if the agent package cannot be imported."""
    try:
        from evals.runners import agent_bridge

        return str(agent_bridge.load_settings().gpt_live_backend_model)
    except Exception:
        return DEFAULT_BACKEND_MODEL


def _estimate(
    tier: Tier, suite: Suite, trials: int | None, backend_model: str = DEFAULT_BACKEND_MODEL
) -> tuple[int, float]:
    """(trial count, rough USD) without constructing runners or touching the network. Uses the
    same per-trial estimates the runners reserve against the budget."""
    from evals.runners.common import estimate_brain_trial_usd, estimate_voice_trial_usd

    total_trials, usd = 0, 0.0
    for case in suite.cases_for(tier):
        n = trials or suite.trials_for(case)
        turns = len(case.user_turns)
        if tier == "brain":
            per_trial = estimate_brain_trial_usd(backend_model, turns)
        else:
            per_trial = estimate_voice_trial_usd(turns)
        total_trials += n
        usd += per_trial * n
    return total_trials, usd


def _dry_run(tier: Tier, suites: dict[str, Suite], args: argparse.Namespace) -> int:
    errors = [] if args.no_agent_check else cross_check(suites)
    backend_model = configured_backend_model()
    print(f"DRY RUN — tier={tier} (no network calls, no API key needed)")
    if tier == "brain":
        print(f"  backend model: {backend_model}")
    grand = 0.0
    for suite in suites.values():
        cases = suite.cases_for(tier)
        if tier not in suite.tiers or not cases:
            print(f"  {suite.name}: skipped (no {tier}-tier cases)")
            continue
        n, usd = _estimate(tier, suite, args.trials, backend_model)
        grand += usd
        print(
            f"  {suite.name}: profile={suite.profile} cases={len(cases)} trials={n} "
            f"threshold={suite.pass_threshold:.0%} est≈${usd:.2f}"
        )
        for case in cases:
            print(
                f"    - {case.id}: {len(case.user_turns)} turn(s), "
                f"{args.trials or suite.trials_for(case)} trial(s)"
                + (", judge" if case.expect.judge else "")
            )
    print(f"  estimated total ≈ ${grand:.2f} (upper-bound estimate)")
    if args.max_cost_usd is not None and grand > args.max_cost_usd:
        print(
            f"  NOTE: estimate exceeds --max-cost-usd {args.max_cost_usd}; the run would stop "
            "early with exit code 3"
        )
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    return EXIT_CONFIG if errors else EXIT_OK


# --------------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------------


def _skipped_results(tier: Tier, suites: dict[str, Suite], reason: str, results_dir: Path) -> int:
    from evals.report import write_results
    from evals.runners.common import SuiteResult

    for suite in suites.values():
        result = SuiteResult(
            suite=suite.name,
            tier=tier,
            profile=suite.profile,
            status="skipped",
            status_detail=reason,
        )
        write_results(result, results_dir)
    print(f"skipped: {reason}")
    return EXIT_OK


def _make_runner(tier: Tier, client: Any, args: argparse.Namespace) -> TierRunner:
    if tier == "brain":
        from evals.runners.brain import DEFAULT_CONCURRENCY, BrainRunner

        return BrainRunner(client=client, concurrency=args.concurrency or DEFAULT_CONCURRENCY)
    from evals.runners.voice import DEFAULT_CONCURRENCY as VOICE_CONCURRENCY
    from evals.runners.voice import VoiceRunner

    return VoiceRunner(
        client=client,
        concurrency=args.concurrency or VOICE_CONCURRENCY,
        input_mode=args.voice_input,
    )


async def _run(tier: Tier, suites: dict[str, Suite], args: argparse.Namespace) -> int:
    from openai import AsyncOpenAI

    from evals.report import results_markdown, write_results
    from evals.runners import agent_bridge
    from evals.runners.common import Budget
    from evals.runners.harness import HarnessOptions, run_suite

    settings = agent_bridge.load_settings()
    # Serves the backend, TTS and judge calls. The SDK default (600 s per attempt) would let one
    # stuck request eat most of a CI job's timeout.
    client = AsyncOpenAI(
        api_key=agent_bridge.openai_api_key(settings),
        timeout=OPENAI_TIMEOUT_S,
        max_retries=OPENAI_MAX_RETRIES,
    )
    budget = Budget(max_usd=args.max_cost_usd)
    options = HarnessOptions(
        trials_override=args.trials, early_stop=not args.no_early_stop, judge=not args.no_judge
    )
    today = agent_bridge.agent_today(settings)
    fingerprints: dict[str, str] = json.loads(args.fingerprints) if args.fingerprints else {}

    exit_code = EXIT_OK
    rendered: list[dict[str, Any]] = []
    try:
        for suite in suites.values():
            if tier not in suite.tiers or not suite.cases_for(tier):
                continue
            result = await run_suite(
                _make_runner(tier, client, args),
                suite,
                judge_client=client,
                budget=budget,
                today=today,
                options=options,
            )
            result.fingerprint = fingerprints.get(suite.name)
            paths = write_results(result, args.results_dir)
            rendered.append(result.to_dict())
            print(f"{tier}/{suite.name}: {'PASS' if result.passed else 'FAIL'} → {paths['json']}")
            if result.status == "budget_exceeded":
                exit_code = EXIT_BUDGET
                break
            if not result.passed:
                exit_code = EXIT_FAILED
    finally:
        await client.close()
    print(results_markdown(rendered))
    print(f"total spend ≈ ${budget.spent_usd:.3f}")
    return exit_code


def _cmd_run(args: argparse.Namespace) -> int:
    tier: Tier = args.tier
    try:
        suites = load_suites(SUITES_DIR, _suite_names(args.suite))
    except SuiteLoadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.dry_run:
        return _dry_run(tier, suites, args)
    if not os.environ.get("OPENAI_API_KEY"):
        if args.skip_if_no_key:
            return _skipped_results(tier, suites, "OPENAI_API_KEY not available", args.results_dir)
        print("ERROR: OPENAI_API_KEY is not set (use --dry-run to plan offline)", file=sys.stderr)
        return EXIT_CONFIG
    return asyncio.run(_run(tier, suites, args))


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        suites = load_suites(SUITES_DIR)
    except SuiteLoadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    errors = [] if args.no_agent_check else cross_check(suites)
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if not errors:
        print(f"{len(suites)} suite(s) valid: {', '.join(suites)}")
    return EXIT_CONFIG if errors else EXIT_OK


def _cmd_schema(args: argparse.Namespace) -> int:
    text = json.dumps(suite_json_schema(), indent=2) + "\n"
    if args.write:
        SUITE_SCHEMA_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {SUITE_SCHEMA_PATH}")
    else:
        print(text, end="")
    return EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    from evals.report import aggregate_markdown

    md = aggregate_markdown(args.results_dir, args.plan, args.note)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
    else:
        print(md)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("EVALS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run suites on one tier")
    run.add_argument("--tier", choices=("brain", "voice"), required=True)
    run.add_argument("--suite", action="append", help="suite name(s); repeat or comma-separate")
    run.add_argument("--dry-run", action="store_true", help="validate + plan; no network")
    run.add_argument("--max-cost-usd", type=float, default=None, help="hard spend ceiling")
    run.add_argument("--trials", type=int, default=None, help="override trials per case")
    run.add_argument("--concurrency", type=int, default=None)
    run.add_argument(
        "--voice-input",
        choices=("audio", "text"),
        default="audio",
        help="voice tier: TTS audio (default, faithful) or session.run text "
        "injection (unverified with GPT-Live; smoke tests only)",
    )
    run.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    run.add_argument(
        "--skip-if-no-key",
        action="store_true",
        help="write 'skipped' results and exit 0 when OPENAI_API_KEY is absent",
    )
    run.add_argument("--no-judge", action="store_true", help="deterministic checks only")
    run.add_argument("--no-early-stop", action="store_true")
    run.add_argument(
        "--no-agent-check",
        action="store_true",
        help="dry-run: skip rendering the agent's profile/tools",
    )
    run.add_argument(
        "--fingerprints",
        default=None,
        help="JSON {suite: fingerprint} from the impact plan, stored in results",
    )
    run.set_defaults(func=_cmd_run)

    val = sub.add_parser("validate", help="validate suites (schema + agent cross-check)")
    val.add_argument("--no-agent-check", action="store_true")
    val.set_defaults(func=_cmd_validate)

    sch = sub.add_parser("schema", help="print or --write evals/suite.schema.json")
    sch.add_argument("--write", action="store_true")
    sch.set_defaults(func=_cmd_schema)

    rep = sub.add_parser("report", help="aggregate result JSON files into markdown")
    rep.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    rep.add_argument("--plan", type=Path, default=None)
    rep.add_argument("--output", default=None)
    rep.add_argument("--note", action="append", default=[])
    rep.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return int(args.func(args))
