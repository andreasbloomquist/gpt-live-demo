"""Command line: ``python -m call_analyzer serve | seed | analyze``.

* ``serve``   run the HTTP API with its background worker.
* ``seed``    load demo (or your own) CallRecord JSON files into the database and analyze them
              in the foreground, so the UI has data with zero keys.
* ``analyze`` grade one CallRecord file and print the Analysis JSON; touches no database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from .analysis import CallAnalyzer
from .config import ConfigurationError, ProviderChoice, Settings
from .logs import configure_logging
from .models import CallRecord
from .providers import ProviderError, build_provider
from .rubric import default_rubric
from .storage import CallRepository, SQLiteCallRepository
from .worker import AnalysisWorker

# Source checkout / Docker image layout: analyzer/demo/calls next to the package.
DEFAULT_DEMO_DIR = Path(__file__).resolve().parents[1] / "demo" / "calls"
PROVIDER_CHOICES = get_args(ProviderChoice)


def _settings(provider: str | None = None) -> Settings:
    """Settings from the environment, with ``--provider`` (if given) taking precedence."""
    settings = Settings()
    if provider:
        settings = settings.model_copy(update={"provider": provider})
    return settings


def _load_record(path: Path) -> CallRecord:
    return CallRecord.model_validate_json(path.read_bytes())


# --- serve --------------------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn  # deferred: only `serve` needs the server stack

    from .api import create_app

    settings = _settings()
    app = create_app(settings)
    # One process only: the worker lives in-process and SQLite wants a single writer.
    uvicorn.run(
        app,
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level=settings.log_level.lower(),
        log_config=None,  # keep our formatter instead of uvicorn's
        server_header=False,
    )
    return 0


# --- seed ---------------------------------------------------------------------------------------


def cmd_seed(args: argparse.Namespace) -> int:
    return asyncio.run(_seed(args))


async def _seed(args: argparse.Namespace) -> int:
    """Store every ``*.json`` record in ``args.dir``, analyze them, and print a results table.
    Exits non-zero if any file was skipped or any analysis failed."""
    settings = _settings(args.provider)
    files = sorted(Path(args.dir).glob("*.json"))
    if not files:
        print(f"No *.json files in {args.dir}", file=sys.stderr)
        return 1
    analyzer = CallAnalyzer(build_provider(settings), default_rubric())
    repo = SQLiteCallRepository(settings.db_path)
    worker = AnalysisWorker(
        repo,
        analyzer,
        max_attempts=settings.max_attempts,
        retry_base_s=settings.retry_base_s,
        job_timeout_s=settings.job_timeout_s,
    )
    try:
        call_ids, skipped = await _store_records(repo, files, reanalyze=args.reanalyze)
        info = analyzer.info
        print(f"\nAnalyzing with provider={info.provider} model={info.model} ...")
        await worker.run_until_idle()
        failed = await _print_results(repo, call_ids)
    finally:
        await analyzer.aclose()
        await repo.close()
    print(f"\nDatabase: {settings.db_path}")
    return 1 if skipped or failed else 0


async def _store_records(
    repo: CallRepository, files: list[Path], *, reanalyze: bool
) -> tuple[list[str], int]:
    """Insert each file's record (queueing its analysis). Returns ``(call_ids, skipped)``."""
    call_ids: list[str] = []
    skipped = 0
    for path in files:
        try:
            record = _load_record(path)
        except (OSError, ValidationError) as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        outcome, _ = await repo.insert_call(record)
        if outcome == "conflict":
            print(f"skip {path.name}: a different record with this call_id exists")
            skipped += 1
            continue
        if outcome == "duplicate" and reanalyze:
            await repo.request_analysis(record.call_id)
        call_ids.append(record.call_id)
        print(f"{outcome:9} {record.call_id}  ({path.name})")
    return call_ids, skipped


async def _print_results(repo: CallRepository, call_ids: list[str]) -> int:
    """Print one row per call and return how many analyses failed."""
    failed = 0
    print(f"\n{'call_id':32} {'status':8} {'score':>5}  outcome")
    for call_id in call_ids:
        stored = await repo.get_call(call_id)
        if stored is None:
            continue
        analysis = stored.analysis.to_analysis()
        score = "-" if analysis.overall_score is None else str(analysis.overall_score)
        detail = analysis.outcome.status if analysis.outcome else (analysis.error or "")
        print(f"{call_id:32} {analysis.status:8} {score:>5}  {detail}")
        if analysis.status == "failed":
            failed += 1
    return failed


# --- analyze ------------------------------------------------------------------------------------


def cmd_analyze(args: argparse.Namespace) -> int:
    return asyncio.run(_analyze(args))


async def _analyze(args: argparse.Namespace) -> int:
    record = _load_record(Path(args.file))
    analyzer = CallAnalyzer(build_provider(_settings(args.provider)), default_rubric())
    try:
        analysis = await analyzer.analyze(record)
    finally:
        await analyzer.aclose()
    print(analysis.model_dump_json(indent=2))
    return 0


# --- entry point --------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="call-analyzer", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", help="bind address (default: ANALYZER_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default: ANALYZER_PORT or 8080)")
    serve.set_defaults(func=cmd_serve)

    seed = sub.add_parser("seed", help="load CallRecord JSON files and analyze them")
    seed.add_argument("--dir", default=str(DEFAULT_DEMO_DIR), help="directory of *.json records")
    seed.add_argument("--provider", choices=PROVIDER_CHOICES)
    seed.add_argument(
        "--reanalyze", action="store_true", help="re-run analysis for records already stored"
    )
    seed.set_defaults(func=cmd_seed)

    analyze = sub.add_parser("analyze", help="analyze one CallRecord file and print the result")
    analyze.add_argument("file", help="path to a CallRecord JSON file")
    analyze.add_argument("--provider", choices=PROVIDER_CHOICES)
    analyze.set_defaults(func=cmd_analyze)

    args = parser.parse_args(argv)
    command: Callable[[argparse.Namespace], int] = args.func
    try:
        settings_level = Settings().log_level
    except ValidationError as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings_level)
    try:
        return command(args)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"Invalid call record:\n{exc}", file=sys.stderr)
        return 1
    except ProviderError as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
