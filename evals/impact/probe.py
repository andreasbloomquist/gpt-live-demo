"""Render one source tree's model-visible surface: prompts per profile and tool schemas.

Run as a **subprocess** by the detector, once per tree (base and head)::

    python evals/impact/probe.py --tree /tmp/base-export --evals-root /repo

with ``PYTHONPATH=<tree>/agent`` so ``import voice_agent`` resolves to *that tree's* code.
A subprocess (rather than importlib tricks) guarantees the two trees never share module state,
and lets an old/broken base tree fail in isolation: any exception becomes ``{"ok": false}``
and the detector falls back to "everything changed".

Only the head tree's ``evals.toolschema`` is used (via ``--evals-root``) so both trees' tools
are serialised by the *same* converter, which is what makes their schemas comparable.

Prints a single JSON object to stdout. Never touches the network.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# Settings must not pick up a developer's .env or CI env overrides: we fingerprint *code*
# defaults. The detector also runs us from an empty cwd with a scrubbed environment.
_NEUTRAL_ENV = {"RESTAURANT_PROVIDER": "mock"}


def _make_settings() -> Any:
    try:
        config = importlib.import_module("voice_agent.config")
    except Exception:
        return None
    settings_cls = getattr(config, "Settings", None)
    if settings_cls is None:
        return None
    try:
        return settings_cls(_env_file=None)
    except TypeError:
        return settings_cls()


def _module_files(module: str, tree: Path) -> list[str]:
    """Resolve a dotted module to the ``.py`` files implementing it (a package → all files)."""
    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        return []
    origin = Path(spec.origin).resolve()
    files = sorted(origin.parent.rglob("*.py")) if origin.name == "__init__.py" else [origin]
    out = []
    for f in files:
        try:
            out.append(f.resolve().relative_to(tree).as_posix())
        except ValueError:  # resolved outside the tree (shouldn't happen): ignore
            continue
    return out


def _with_greeting(voice: str, greeting: str | None) -> str:
    return voice if not greeting else f"{voice}\n\n[greeting instruction]\n{greeting}"


def probe(tree: Path) -> dict[str, Any]:
    from evals.toolschema import tools_to_responses_schemas

    result: dict[str, Any] = {
        "ok": True,
        "profiles": {},
        "profile_errors": {},
        "tools": {},
        "tool_errors": {},
    }
    settings = _make_settings()

    prompts_pkg = importlib.import_module("voice_agent.prompts")
    composer_cls = getattr(prompts_pkg, "PromptComposer", None)
    if composer_cls is None:
        composer_cls = importlib.import_module("voice_agent.prompts.composer").PromptComposer
    composer = composer_cls(tree / "prompts")
    # No extra variables: the composer renders runtime variables (``today``…) as stable
    # ``<runtime:name>`` placeholders, so base and head render identically on any day.
    for profile in composer.list_profiles():
        try:
            bundle = composer.compose(profile)
        except Exception as exc:
            result["profile_errors"][profile] = f"{type(exc).__name__}: {exc}"
            continue
        result["profiles"][profile] = {
            # The greeting is spoken by the voice model: it is voice-side behaviour too.
            "voice": _with_greeting(bundle.voice_instructions, getattr(bundle, "greeting", None)),
            "backend": bundle.backend_instructions,
            "tools": list(bundle.tools),
        }

    registry = importlib.import_module("voice_agent.tools.registry")
    for name, spec in registry.TOOL_REGISTRY.items():
        entry: dict[str, Any] = {
            "source_modules": list(getattr(spec, "source_modules", ()) or ()),
        }
        entry["source_files"] = sorted(
            {f for m in entry["source_modules"] for f in _module_files(m, tree)}
        )
        try:
            factory = getattr(spec, "factory", None)
            tools = factory(settings) if factory else registry.resolve_tools([name], settings)
            entry["schemas"] = tools_to_responses_schemas(tools)
        except Exception as exc:
            result["tool_errors"][name] = f"{type(exc).__name__}: {exc}"
            entry["schemas"] = None
        result["tools"][name] = entry
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, type=Path)
    parser.add_argument("--evals-root", required=True, type=Path)
    args = parser.parse_args(argv)
    tree = args.tree.resolve()

    os.environ.update(_NEUTRAL_ENV)
    os.environ.setdefault("PROMPTS_DIR", str(tree / "prompts"))
    # Order matters: the tree's agent package must win over any installed/editable copy.
    sys.path[:0] = [str(tree / "agent"), str(args.evals_root.resolve())]

    try:
        out = probe(tree)
    except BaseException as exc:
        out = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=5),
        }
    json.dump(out, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
