"""``python -m evals.impact`` — decide which paid evals a change requires.

Examples::

    # What would a PR against main run? (head = working tree, incl. uncommitted changes)
    python -m evals.impact plan --base origin/main

    # CI: compare two commits, write $GITHUB_OUTPUT + step summary, keep the plan for reporting
    python -m evals.impact plan --base origin/main --head HEAD --format github --output plan.json

    # Render every profile and tool schema of a tree (CI prompt-render check)
    python -m evals.impact probe --tree . --strict
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evals.impact import gitutil, output
from evals.impact.planner import Overrides, Plan, fast_path_plan, make_plan
from evals.impact.snapshot import SuiteInfo, build_snapshot, load_suite_infos, run_probe
from evals.paths import REPO_ROOT
from evals.schema import TIERS

FULL_LABEL = "evals:full"
SKIP_LABEL = "evals:skip"
_TRUTHY = {"1", "true", "yes", "on"}

# CI runs the *base* commit's planner (see the `plan` job in .github/workflows/evals.yml), so a
# PR cannot rewrite the rules that judge it. A PR that changes the planner or that bootstrap is
# judged by nobody trustworthy, so it gets everything. This check lives in the base's copy too.
PLANNER_PATHS = ("evals/impact/", ".github/workflows/evals.yml")
PLANNER_CHANGED = "planner code changed: running everything"


def _csv(value: str | None) -> frozenset[str] | None:
    if not value or value.strip().lower() in ("", "all"):
        return None
    return frozenset(v.strip() for v in value.split(",") if v.strip())


def resolve_overrides(args: argparse.Namespace) -> Overrides:
    """Merge CLI flags with env/label-driven overrides.

    Labels arrive via ``--labels`` or ``EVALS_LABELS`` (comma-separated) so the workflow never
    interpolates user-controlled label names into a shell command.
    """
    labels = {
        label.strip()
        for label in (args.labels or os.environ.get("EVALS_LABELS", "")).split(",")
        if label.strip()
    }
    force = None
    if args.force_all:
        force = "--force-all"
    elif os.environ.get("EVALS_FORCE_ALL", "").lower() in _TRUTHY:
        force = os.environ.get("EVALS_FORCE_REASON") or "EVALS_FORCE_ALL"
    elif FULL_LABEL in labels:
        force = f"label `{FULL_LABEL}`"
    skip = f"label `{SKIP_LABEL}`" if SKIP_LABEL in labels else None
    return Overrides(
        force_all=force,
        skip_all=skip,
        only_suites=_csv(args.suites),
        only_tiers=_csv(args.tiers),
    )


class PlanError(ValueError):
    """The request cannot be planned (bad ref, unknown suite/tier filter)."""


def check_filters(overrides: Overrides, suites: dict[str, SuiteInfo]) -> None:
    """A typo in ``--suites``/``--tiers`` (or a workflow input) must fail, not plan nothing and
    report green."""
    unknown_suites = sorted((overrides.only_suites or set()) - set(suites))
    if unknown_suites:
        raise PlanError(f"unknown suite(s) {unknown_suites}; available: {sorted(suites)}")
    unknown_tiers = sorted((overrides.only_tiers or set()) - set(TIERS))
    if unknown_tiers:
        raise PlanError(f"unknown tier(s) {unknown_tiers}; available: {list(TIERS)}")


def build_plan(
    repo: Path,
    base: str,
    head: str | None,
    *,
    overrides: Overrides,
    use_merge_base: bool = True,
    fast_path: bool = True,
    python: str = sys.executable,
) -> Plan:
    """Export base (and head, if a ref) and diff their behavioural fingerprints."""
    head_sha = gitutil.rev_parse(repo, head) if head else "WORKTREE"
    base_sha = gitutil.rev_parse(repo, base)
    if use_merge_base:
        base_sha = gitutil.merge_base(repo, base_sha, head_sha if head else "HEAD")

    changed = gitutil.changed_files(repo, base_sha, head_sha if head else None)
    if not overrides.force_all and any(f.startswith(PLANNER_PATHS) for f in changed):
        overrides = dataclasses.replace(overrides, force_all=PLANNER_CHANGED)
    with tempfile.TemporaryDirectory(prefix="evals-impact-") as tmp:
        head_tree = gitutil.export_tree(repo, head_sha, Path(tmp) / "head") if head else repo
        head_suites = load_suite_infos(head_tree)
        check_filters(overrides, head_suites)
        if fast_path:
            quick = fast_path_plan(
                head_suites,
                base_ref=base_sha,
                head_ref=head_sha,
                changed_files=changed,
                overrides=overrides,
            )
            if quick is not None:
                return quick
        base_tree = gitutil.export_tree(repo, base_sha, Path(tmp) / "base")
        # Both trees are fingerprinted with the *head* checkout's evals code, so the rules and
        # normalizers are identical on both sides.
        evals_root = Path(__file__).resolve().parents[2]
        with ThreadPoolExecutor(max_workers=2) as pool:  # two independent subprocesses
            base_future = pool.submit(
                build_snapshot, base_tree, label="base", evals_root=evals_root, python=python
            )
            head_future = pool.submit(
                build_snapshot, head_tree, label="head", evals_root=evals_root, python=python
            )
            base_snap, head_snap = base_future.result(), head_future.result()
    return make_plan(
        base_snap,
        head_snap,
        base_ref=base_sha,
        head_ref=head_sha,
        overrides=overrides,
        changed_files=changed,
    )


def _cmd_plan(args: argparse.Namespace) -> int:
    repo = gitutil.repo_root(Path(args.repo))
    plan = build_plan(
        repo,
        args.base,
        args.head,
        overrides=resolve_overrides(args),
        use_merge_base=not args.no_merge_base,
        fast_path=not args.no_fast_path,
    )
    if args.output:
        Path(args.output).write_text(output.to_json(plan), encoding="utf-8")
    if args.format == "json":
        print(output.to_json(plan))
    elif args.format == "github":
        output.write_github(plan)
        print(output.to_text(plan))
    else:
        print(output.to_text(plan))
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    tree = Path(args.tree).resolve()
    result = run_probe(tree, Path(__file__).resolve().parents[2])
    errors = [] if result.get("ok") else [result.get("error", "probe failed")]
    errors += [f"profile {k}: {v}" for k, v in result.get("profile_errors", {}).items()]
    errors += [f"tool {k}: {v}" for k, v in result.get("tool_errors", {}).items()]
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for profile, data in sorted(result.get("profiles", {}).items()):
            print(
                f"profile {profile}: voice {len(data['voice'])} chars, "
                f"backend {len(data['backend'])} chars, tools={data['tools']}"
            )
        for name, entry in sorted(result.get("tools", {}).items()):
            n = len(entry.get("schemas") or [])
            print(f"tool {name}: {n} schema(s), sources={entry.get('source_files')}")
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    return 1 if (args.strict and errors) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.impact",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="compute which suites x tiers must run")
    p.add_argument("--base", required=True, help="base ref (e.g. origin/main)")
    p.add_argument("--head", default=None, help="head ref (default: working tree)")
    p.add_argument("--repo", default=str(REPO_ROOT))
    p.add_argument("--format", choices=("text", "json", "github"), default="text")
    p.add_argument("--output", help="also write the plan JSON to this file")
    p.add_argument("--force-all", action="store_true")
    p.add_argument(
        "--labels",
        default=None,
        help=f"comma-separated PR labels ({FULL_LABEL}, {SKIP_LABEL}); defaults to $EVALS_LABELS",
    )
    p.add_argument("--suites", default=None, help="comma-separated suite filter, or 'all'")
    p.add_argument("--tiers", default=None, help="comma-separated tier filter, or 'all'")
    p.add_argument(
        "--no-merge-base",
        action="store_true",
        help="diff against --base directly instead of merge-base(base, head)",
    )
    p.add_argument(
        "--no-fast-path",
        action="store_true",
        help="always fingerprint both trees, even for docs-only diffs",
    )
    p.set_defaults(func=_cmd_plan)

    q = sub.add_parser("probe", help="render prompts + tool schemas of a tree")
    q.add_argument("--tree", default=str(REPO_ROOT))
    q.add_argument("--json", action="store_true")
    q.add_argument("--strict", action="store_true", help="exit 1 on any render error")
    q.set_defaults(func=_cmd_probe)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (PlanError, gitutil.GitError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
