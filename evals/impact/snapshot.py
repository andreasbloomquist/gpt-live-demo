"""Fingerprint one source tree into named *components* (see :mod:`evals.impact.rules`).

A snapshot is a flat ``{component_key: Component}`` map. Comparing two snapshots is then a
dictionary diff, and routing a changed key to (suite, tier) pairs is :func:`rules.affects`.
Keeping components fine-grained (per profile, per tool, per setting) is what lets the planner
say *why* a suite runs, and avoid running the ones a change cannot reach.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evals.impact import normalize as norm
from evals.impact import rules

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


@dataclass(frozen=True)
class Component:
    digest: str
    detail: str = ""
    """Human-readable value (setting default, package version) shown in reasons."""
    text: str | None = None
    """Raw rendered prompt, kept only to report +/- line counts."""


@dataclass(frozen=True)
class SuiteInfo:
    name: str
    profile: str
    tiers: tuple[str, ...]
    tools: tuple[str, ...]
    case_tiers: tuple[str, ...]
    """Tiers that at least one case runs on (a suite may enable a tier no case uses yet)."""


@dataclass
class Snapshot:
    label: str
    ok: bool = True
    error: str | None = None
    components: dict[str, Component] = field(default_factory=dict)
    profile_tools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    suites: dict[str, SuiteInfo] = field(default_factory=dict)


# --------------------------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def run_probe(tree: Path, evals_root: Path, python: str = sys.executable) -> dict[str, Any]:
    """Run :mod:`evals.impact.probe` against ``tree`` in a clean subprocess."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG", "VIRTUAL_ENV")
    }
    env.update(
        PYTHONPATH=os.pathsep.join([str(tree / "agent"), str(evals_root)]),
        PROMPTS_DIR=str(tree / "prompts"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    probe_script = evals_root / "evals" / "impact" / "probe.py"
    with tempfile.TemporaryDirectory(prefix="evals-probe-cwd-") as cwd:
        proc = subprocess.run(
            [python, str(probe_script), "--tree", str(tree), "--evals-root", str(evals_root)],
            cwd=cwd,  # empty cwd: no developer .env can leak into Settings
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"ok": False, "error": f"probe exited {proc.returncode}: {proc.stderr[-2000:]}"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"probe emitted invalid JSON: {exc}"}


def load_suite_infos(tree: Path) -> dict[str, SuiteInfo]:
    """Lenient suite parsing (no pydantic) so an *older* tree's suites never break planning."""
    infos: dict[str, SuiteInfo] = {}
    for path in sorted((tree / "evals" / "suites").glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            data = yaml.safe_load(_read(path)) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        tiers = tuple(data.get("tiers") or ())
        case_tiers: set[str] = set()
        for case in data.get("cases") or []:
            if isinstance(case, dict):
                case_tiers.update(case.get("tiers") or tiers)
        name = str(data.get("name") or path.stem)
        if not _SAFE_NAME.match(name):
            # Suite names flow into CI matrices and shell steps: never trust PR-controlled text.
            continue
        infos[name] = SuiteInfo(
            name=name,
            profile=str(data.get("profile", "")),
            tiers=tiers,
            tools=tuple(data.get("tools") or ()),
            case_tiers=tuple(t for t in tiers if t in case_tiers),
        )
    return infos


def _python_files(tree: Path, rel_dir: str) -> list[str]:
    root = tree / rel_dir
    if not root.is_dir():
        return []
    return sorted(
        p.relative_to(tree).as_posix() for p in root.rglob("*.py") if "__pycache__" not in p.parts
    )


def _code_digest(tree: Path, paths: list[str]) -> str:
    return norm.digest({p: norm.normalized_python(_read(tree / p)) for p in sorted(paths)})


def _add_prompt_components(snap: Snapshot, probe: dict[str, Any]) -> None:
    for profile, data in probe.get("profiles", {}).items():
        for target in ("voice", "backend"):
            text = data[target]
            snap.components[f"prompt.{target}:{profile}"] = Component(
                digest=norm.digest(norm.normalize_prompt_for_fingerprint(text)), text=text
            )
        tools = tuple(sorted(data.get("tools", ())))
        snap.profile_tools[profile] = tools
        snap.components[f"profile.tools:{profile}"] = Component(
            digest=norm.digest(list(tools)), detail=", ".join(tools)
        )
    for profile, error in probe.get("profile_errors", {}).items():
        snap.components[f"profile.error:{profile}"] = Component(
            digest=norm.digest(error), detail=error
        )


def _add_tool_components(snap: Snapshot, tree: Path, probe: dict[str, Any]) -> set[str]:
    claimed: set[str] = set()
    for name, entry in probe.get("tools", {}).items():
        schemas = entry.get("schemas")
        error = probe.get("tool_errors", {}).get(name)
        snap.components[f"tool.schema:{name}"] = Component(
            digest=norm.digest(schemas if error is None else f"ERROR {error}"),
            detail=error or "",
        )
        files = [f for f in entry.get("source_files", []) if (tree / f).is_file()]
        claimed.update(files)
        snap.components[f"tool.impl:{name}"] = Component(
            digest=_code_digest(tree, files) if files else norm.digest(entry.get("source_modules"))
        )
    return claimed


def _add_agent_code_components(snap: Snapshot, tree: Path, claimed: set[str]) -> None:
    core: list[str] = []
    backend_runtime: list[str] = []
    voice_runtime: list[str] = []
    for path in _python_files(tree, rules.AGENT_PKG):
        if path.startswith(rules.CORE_CODE):
            core.append(path)
        elif path in claimed:
            continue
        elif path in rules.BACKEND_RUNTIME_CODE:
            backend_runtime.append(path)
        elif path in rules.VOICE_RUNTIME_CODE:
            voice_runtime.append(path)
        elif path == rules.CONFIG_FILE:
            _add_config_components(snap, _read(tree / path))
        else:
            snap.components[f"code.other:{path}"] = Component(
                digest=norm.digest(norm.normalized_python(_read(tree / path)))
            )
    snap.components["code.core"] = Component(digest=_code_digest(tree, core))
    snap.components["code.backend_runtime"] = Component(digest=_code_digest(tree, backend_runtime))
    snap.components["code.voice_runtime"] = Component(digest=_code_digest(tree, voice_runtime))


def _add_config_components(snap: Snapshot, source: str) -> None:
    fields, logic = norm.settings_fields(source)
    displays = _settings_default_display(source)
    for name, value in fields.items():
        kind = rules.config_kind(name)
        if kind is not None:
            snap.components[f"config.{kind}:{name}"] = Component(
                digest=norm.digest(value), detail=displays.get(name, "")
            )
    snap.components["config.logic"] = Component(digest=norm.digest(logic))


def _settings_default_display(source: str) -> dict[str, str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    return {
        node.target.id: ast.unparse(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }


def _add_eval_components(snap: Snapshot, tree: Path) -> None:
    groups: dict[str, list[str]] = {"brain": [], "voice": [], "shared": []}
    for path in _python_files(tree, "evals"):
        group = rules.runner_group(path)
        if group is not None:
            groups[group].append(path)
    for group, paths in groups.items():
        snap.components[f"runner:{group}"] = Component(digest=_code_digest(tree, paths))
    for path in sorted((tree / "evals" / "suites").glob("*.yaml")):
        snap.components[f"suite:{path.stem}"] = Component(
            digest=norm.digest(norm.normalized_yaml(_read(path)))
        )


def _add_dependency_components(snap: Snapshot, tree: Path) -> None:
    lock = tree / "uv.lock"
    if not lock.is_file():
        return
    for pkg, version in norm.lockfile_versions(_read(lock)).items():
        if rules.WATCHED_PACKAGE.match(pkg):
            snap.components[f"deps:{pkg}"] = Component(digest=norm.digest(version), detail=version)


def build_snapshot(
    tree: Path, *, label: str, evals_root: Path, python: str = sys.executable
) -> Snapshot:
    """Fingerprint ``tree``. Never raises for problems *in the tree*: they surface as
    ``ok=False`` (tree-wide) or ``profile.error:*`` / tool errors (local), which the planner
    turns into conservative "run it" decisions."""
    snap = Snapshot(label=label, suites=load_suite_infos(tree))
    _add_eval_components(snap, tree)
    _add_dependency_components(snap, tree)

    probe = run_probe(tree, evals_root, python)
    if not probe.get("ok"):
        snap.ok = False
        snap.error = str(probe.get("error", "unknown probe failure")).strip()
        return snap
    _add_prompt_components(snap, probe)
    claimed = _add_tool_components(snap, tree, probe)
    _add_agent_code_components(snap, tree, claimed)
    return snap
