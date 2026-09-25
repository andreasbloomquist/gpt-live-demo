"""End-to-end detector tests on real git repositories (base commit vs. working tree / commit).

Each test makes one kind of edit and asserts the exact set of (suite, tier) pairs planned.
All suites in this repo use the ``concierge`` profile; tool usage differs:

* restaurant_availability → check_restaurant_availability
* web_search              → web_search
* conversation_style      → both
"""

from __future__ import annotations

import re
from pathlib import Path

from evals.impact.cli import build_plan
from evals.impact.planner import Overrides, Plan
from evals.tests.conftest import commit_all

ALL_SUITES = {"restaurant_availability", "web_search", "conversation_style"}
VOICE_STYLE = "prompts/modules/core/voice_style.md"
TOOL_POLICY = "prompts/modules/backend/tool_policy.md"
RESTAURANT_TOOL = "agent/voice_agent/tools/restaurants/tool.py"
MOCK_PROVIDER = "agent/voice_agent/tools/restaurants/mock.py"


def plan_for(repo: Path, overrides: Overrides = Overrides(), **kw: object) -> Plan:  # noqa: B008
    return build_plan(repo, "HEAD", None, overrides=overrides, **kw)  # type: ignore[arg-type]


def planned(plan: Plan) -> set[tuple[str, str]]:
    return {(r.suite, r.tier) for r in plan.runs}


def every(tier: str, suites: set[str] = ALL_SUITES) -> set[tuple[str, str]]:
    return {(s, tier) for s in suites}


def test_no_change_runs_nothing(repo: Path) -> None:
    assert planned(plan_for(repo, fast_path=False)) == set()


def test_whitespace_and_comment_only_prompt_edit_runs_nothing(repo: Path, edit) -> None:
    def reformat(text: str) -> str:
        # re-wrap a bullet at a different column, add trailing spaces, blank lines, a comment
        text = text.replace(
            "- Keep turns short: usually one or two sentences, then hand the turn back. Offer more"
            " detail only\n  if the caller wants it.",
            "- Keep turns short:   usually one or two sentences, then hand the turn\n"
            "  back. Offer more detail only if the caller wants it.   ",
        )
        return text.replace(
            "# Timing and turn-taking", "\n\n<!-- reviewed -->\n# Timing and turn-taking"
        )

    edit(VOICE_STYLE, reformat)
    plan = plan_for(repo)
    assert planned(plan) == set(), plan.runs
    assert all("no behaviour-affecting change" in s.reason for s in plan.skipped)


def test_voice_prompt_wording_runs_voice_tier_only(repo: Path, edit) -> None:
    edit(
        VOICE_STYLE,
        lambda t: t.replace(
            "Use contractions and plain words.", "Use contractions, plain words and a warm tone."
        ),
    )
    plan = plan_for(repo)
    assert planned(plan) == every("voice")
    reasons = " ".join(r for run in plan.runs for r in run.reasons)
    assert "voice prompt of profile `concierge` changed (+1/-1 normalized lines)" in reasons


def test_backend_prompt_runs_brain_and_voice(repo: Path, edit) -> None:
    edit(TOOL_POLICY, lambda t: t.rstrip() + "\n\nPrefer the most recent sources.\n")
    assert planned(plan_for(repo)) == every("brain") | every("voice")


def test_tool_schema_change_runs_brain_and_voice(repo: Path, edit) -> None:
    # The docstring is the tool description the model sees → schema change, not an impl change.
    edit(
        RESTAURANT_TOOL,
        lambda t: t.replace(
            "Check whether a restaurant has a table", "Check if a restaurant has a free table"
        ),
    )
    plan = plan_for(repo)
    assert planned(plan) == every("brain") | every("voice")
    assert "tool.schema:check_restaurant_availability" in plan.changed_components
    assert "tool.impl:check_restaurant_availability" not in plan.changed_components


def test_tool_impl_change_runs_brain_for_suites_using_that_tool(repo: Path, edit) -> None:
    edit(
        MOCK_PROVIDER,
        lambda t: t.replace(
            "def _to_minutes(value: dt.time) -> int:\n    return value.hour * 60 + value.minute",
            "def _to_minutes(value: dt.time) -> int:\n"
            "    return int(value.hour * 60 + value.minute)",
        ),
    )
    plan = plan_for(repo)
    assert planned(plan) == {
        ("restaurant_availability", "brain"),
        ("conversation_style", "brain"),
    }
    assert plan.changed_components == ["tool.impl:check_restaurant_availability"]


def test_tool_impl_comment_and_docstring_edit_runs_nothing(repo: Path, edit) -> None:
    edit(
        MOCK_PROVIDER,
        lambda t: t.replace(
            "def _to_minutes(value: dt.time) -> int:\n",
            "def _to_minutes(value: dt.time) -> int:  # minutes since midnight\n"
            '    """Minutes since midnight."""\n',
        ),
    )
    assert planned(plan_for(repo)) == set()


