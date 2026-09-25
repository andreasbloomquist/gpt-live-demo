"""Render a :class:`Plan` for humans (text/markdown) and machines (JSON, GitHub outputs)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from evals.impact.planner import Plan
from evals.schema import TIERS


def to_json(plan: Plan) -> str:
    return json.dumps(plan.to_dict(), indent=2, sort_keys=True)


def to_text(plan: Plan) -> str:
    lines = [f"change-impact plan  base={plan.base[:12]}  head={plan.head[:12]}"]
    lines += [f"  note: {n}" for n in plan.notes]
    if not plan.runs:
        lines.append("  nothing to run")
    for run in plan.runs:
        lines.append(f"  RUN  {run.tier:<5} {run.suite}  [fp {run.fingerprint}]")
        lines += [f"         - {r}" for r in run.reasons]
    for skip in plan.skipped:
        lines.append(f"  skip {skip.tier or '-':<5} {skip.suite}: {skip.reason}")
    return "\n".join(lines)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown(plan: Plan) -> str:
    """Summary table for ``$GITHUB_STEP_SUMMARY`` and the PR comment."""
    out = ["### Eval plan", ""]
    out += [f"> {_md_escape(n)}" for n in plan.notes]
    if plan.notes:
        out.append("")
    out += ["| suite | tier | decision | why |", "|---|---|---|---|"]
    for run in plan.runs:
        why = "<br>".join(_md_escape(r) for r in run.reasons[:6])
        if len(run.reasons) > 6:
            why += f"<br>… +{len(run.reasons) - 6} more"
        out.append(f"| `{run.suite}` | {run.tier} | ▶️ run | {why} |")
    for skip in plan.skipped:
        out.append(f"| `{skip.suite}` | {skip.tier or '—'} | ⏭️ skip | {_md_escape(skip.reason)} |")
    if not plan.runs and not plan.skipped:
        out.append("| — | — | — | no suites found |")
    return "\n".join(out) + "\n"


def github_outputs(plan: Plan) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for tier in TIERS:
        suites = sorted({r.suite for r in plan.runs_for(tier)})
        outputs[f"{tier}_matrix"] = json.dumps({"suite": suites}, separators=(",", ":"))
        outputs[f"run_{tier}"] = "true" if suites else "false"
        outputs[f"{tier}_fingerprints"] = json.dumps(
            {r.suite: r.fingerprint for r in plan.runs_for(tier)}, separators=(",", ":")
        )
    outputs["any"] = "true" if plan.runs else "false"
    return outputs


def write_github(plan: Plan) -> None:
    """Append to ``$GITHUB_OUTPUT`` and ``$GITHUB_STEP_SUMMARY`` (no-ops outside Actions)."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as fh:
            for key, value in github_outputs(plan).items():
                fh.write(f"{key}={value}\n")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as fh:
            fh.write(to_markdown(plan))
