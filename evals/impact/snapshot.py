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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evals.impact import normalize as norm
from evals.impact import rules

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_PROBE_TIMEOUT_S = 180


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
        try:
            proc = subprocess.run(
                [python, str(probe_script), "--tree", str(tree), "--evals-root", str(evals_root)],
                cwd=cwd,  # empty cwd: no developer .env can leak into Settings
                env=env,
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # e.g. an import that blocks: a problem *in the tree*, so conservative, not fatal.
            return {"ok": False, "error": f"probe timed out after {_PROBE_TIMEOUT_S}s"}
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


def _source_files(tree: Path, rel_dir: str) -> list[str]:
    """Every file under ``rel_dir``, not only ``.py``: a data file a module reads (fixtures, a
    JSON table) is behaviour too. Bytecode and hidden files (``.DS_Store``) are skipped."""
    root = tree / rel_dir
    if not root.is_dir():
        return []
    files = []
    for p in root.rglob("*"):
        rel = p.relative_to(tree)
        if not p.is_file() or p.suffix in (".pyc", ".pyo"):
            continue
        if any(part == "__pycache__" or part.startswith(".") for part in rel.parts):
            continue
        files.append(rel.as_posix())
    return sorted(files)


def _normalized_source(tree: Path, path: str) -> str:
    text = _read(tree / path)
    return norm.normalized_python(text) if path.endswith(".py") else text


def _code_digest(tree: Path, paths: Iterable[str]) -> str:
    return norm.digest({p: _normalized_source(tree, p) for p in sorted(paths)})


def _module_file(tree: Path, module: str) -> str | None:
    """``voice_agent.x.y`` → ``agent/voice_agent/x/y.py`` (or its package ``__init__.py``)."""
    base = Path("agent", *module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if (tree / candidate).is_file():
            return candidate.as_posix()
    return None


def _imported_agent_modules(tree: Path, path: str) -> set[str]:
    """``voice_agent`` modules imported by ``path`` (plus their parent packages, which Python
    executes first). Static and lenient: unresolvable imports are simply not followed."""
    try:
        module_ast = ast.parse(_read(tree / path))
    except SyntaxError:
        return set()
    # The package a relative import is resolved against (for ``pkg/__init__.py`` it is ``pkg``).
    package = Path(path).relative_to("agent").parent.parts
    names: set[str] = set()
    for node in ast.walk(module_ast):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package[: len(package) - node.level + 1]
                prefix = ".".join((*anchor, *(node.module.split(".") if node.module else ())))
            else:
                prefix = node.module or ""
            names.add(prefix)
            # ``from pkg import name`` may import a submodule.
            names.update(f"{prefix}.{alias.name}" for alias in node.names)
    modules: set[str] = set()
    for name in names:
        dotted = name.split(".")
        if dotted[0] != "voice_agent":
            continue
        modules.update(".".join(dotted[: i + 1]) for i in range(len(dotted)))
    return modules


def _has_own_route(path: str) -> bool:
    """Core and settings files re-run at least as much as any tool change on their own."""
    return path.startswith(rules.CORE_CODE) or path == rules.CONFIG_FILE


def _transitive_agent_files(tree: Path, roots: Iterable[str]) -> set[str]:
    """``roots`` plus every agent file they (transitively) import, minus core/settings files.

    Core files are not followed either: ``tools/__init__`` imports the registry, which imports
    every tool, and following it would make every tool depend on every other one.
    """
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        path = todo.pop()
        if path in seen or _has_own_route(path):
            continue
        seen.add(path)
        for module in _imported_agent_modules(tree, path):
            found = _module_file(tree, module)
            if found is not None and found not in seen:
                todo.append(found)
    return seen


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
        # A tool also runs the agent code it imports (a helper, another tool's models). Those
        # files keep their own component too; this only widens what re-runs *this* tool's suites.
        impl_files = _transitive_agent_files(tree, files)
        snap.components[f"tool.impl:{name}"] = Component(
            digest=_code_digest(tree, impl_files)
            if impl_files
            else norm.digest(entry.get("source_modules"))
        )
    return claimed


def _add_agent_code_components(snap: Snapshot, tree: Path, claimed: set[str]) -> None:
    core: list[str] = []
    backend_runtime: list[str] = []
    voice_runtime: list[str] = []
    # Fixed roles win over a tool's ``source_modules`` claim: a tool listing e.g.
    # ``voice_agent.config`` must not demote settings changes to "that tool's brain tier".
    for path in _source_files(tree, rules.AGENT_PKG):
        if path.startswith(rules.CORE_CODE):
            core.append(path)
        elif path in rules.BACKEND_RUNTIME_CODE:
            backend_runtime.append(path)
        elif path in rules.VOICE_RUNTIME_CODE:
            voice_runtime.append(path)
        elif path == rules.CONFIG_FILE:
            _add_config_components(snap, _read(tree / path))
        elif path in claimed:
            continue
        else:
            snap.components[f"code.other:{path}"] = Component(
                digest=norm.digest(_normalized_source(tree, path))
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
    for path in _source_files(tree, "evals"):
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