def test_docs_and_frontend_changes_take_the_fast_path(repo: Path) -> None:
    (repo / "docs" / "architecture.md").write_text("# Architecture\n\nMore words.\n")
    (repo / "frontend" / "page.tsx").write_text("export default function Page() { return 1 }\n")
    (repo / "README.md").write_text("hello\n")
    plan = plan_for(repo)
    assert planned(plan) == set()
    assert plan.notes and plan.notes[0].startswith("fast path")
    assert {s.suite for s in plan.skipped} == ALL_SUITES


def test_voice_setting_default_runs_voice_only(repo: Path, edit) -> None:
    edit(
        "agent/voice_agent/config.py",
        lambda t: t.replace('gpt_live_voice: str = "marin"', 'gpt_live_voice: str = "cinder"'),
    )
    plan = plan_for(repo)
    assert planned(plan) == every("voice")
    assert any("'marin' → 'cinder'" in r for run in plan.runs for r in run.reasons)


def test_backend_setting_default_runs_both(repo: Path, edit) -> None:
    edit(
        "agent/voice_agent/config.py",
        lambda t: re.sub(r'(gpt_live_backend_reasoning_effort: \w+ = )"low"', r'\1"medium"', t),
    )
    assert planned(plan_for(repo)) == every("brain") | every("voice")


def test_secret_setting_change_runs_nothing(repo: Path, edit) -> None:
    edit(
        "agent/voice_agent/config.py",
        lambda t: t.replace('log_level: str = "INFO"', 'log_level: str = "DEBUG"'),
    )
    assert planned(plan_for(repo)) == set()


def test_lockfile_bump_of_livekit_runs_everything(repo: Path, edit) -> None:
    edit(
        "uv.lock",
        lambda t: t.replace(
            'name = "livekit-agents"\nversion = "1.8.3"',
            'name = "livekit-agents"\nversion = "1.8.4"',
        ),
    )
    plan = plan_for(repo)
    assert planned(plan) == every("brain") | every("voice")
    assert "deps:livekit-agents" in plan.changed_components


def test_suite_edit_runs_only_that_suite(repo: Path, edit) -> None:
    edit("evals/suites/web_search.yaml", lambda t: t.replace("trials: 3", "trials: 5"))
    assert planned(plan_for(repo)) == {("web_search", "brain"), ("web_search", "voice")}


def test_suite_comment_edit_runs_nothing(repo: Path, edit) -> None:
    edit("evals/suites/web_search.yaml", lambda t: "# reviewed\n" + t)
    assert planned(plan_for(repo)) == set()


def test_voice_runner_change_runs_voice_only(repo: Path, edit) -> None:
    edit("evals/runners/voice.py", lambda t: t.replace("SETTLE_S = 2.5", "SETTLE_S = 3.0"))
    assert planned(plan_for(repo)) == every("voice")


def test_composer_change_runs_everything(repo: Path, edit) -> None:
    edit("agent/voice_agent/prompts/composer.py", lambda t: t + "\n\n_UNUSED_FLAG = True\n")
    assert planned(plan_for(repo)) == every("brain") | every("voice")


def test_unrenderable_base_treats_everything_as_changed(repo: Path) -> None:
    # Base commit where the composer does not exist yet (e.g. the PR that introduces it).
    composer = repo / "agent/voice_agent/prompts/composer.py"
    source = composer.read_text()
    composer.unlink()
    commit_all(repo, "base without composer")
    composer.write_text(source)
    plan = plan_for(repo)
    assert planned(plan) == every("brain") | every("voice")
    assert any("base tree could not be fingerprinted" in n for n in plan.notes)


def test_head_as_commit_ref(repo: Path, edit) -> None:
    base = repo / ".git"
    assert base.exists()
    edit(VOICE_STYLE, lambda t: t.replace("Use contractions and plain words.", "Use plain words."))
    commit_all(repo, "voice tweak")
    plan = build_plan(repo, "HEAD~1", "HEAD", overrides=Overrides())
    assert planned(plan) == every("voice")


def test_overrides(repo: Path) -> None:
    forced = plan_for(repo, Overrides(force_all="label `evals:full`"))
    assert planned(forced) == every("brain") | every("voice")
    assert all(r.reasons[0] == "forced: label `evals:full`" for r in forced.runs)

    skipped = plan_for(repo, Overrides(force_all="x", skip_all="label `evals:skip`"))
    assert planned(skipped) == set()

    filtered = plan_for(
        repo,
        Overrides(
            force_all="x", only_suites=frozenset({"web_search"}), only_tiers=frozenset({"brain"})
        ),
    )
    assert planned(filtered) == {("web_search", "brain")}


def test_backend_options_in_model_py_run_both_tiers(repo: Path, edit) -> None:
    # build_responses_options (model.py) is the brain tier's backend config, not voice-only code.
    edit(
        "agent/voice_agent/model.py",
        lambda t: t.replace("parallel_tool_calls=True", "parallel_tool_calls=False"),
    )
    plan = plan_for(repo)
    assert planned(plan) == every("brain") | every("voice")
    assert plan.changed_components == ["code.backend_runtime"]


def test_voice_only_runtime_change_runs_voice_only(repo: Path, edit) -> None:
    edit("agent/voice_agent/agent.py", lambda t: t + "\n\n_EVALS_MARKER = 1\n")
    assert planned(plan_for(repo)) == every("voice")
