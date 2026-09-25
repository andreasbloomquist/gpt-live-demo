"""Tiny CLI for inspecting composed prompts without starting an agent.

Examples::

    uv run python -m voice_agent.prompts list
    uv run python -m voice_agent.prompts render concierge
    uv run python -m voice_agent.prompts render concierge --target backend
    uv run python -m voice_agent.prompts render concierge --json --var today=2026-01-15
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .composer import PromptComposer, PromptCompositionError, Target


def _parse_vars(pairs: Sequence[str]) -> dict[str, str]:
    """Turn repeated ``--var NAME=VALUE`` options into a dict (later values win)."""
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--var expects NAME=VALUE, got {pair!r}")
        out[key] = value
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI; returns the process exit status."""
    parser = argparse.ArgumentParser(prog="python -m voice_agent.prompts")
    parser.add_argument(
        "--prompts-dir",
        type=Path,
        default=None,
        help="prompts directory (default: $PROMPTS_DIR, else the repo's prompts/)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list profiles")
    render = sub.add_parser("render", help="render a profile")
    render.add_argument("profile")
    render.add_argument("--target", choices=["voice", "backend"], default=None)
    render.add_argument("--json", action="store_true", help="emit the full bundle as JSON")
    render.add_argument(
        "--var", action="append", default=[], metavar="NAME=VALUE", help="extra variable"
    )
    args = parser.parse_args(argv)

    try:
        composer = PromptComposer(args.prompts_dir)
        if args.command == "list":
            for name in composer.list_profiles():
                desc = composer.manifest["profiles"][name].get("description", "")
                print(f"{name}\t{desc}")
            return 0
        bundle = composer.compose(args.profile, extra_variables=_parse_vars(args.var))
    except PromptCompositionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "profile": bundle.profile,
            "fingerprint": bundle.fingerprint,
            "version": bundle.version,
            "fingerprints": dict(bundle.target_fingerprints),
            "tools": list(bundle.tools),
            "modules": {k: list(v) for k, v in bundle.modules.items()},
            "greeting": bundle.greeting,
            "voice_instructions": bundle.voice_instructions,
            "backend_instructions": bundle.backend_instructions,
        }
        if args.target:
            payload = {
                k: v
                for k, v in payload.items()
                if not k.endswith("_instructions") or k.startswith(args.target)
            }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    sections: list[Target] = [args.target] if args.target else ["voice", "backend"]
    print(f"# profile={bundle.profile} fingerprint={bundle.version} tools={','.join(bundle.tools)}")
    for target in sections:
        text = bundle.voice_instructions if target == "voice" else bundle.backend_instructions
        print(f"\n===== {target} ({bundle.short_fingerprint_for(target)}) =====\n")
        print(text)
    if bundle.greeting and args.target in (None, "voice"):
        print(f"\n===== greeting =====\n\n{bundle.greeting}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
