"""Pure unit tests for normalizers, routing rules, planning and GitHub output (no git, no I/O)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.impact import normalize as norm
from evals.impact import output
from evals.impact.planner import Overrides, fast_path_plan, make_plan
from evals.impact.rules import SuiteTarget, affects, config_kind, is_relevant_path
from evals.impact.snapshot import Component, Snapshot, SuiteInfo

# ----------------------------------------------------------------------------- normalizers


def test_prompt_normalization_ignores_layout_but_not_words() -> None:
    a = "# Style\n\nKeep it short and\nfriendly.   \n\n\n\nBe kind.\n"
    b = "# Style\n\nKeep it   short and friendly.\n\nBe kind."
    assert norm.normalize_prompt_for_fingerprint(a) == norm.normalize_prompt_for_fingerprint(b)
    c = "# Style\n\nKeep it short and friendly!\n\nBe kind."
    assert norm.normalize_prompt_for_fingerprint(a) != norm.normalize_prompt_for_fingerprint(c)


def test_prompt_normalization_keeps_list_structure() -> None:
    para = "Rules:\nbe brief\nbe kind"
    bullets = "Rules:\n- be brief\n- be kind"
    assert norm.normalize_prompt_for_fingerprint(para) != norm.normalize_prompt_for_fingerprint(
        bullets
    )
    rewrapped = "Rules:\n- be\n  brief\n- be kind"
    assert norm.normalize_prompt_for_fingerprint(
        rewrapped
    ) == norm.normalize_prompt_for_fingerprint(bullets)


def test_prompt_normalization_keeps_html_comments_that_reach_the_model() -> None:
    # The composer strips comments per module; one opened in a module and closed in a later
    # one (or re-formed by nesting) survives into the rendered prompt the model reads.
    from voice_agent.prompts import normalize_prompt_text

    def rendered(secret: str) -> str:
        return normalize_prompt_text(f"Say <!<!-- x -->-- {secret} -->")

    assert rendered("hi") == "Say <!-- hi -->"
    assert norm.normalize_prompt_for_fingerprint(
        rendered("hi")
    ) != norm.normalize_prompt_for_fingerprint(rendered("bye"))
    joined = "A <!-- note\n\nB is the rule -->"
    assert norm.normalize_prompt_for_fingerprint(joined) != norm.normalize_prompt_for_fingerprint(
        joined.replace("B is", "B is not")
    )


def test_prompt_normalization_keeps_nesting_and_code_blocks() -> None:
    nested = "Rules:\n- be brief\n  - one sentence\n- be kind"
    flat = "Rules:\n- be brief\n- one sentence\n- be kind"
    assert norm.normalize_prompt_for_fingerprint(nested) != norm.normalize_prompt_for_fingerprint(
        flat
    )
    fenced = "Example:\n```\nCaller: hi\nAgent:  hello\n```\nDone."
    joined = "Example:\n```\nCaller: hi Agent: hello\n```\nDone."
    assert norm.normalize_prompt_for_fingerprint(fenced) != norm.normalize_prompt_for_fingerprint(
        joined
    )
    # ...while trailing space inside a fence is still cosmetic.
    assert norm.normalize_prompt_for_fingerprint(fenced) == norm.normalize_prompt_for_fingerprint(
        fenced.replace("hi\n", "hi   \n")
    )


def test_python_normalization_ignores_comments_docstrings_formatting() -> None:
    a = 'def f(x):\n    """Doc."""\n    return x+1  # inc\n'
    b = "def f(x):\n\n    # different comment\n    return (x + 1)\n"
    assert norm.normalized_python(a) == norm.normalized_python(b)
    assert norm.normalized_python(a) != norm.normalized_python("def f(x):\n    return x + 2\n")


def test_python_normalization_of_broken_source_is_stable_but_distinct() -> None:
    assert norm.normalized_python("def (").startswith("UNPARSEABLE:")
    assert norm.normalized_python("def (") != norm.normalized_python("def ((")


def test_settings_fields_split_defaults_from_logic() -> None:
    src = (
        "class Settings(BaseSettings):\n"
        '    """Doc."""\n'
        "    model_config = {}\n"
        '    gpt_live_voice: str = "marin"\n'
        "    timeout: float = Field(default=6.0, gt=0)\n"
        "    def helper(self):\n        return 1\n"
    )
    fields, logic = norm.settings_fields(src)
    assert set(fields) == {"gpt_live_voice", "timeout"}
    fields2, logic2 = norm.settings_fields(src.replace('"marin"', "'cinder'"))
    assert fields2["gpt_live_voice"] != fields["gpt_live_voice"]
    assert logic2 == logic  # a default change is not a logic change
    _, logic3 = norm.settings_fields(src.replace("return 1", "return 2"))
    assert logic3 != logic


def test_yaml_normalization_ignores_comments_and_style() -> None:
    assert norm.normalized_yaml("# c\na: [1, 2]\nb: x\n") == norm.normalized_yaml(
        "b: 'x'\na:\n  - 1\n  - 2\n"
    )


def test_lockfile_versions() -> None:
    lock = (
        'version = 1\n\n[[package]]\nname = "livekit-agents"\nversion = "1.8.3"\n'
        'source = { registry = "x" }\n\n[[package]]\nname = "OpenAI"\nversion = "2.54.0"\n'
    )
    assert norm.lockfile_versions(lock) == {"livekit-agents": "1.8.3", "openai": "2.54.0"}


def test_lockfile_versions_track_git_sources_and_forked_entries() -> None:
    def lock(rev: str, second: str) -> str:
        return (
            f'[[package]]\nname = "livekit-agents"\nversion = "1.8.3"\n'
            f'source = {{ git = "https://github.com/livekit/agents?rev={rev}" }}\n'
            'dependencies = [\n    { name = "openai" },\n]\n\n'
            '[package.optional-dependencies]\nname = "not-a-package"\n\n'
            '[[package]]\nname = "openai"\nversion = "2.54.0"\nsource = { registry = "x" }\n\n'
            f'[[package]]\nname = "openai"\nversion = "{second}"\nsource = {{ registry = "x" }}\n'
        )

    a = norm.lockfile_versions(lock("aaa", "2.10.0"))
    assert set(a) == {"livekit-agents", "openai"}
    assert a != norm.lockfile_versions(lock("bbb", "2.10.0"))  # same version, new git rev
    assert a != norm.lockfile_versions(lock("aaa", "2.11.0"))  # the *second* openai entry


# ----------------------------------------------------------------------------- rules


@pytest.mark.parametrize(
    ("field", "kind"),
    [
        ("gpt_live_voice", "voice"),
        ("gpt_live_model", "voice"),
        ("gpt_live_backend_model", "shared"),
        ("gpt_live_backend_reasoning_effort", "shared"),
        ("gpt_live_backend_max_output_tokens", "shared"),  # not a credential "token"
        ("openai_api_key", None),
        ("livekit_api_secret", None),
        ("opentable_client_secret", None),
        ("github_token", None),
        ("livekit_url", None),
        ("opentable_client_id", None),
        ("log_level", None),
        ("brand_new_setting", "shared"),
    ],
)
def test_config_kind(field: str, kind: str | None) -> None:
    assert config_kind(field) == kind


@pytest.mark.parametrize(
    ("path", "relevant"),
    [
        ("prompts/modules/core/identity.md", True),
        ("agent/voice_agent/tools/web_search.py", True),
        ("uv.lock", True),
        ("evals/suites/web_search.yaml", True),
        ("evals/tests/test_x.py", False),
        ("evals/impact/rules.py", False),
        ("docs/architecture.md", False),
        ("frontend/app/page.tsx", False),
        ("README.md", False),
        (".github/workflows/evals.yml", False),
    ],
)
def test_relevant_paths(path: str, relevant: bool) -> None:
    assert is_relevant_path(path) is relevant


def test_post_call_code_and_settings_run_nothing() -> None:
    s = SuiteTarget("restaurants", "concierge", ("check",), ("check", "web_search"))
    assert not affects("code.post_call", s, "brain")
    assert not affects("code.post_call", s, "voice")
    assert config_kind("call_recording_enabled") is None
    assert config_kind("call_records_dir") is None


def test_affects_routing_table() -> None:
    s = SuiteTarget("restaurants", "concierge", ("check",), ("check", "web_search"))
    assert affects("prompt.voice:concierge", s, "voice")
    assert not affects("prompt.voice:concierge", s, "brain")
    assert not affects("prompt.voice:other", s, "voice")
    assert affects("prompt.backend:concierge", s, "brain")
    assert affects("tool.schema:web_search", s, "brain")  # visible to the model via the profile
    assert not affects("tool.impl:web_search", s, "brain")  # not used by this suite
    assert affects("tool.impl:check", s, "brain")
    assert not affects("tool.impl:check", s, "voice")
    assert affects("code.backend_runtime", s, "brain")
    assert affects("code.backend_runtime", s, "voice")
    assert affects("code.voice_runtime", s, "voice") and not affects(
        "code.voice_runtime", s, "brain"
    )
    assert affects("runner:shared", s, "brain") and affects("runner:voice", s, "voice")
    assert not affects("runner:voice", s, "brain")
    assert affects("suite:restaurants", s, "brain") and not affects("suite:other", s, "brain")
    assert affects("something.new", s, "brain")  # unknown ⇒ conservative


# ----------------------------------------------------------------------------- planner


def _snap(**components: str) -> Snapshot:
    snap = Snapshot(label="t")
    snap.components = {k.replace("__", ":"): Component(digest=v) for k, v in components.items()}
    snap.profile_tools = {"concierge": ("check",)}
    snap.suites = {
        "restaurants": SuiteInfo(
            "restaurants", "concierge", ("brain", "voice"), ("check",), ("brain", "voice")
        ),
        "brain_only": SuiteInfo("brain_only", "concierge", ("brain", "voice"), (), ("brain",)),
    }
    return snap


def test_make_plan_routes_and_fingerprints() -> None:
    base = _snap(**{"prompt.voice__concierge": "a", "tool.impl__check": "x"})
    head = _snap(**{"prompt.voice__concierge": "b", "tool.impl__check": "x"})
    plan = make_plan(base, head, base_ref="b", head_ref="h")
    assert {(r.suite, r.tier) for r in plan.runs} == {("restaurants", "voice")}
    skipped = {(s.suite, s.tier): s.reason for s in plan.skipped}
    assert skipped[("brain_only", "voice")] == "suite has no voice-tier cases"
    # fingerprints are deterministic and differ per tier
    again = make_plan(base, head, base_ref="b", head_ref="h")
    assert plan.runs[0].fingerprint == again.runs[0].fingerprint


def test_make_plan_unrenderable_head_runs_everything() -> None:
    base = _snap(**{"prompt.voice__concierge": "a"})
    head = _snap(**{"prompt.voice__concierge": "a"})
    head.ok, head.error = False, "ImportError: boom"
    plan = make_plan(base, head, base_ref="b", head_ref="h")
    assert {(r.suite, r.tier) for r in plan.runs} == {
        ("restaurants", "brain"),
        ("restaurants", "voice"),
        ("brain_only", "brain"),
    }


def test_fast_path_plan() -> None:
    suites = _snap().suites
    plan = fast_path_plan(
        suites, base_ref="b", head_ref="h", changed_files=["docs/a.md"], overrides=Overrides()
    )
    assert plan is not None and not plan.runs
    assert (
        fast_path_plan(
            suites,
            base_ref="b",
            head_ref="h",
            changed_files=["prompts/x.md"],
            overrides=Overrides(),
        )
        is None
    )
    assert (
        fast_path_plan(
            suites,
            base_ref="b",
            head_ref="h",
            changed_files=["docs/a.md"],
            overrides=Overrides(force_all="x"),
        )
        is None
    )


def test_github_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _snap(**{"prompt.backend__concierge": "a"})
    head = _snap(**{"prompt.backend__concierge": "b"})
    plan = make_plan(base, head, base_ref="b", head_ref="h")
    out, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    output.write_github(plan)
    kv = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert kv["run_brain"] == "true" and kv["run_voice"] == "true" and kv["any"] == "true"
    assert json.loads(kv["brain_matrix"]) == {"suite": ["brain_only", "restaurants"]}
    assert json.loads(kv["voice_matrix"]) == {"suite": ["restaurants"]}
    assert set(json.loads(kv["brain_fingerprints"])) == {"brain_only", "restaurants"}
    md = summary.read_text()
    assert "| `restaurants` | brain | ▶️ run |" in md
    assert json.loads(output.to_json(plan))["summary"]["voice"] == ["restaurants"]


def test_markdown_neutralizes_pr_controlled_text() -> None:
    base = _snap(**{"config.shared__x": "a"})
    head = _snap(**{"config.shared__x": "b"})
    plan = make_plan(base, head, base_ref="b", head_ref="h")
    plan.notes.append('<img src="https://t.example/p.gif"> | row\nbreak')
    md = output.to_markdown(plan)
    assert "<img" not in md and "&lt;img" in md
    assert "\\| row break" in md


def test_unknown_suite_or_tier_filter_is_an_error() -> None:
    from evals.impact.cli import PlanError, check_filters

    suites = _snap().suites
    check_filters(Overrides(only_suites=frozenset({"restaurants"})), suites)
    with pytest.raises(PlanError, match="unknown suite"):
        check_filters(Overrides(only_suites=frozenset({"restaurant"})), suites)
    with pytest.raises(PlanError, match="unknown tier"):
        check_filters(Overrides(only_tiers=frozenset({"brian"})), suites)


def test_probe_timeout_is_a_tree_problem_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from evals.impact import snapshot

    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="probe", timeout=1)

    monkeypatch.setattr(snapshot.subprocess, "run", hang)
    result = snapshot.run_probe(Path("."), Path("."))
    assert result["ok"] is False and "timed out" in result["error"]


def test_describe_change_wording() -> None:
    from evals.impact.planner import describe_change

    base, head = _snap(), _snap(**{"code.other__agent/voice_agent/x.py": "a"})
    assert describe_change("code.other:agent/voice_agent/x.py", base, head) == (
        "agent module `agent/voice_agent/x.py` added"
    )
    assert describe_change("code.other:agent/voice_agent/x.py", head, base).endswith("removed")
    changed = _snap(**{"code.other__agent/voice_agent/x.py": "b"})
    assert describe_change("code.other:agent/voice_agent/x.py", head, changed).endswith(
        "changed (normalized AST)"
    )


def test_force_reason_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from evals.impact.cli import resolve_overrides

    args = argparse.Namespace(labels="", force_all=False, suites=None, tiers=None)
    monkeypatch.setenv("EVALS_FORCE_ALL", "true")
    assert resolve_overrides(args).force_all == "EVALS_FORCE_ALL"
    monkeypatch.setenv("EVALS_FORCE_REASON", "planner code changed: running everything")
    assert resolve_overrides(args).force_all == "planner code changed: running everything"
    monkeypatch.setenv("EVALS_FORCE_ALL", "false")
    assert resolve_overrides(args).force_all is None
