"""Result writers (JSON, JUnit XML, Markdown) and the cross-job aggregate report.

JSON is the source of truth (everything, including transcripts); JUnit lets any CI UI show
per-case pass/fail; Markdown goes to ``$GITHUB_STEP_SUMMARY`` and the sticky PR comment.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from evals.runners.common import PASS_HAT_K, SuiteResult

COMMENT_MARKER = "<!-- gpt-live-evals-report -->"


def result_stem(tier: str, suite: str) -> str:
    return f"{tier}__{suite}"


def write_results(result: SuiteResult, results_dir: Path) -> dict[str, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    stem = results_dir / result_stem(result.tier, result.suite)
    paths = {
        "json": stem.with_suffix(".json"),
        "junit": stem.with_suffix(".xml"),
        "markdown": stem.with_suffix(".md"),
    }
    data = result.to_dict()
    paths["json"].write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    paths["junit"].write_text(to_junit(data), encoding="utf-8")
    paths["markdown"].write_text(results_markdown([data]), encoding="utf-8")
    return paths


def to_junit(data: dict[str, Any]) -> str:
    cases = data["cases"]
    suite_el = ET.Element(
        "testsuite",
        name=f"{data['tier']}.{data['suite']}",
        tests=str(len(cases)),
        failures=str(sum(not c["passed"] for c in cases)),
        time=f"{data['duration_s']:.2f}",
    )
    props = ET.SubElement(suite_el, "properties")
    for key in ("status", "cost_usd", "profile", "fingerprint"):
        ET.SubElement(props, "property", name=key, value=str(data.get(key)))
    for case in cases:
        el = ET.SubElement(
            suite_el,
            "testcase",
            classname=f"{data['tier']}.{data['suite']}",
            name=case["case_id"],
            time=f"{sum(t['latency_s'] for t in case['trials']):.2f}",
        )
        if not case["passed"]:
            fails = [
                f"trial {t['trial']}: {_failure(t)}" for t in case["trials"] if not t["passed"]
            ]
            failure = ET.SubElement(
                el,
                "failure",
                message=f"pass rate {case['pass_rate']:.2f} < threshold {case['threshold']:.2f}",
            )
            failure.text = "\n".join(fails)
        out = ET.SubElement(el, "system-out")
        out.text = "\n\n".join(
            f"--- trial {t['trial']} ({'PASS' if t['passed'] else 'FAIL'})\n{t['transcript']}"
            for t in case["trials"]
        )
    if data["status"] == "budget_exceeded":
        ET.SubElement(suite_el, "system-err").text = data["status_detail"]
    ET.indent(suite_el)
    return ET.tostring(suite_el, encoding="unicode", xml_declaration=True)


def _failure(trial: dict[str, Any]) -> str:
    if trial.get("error"):
        return f"error: {trial['error']}"
    parts = [f"{c['name']}: {c['detail']}" for c in trial["checks"] if not c["passed"]]
    judge = trial.get("judge")
    if judge and not judge["passed"]:
        parts.append(f"judge: {judge['reason']}")
    return "; ".join(parts) or "failed"


def _median(values: list[float]) -> str:
    return f"{statistics.median(values):.2f}s" if values else "—"


def results_markdown(results: list[dict[str, Any]]) -> str:
    if not results:
        return "_No eval results._\n"
    lines = [
        f"| tier | suite | result | cases | mean pass rate | min pass^{PASS_HAT_K} | p50 latency | "
        "p50 first response | cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    failures: list[str] = []
    for data in sorted(results, key=lambda d: (d["tier"] != "brain", d["suite"])):
        cases = data["cases"]
        trials = [t for c in cases for t in c["trials"]]
        rates = [c["pass_rate"] for c in cases]
        hats = [c["pass_hat_k"] for c in cases if c.get("pass_hat_k") is not None]
        icon = {"completed": "✅" if data["passed"] else "❌", "skipped": "⏭️"}.get(
            data["status"], "⚠️"
        )
        status = data["status"] if data["status"] != "completed" else ""
        lines.append(
            f"| {data['tier']} | `{data['suite']}` | {icon} {status} | "
            f"{sum(c['passed'] for c in cases)}/{len(cases)} | "
            f"{(sum(rates) / len(rates)) if rates else 0:.0%} | "
            f"{f'{min(hats):.2f}' if hats else '—'} | "
            f"{_median([t['latency_s'] for t in trials])} | "
            f"{_median([t['first_response_latency_s'] for t in trials if t.get('first_response_latency_s') is not None])} | "  # noqa: E501
            f"${data['cost_usd']:.3f} |"
        )
        for case in cases:
            if not case["passed"]:
                first_fail = next((t for t in case["trials"] if not t["passed"]), None)
                why = _failure(first_fail) if first_fail else "no trials ran"
                failures.append(
                    f"- **{data['tier']}/{data['suite']}/{case['case_id']}** "
                    f"({case['pass_rate']:.0%}): {why[:300]}"
                )
        if data["status_detail"]:
            failures.append(f"- **{data['tier']}/{data['suite']}**: {data['status_detail']}")
    total = sum(d["cost_usd"] for d in results)
    out = "\n".join(lines) + f"\n\nTotal eval spend: **${total:.3f}**\n"
    if failures:
        out += "\n<details><summary>Failures</summary>\n\n" + "\n".join(failures) + "\n</details>\n"
    return out


def load_results(results_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(results_dir.rglob("*__*.json"))
    ]


def aggregate_markdown(
    results_dir: Path, plan_path: Path | None = None, notes: list[str] | None = None
) -> str:
    """The sticky PR comment / job summary: what ran, what was skipped and why, and results."""
    from evals.impact.output import to_markdown as plan_markdown
    from evals.impact.planner import Plan, PlannedRun, SkippedRun

    parts = [COMMENT_MARKER, "## 🎙️ GPT-Live eval report", ""]
    parts += [f"> {n}" for n in notes or []]
    if notes:
        parts.append("")
    parts += ["### Results", "", results_markdown(load_results(results_dir))]
    if plan_path is not None and plan_path.is_file():
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = Plan(
            base=raw["base"],
            head=raw["head"],
            runs=[PlannedRun(**r) for r in raw["runs"]],
            skipped=[SkippedRun(**s) for s in raw["skipped"]],
            notes=raw.get("notes", []),
        )
        parts += [
            "<details><summary>Change-impact plan "
            f"(base <code>{plan.base[:10]}</code> → head <code>{plan.head[:10]}</code>)"
            "</summary>\n",
            plan_markdown(plan),
            "</details>",
        ]
    return "\n".join(parts) + "\n"
