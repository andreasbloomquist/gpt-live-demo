"""Compare two snapshots and decide which (suite, tier) pairs must run, and why."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from evals.impact import normalize as norm
from evals.impact import rules
from evals.impact.snapshot import Snapshot, SuiteInfo
from evals.schema import TIERS, Tier


@dataclass(frozen=True)
class PlannedRun:
    suite: str
    tier: Tier
    reasons: list[str]
    fingerprint: str
    """Behavioral fingerprint of (suite, tier) at head: a hash of every component that can
    affect it. Two commits with equal fingerprints get equal eval results (modulo sampling),
    so it doubles as a cache key for "already evaluated this exact behavior"."""


@dataclass(frozen=True)
class SkippedRun:
    suite: str
    tier: Tier | None
    reason: str


@dataclass
class Plan:
    base: str
    head: str
    runs: list[PlannedRun] = field(default_factory=list)
    skipped: list[SkippedRun] = field(default_factory=list)
    changed_components: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def runs_for(self, tier: Tier) -> list[PlannedRun]:
        return [r for r in self.runs if r.tier == tier]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["summary"] = {t: sorted(r.suite for r in self.runs_for(t)) for t in TIERS}
        return data


@dataclass(frozen=True)
class Overrides:
    force_all: str | None = None
    """Reason string when everything must run (``--force-all``, ``evals:full``, schedule)."""
    skip_all: str | None = None
    """Reason string when nothing may run (``evals:skip``). Wins over ``force_all``."""
    only_suites: frozenset[str] | None = None
    """Restrict the plan to these suites (``None``: all)."""
    only_tiers: frozenset[str] | None = None
    """Restrict the plan to these tiers (``None``: all)."""


def describe_change(key: str, base: Snapshot, head: Snapshot) -> str:
    """One human-readable line for a changed component (goes into the PR comment)."""
    b, h = base.components.get(key), head.components.get(key)
    kind, _, arg = key.partition(":")
    verb = "added" if b is None else "removed" if h is None else "changed"
    ast_verb = verb if verb != "changed" else "changed (normalized AST)"
    if kind in ("prompt.voice", "prompt.backend"):
        target = kind.split(".")[1]
        stats = ""
        if b is not None and h is not None and b.text is not None and h.text is not None:
            plus, minus = norm.line_diff_stats(b.text, h.text)
            stats = f" (+{plus}/-{minus} normalized lines)"
        return f"{target} prompt of profile `{arg}` {verb}{stats}"
    if kind == "profile.tools":
        return (
            f"tool list of profile `{arg}` {verb}: "
            f"[{b.detail if b else ''}] → [{h.detail if h else ''}]"
        )
    if kind == "profile.error":
        return f"profile `{arg}` fails to render at {'head' if h else 'base'}: " + (
            h.detail if h else b.detail if b else ""
        )
    if kind == "tool.schema":
        return f"model-visible schema of tool `{arg}` {verb}"
    if kind == "tool.impl":
        return f"implementation of tool `{arg}` {ast_verb}"
    if kind == "code.core":
        return f"prompt composer / tool registry code {ast_verb}"
    if kind == "code.other":
        return f"agent module `{arg}` {ast_verb}"
    if kind == "code.backend_runtime":
        return f"model construction code (model.py, incl. backend options) {ast_verb}"
    if kind == "code.voice_runtime":
        return f"voice runtime code (agent.py/main.py) {ast_verb}"
    if kind == "config.logic":
        return f"settings module logic {ast_verb}"
    if kind in ("config.voice", "config.shared"):
        return f"setting `{arg}` default: {b.detail if b else '∅'} → {h.detail if h else '∅'}"
    if kind == "suite":
        return f"suite definition `{arg}.yaml` {verb}"
    if kind == "runner":
        return f"{arg} eval runner code {ast_verb}"
    if kind == "deps":
        return f"dependency `{arg}`: {b.detail if b else '∅'} → {h.detail if h else '∅'}"
    return f"{key} {verb}"


def changed_keys(base: Snapshot, head: Snapshot) -> list[str]:
    """Component keys added, removed, or with a different digest between the snapshots."""

    def digest(snap: Snapshot, key: str) -> str | None:
        component = snap.components.get(key)
        return component.digest if component is not None else None

    keys = set(base.components) | set(head.components)
    return sorted(k for k in keys if digest(base, k) != digest(head, k))


def _target(info: SuiteInfo, base: Snapshot, head: Snapshot) -> rules.SuiteTarget:
    profile_tools = set(head.profile_tools.get(info.profile, ())) | set(
        base.profile_tools.get(info.profile, ())
    )
    return rules.SuiteTarget(
        name=info.name, profile=info.profile, tools=info.tools, profile_tools=profile_tools
    )


def _fingerprint(head: Snapshot, target: rules.SuiteTarget, tier: Tier) -> str:
    relevant = {k: c.digest for k, c in head.components.items() if rules.affects(k, target, tier)}
    return norm.digest(relevant)


def _pairs(suites: dict[str, SuiteInfo]) -> Iterable[tuple[SuiteInfo, Tier]]:
    for name in sorted(suites):
        info = suites[name]
        for tier in TIERS:
            if tier in info.tiers:
                yield info, tier


def make_plan(
    base: Snapshot,
    head: Snapshot,
    *,
    base_ref: str,
    head_ref: str,
    overrides: Overrides = Overrides(),  # noqa: B008 - frozen dataclass, safe default
    changed_files: list[str] | None = None,
) -> Plan:
    plan = Plan(base=base_ref, head=head_ref, changed_files=list(changed_files or []))

    blanket: str | None = None
    if not head.ok:
        blanket = f"head tree could not be fingerprinted ({head.error}); running everything"
    elif not base.ok:
        blanket = (
            f"base tree could not be fingerprinted ({base.error}); treating everything as changed"
        )
    if blanket:
        plan.notes.append(blanket)
    if overrides.force_all:
        plan.notes.append(f"forced full run: {overrides.force_all}")

    keys = changed_keys(base, head)
    plan.changed_components = keys

    for info, tier in _pairs(head.suites):
        if overrides.skip_all:
            plan.skipped.append(SkippedRun(info.name, tier, overrides.skip_all))
            continue
        if overrides.only_suites is not None and info.name not in overrides.only_suites:
            plan.skipped.append(SkippedRun(info.name, tier, "not selected (suite filter)"))
            continue
        if overrides.only_tiers is not None and tier not in overrides.only_tiers:
            plan.skipped.append(SkippedRun(info.name, tier, "not selected (tier filter)"))
            continue
        if tier not in info.case_tiers:
            plan.skipped.append(SkippedRun(info.name, tier, f"suite has no {tier}-tier cases"))
            continue

        target = _target(info, base, head)
        fingerprint = _fingerprint(head, target, tier)
        reasons = [describe_change(k, base, head) for k in keys if rules.affects(k, target, tier)]
        if overrides.force_all:
            reasons = [f"forced: {overrides.force_all}", *reasons]
        elif blanket:
            reasons = [blanket, *reasons]
        if reasons:
            plan.runs.append(PlannedRun(info.name, tier, reasons, fingerprint))
        else:
            plan.skipped.append(
                SkippedRun(info.name, tier, "no behavior-affecting change for this tier")
            )

    for name in sorted(set(base.suites) - set(head.suites)):
        plan.skipped.append(SkippedRun(name, None, "suite removed"))
    return plan


def fast_path_plan(
    head_suites: dict[str, SuiteInfo],
    *,
    base_ref: str,
    head_ref: str,
    changed_files: list[str],
    overrides: Overrides,
) -> Plan | None:
    """Skip all work when no changed file can possibly affect behavior (docs, frontend, CI).

    Returns ``None`` when the full semantic comparison is needed.
    """
    if overrides.force_all or any(rules.is_relevant_path(f) for f in changed_files):
        return None
    plan = Plan(base=base_ref, head=head_ref, changed_files=changed_files)
    reason = overrides.skip_all or (
        f"no behavior-relevant files changed ({len(changed_files)} file(s): "
        + ", ".join(changed_files[:5])
        + (" …" if len(changed_files) > 5 else "")
        + ")"
    )
    plan.notes.append("fast path: " + reason)
    for info, tier in _pairs(head_suites):
        plan.skipped.append(SkippedRun(info.name, tier, reason))
    return plan
