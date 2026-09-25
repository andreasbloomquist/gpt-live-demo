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
    a = "# Style\n\nKeep it short and\nfriendly.   \n\n\n\n<!-- note -->Be kind.\n"
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


# ----------------------------------------------------------------------------- rules


@pytest.mark.parametrize(
    ("field", "kind"),
    [
        ("gpt_live_voice", "voice"),
        ("gpt_live_model", "voice"),
        ("gpt_live_backend_model", "shared"),
        ("gpt_live_backend_reasoning_effort", "shared"),
        ("openai_api_key", None),
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


def test_estimate_uses_configured_backend_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from evals import cli
    from evals.schema import load_suites

    monkeypatch.setenv("GPT_LIVE_BACKEND_MODEL", "gpt-4.1-mini")
    assert cli.configured_backend_model() == "gpt-4.1-mini"
    suite = load_suites(names=["web_search"])["web_search"]
    _, cheap = cli._estimate("brain", suite, None, "gpt-4.1-mini")
    _, default = cli._estimate("brain", suite, None, cli.DEFAULT_BACKEND_MODEL)
    assert cheap < default
