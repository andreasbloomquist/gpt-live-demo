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
    """Behavioural fingerprint of (suite, tier) at head: a hash of every component that can
    affect it. Two commits with equal fingerprints get equal eval results (modulo sampling),
    so it doubles as a cache key for "already evaluated this exact behaviour"."""


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
    only_tiers: frozenset[str] | None = None


# --------------------------------------------------------------------------------------------


def describe_change(key: str, base: Snapshot, head: Snapshot) -> str:
    """One human-readable line for a changed component (goes into the PR comment)."""
    b, h = base.components.get(key), head.components.get(key)
    kind, _, arg = key.partition(":")
    if b is None and h is not None and kind not in ("profile.error",):
        prefix = "added: "
    elif h is None and b is not None and kind not in ("profile.error",):
        prefix = "removed: "
    else:
        prefix = ""
    if kind in ("prompt.voice", "prompt.backend"):
        target = kind.split(".")[1]
        stats = ""
        if b is not None and h is not None and b.text is not None and h.text is not None:
            plus, minus = norm.line_diff_stats(b.text, h.text)
            stats = f" (+{plus}/-{minus} normalized lines)"
        return f"{prefix}{target} prompt of profile `{arg}` changed{stats}"
    if kind == "profile.tools":
        return (
            f"{prefix}tool list of profile `{arg}` changed: "
            f"[{b.detail if b else ''}] → [{h.detail if h else ''}]"
        )
    if kind == "profile.error":
        return f"profile `{arg}` fails to render at {'head' if h else 'base'}: " + (
            h.detail if h else b.detail if b else ""
        )
    if kind == "tool.schema":
        return f"{prefix}model-visible schema of tool `{arg}` changed"
    if kind == "tool.impl":
        return f"{prefix}implementation of tool `{arg}` changed (normalized AST)"
    if kind == "code.core":
        return "prompt composer / tool registry code changed (normalized AST)"
    if kind == "code.other":
        return f"{prefix}agent module `{arg}` changed (normalized AST)"
    if kind == "code.voice_runtime":
        return "voice runtime code (agent/model/main) changed (normalized AST)"
    if kind.startswith("config."):
        if kind == "config.logic":
            return "settings module logic changed (normalized AST)"
        return f"setting `{arg}` default: {b.detail if b else '∅'} → {h.detail if h else '∅'}"
    if kind == "suite":
        return f"{prefix}suite definition `{arg}.yaml` changed"
    if kind == "runner":
        return f"{arg} eval runner code changed (normalized AST)"
    if kind == "deps":
        return f"dependency `{arg}`: {b.detail if b else '∅'} → {h.detail if h else '∅'}"
    return f"{prefix}{key} changed"


def changed_keys(base: Snapshot, head: Snapshot) -> list[str]:
    keys = set(base.components) | set(head.components)
    return sorted(
        k
        for k in keys
        if (base.components.get(k) or _MISSING).digest
        != (head.components.get(k) or _MISSING).digest
    )


class _Missing:
    digest = "<missing>"


_MISSING = _Missing()


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
                SkippedRun(info.name, tier, "no behaviour-affecting change for this tier")
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
    """Skip all work when no changed file can possibly affect behaviour (docs, frontend, CI).

    Returns ``None`` when the full semantic comparison is needed.
    """
    if overrides.force_all or any(rules.is_relevant_path(f) for f in changed_files):
        return None
    plan = Plan(base=base_ref, head=head_ref, changed_files=changed_files)
    reason = overrides.skip_all or (
        f"no behaviour-relevant files changed ({len(changed_files)} file(s): "
        + ", ".join(changed_files[:5])
        + (" …" if len(changed_files) > 5 else "")
        + ")"
    )
    plan.notes.append("fast path: " + reason)
    for info, tier in _pairs(head_suites):
        plan.skipped.append(SkippedRun(info.name, tier, reason))
    return plan
