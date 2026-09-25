"""PromptComposer: variables, strictness, fingerprints, and the ``prompts`` CLI."""

from __future__ import annotations

import json

import pytest
from helpers import PromptTree, module

from voice_agent.prompts import PromptComposer, PromptCompositionError, normalize_prompt_text
from voice_agent.prompts.__main__ import main as prompts_cli

MANIFEST = """
version: 1
variables:
  agent_name: Ava
runtime_variables:
  today: current date
profiles:
  demo:
    description: test profile
    variables: { city: Paris }
    greeting: "Say hi as {{ agent_name }}."
    voice: [core/hello]
    backend: [backend/policy]
    tools: [web_search]
"""


def _modules(**overrides: str) -> dict[str, str]:
    mods = {
        "core/hello": module(
            "core/hello",
            "Hi, I'm {{ agent_name }} in {{ city }}.  \n"
            "<!-- author note -->\nToday is {{ today }}.",
            variables=["agent_name", "city", "today"],
        ),
        "backend/policy": module(
            "backend/policy", "Use tools wisely.", target="backend", requires_tools=["web_search"]
        ),
    }
    mods.update(overrides)
    return mods


def test_render_substitutes_variables_and_strips_comments(prompt_tree: PromptTree) -> None:
    bundle = PromptComposer(prompt_tree(MANIFEST, _modules())).compose(
        "demo", extra_variables={"today": "Friday, 2026-09-25"}
    )
    assert bundle.voice_instructions == "Hi, I'm Ava in Paris.\n\nToday is Friday, 2026-09-25."
    assert bundle.backend_instructions == "Use tools wisely."
    assert bundle.tools == ("web_search",)
    assert bundle.modules == {"voice": ("core/hello",), "backend": ("backend/policy",)}
    assert bundle.greeting == "Say hi as Ava."
    assert "author note" not in bundle.voice_instructions


def test_modules_are_joined_with_a_blank_line(prompt_tree: PromptTree) -> None:
    manifest = MANIFEST.replace("voice: [core/hello]", "voice: [core/a, core/b]")
    mods = _modules(**{"core/a": module("core/a", "A"), "core/b": module("core/b", "B")})
    bundle = PromptComposer(prompt_tree(manifest, mods)).compose("demo")
    assert bundle.voice_instructions == "A\n\nB"


def test_extra_variables_override_profile_and_globals(prompt_tree: PromptTree) -> None:
    bundle = PromptComposer(prompt_tree(MANIFEST, _modules())).compose(
        "demo", extra_variables={"agent_name": "Max", "city": "Rome"}
    )
    assert bundle.voice_instructions.startswith("Hi, I'm Max in Rome.")


def test_unsupplied_runtime_variable_renders_placeholder(prompt_tree: PromptTree) -> None:
    bundle = PromptComposer(prompt_tree(MANIFEST, _modules())).compose("demo")
    assert "Today is <runtime:today>." in bundle.voice_instructions
    assert bundle.runtime_variables == ("today",)


def test_undeclared_variable_is_an_error(prompt_tree: PromptTree) -> None:
    mods = _modules(**{"core/hello": module("core/hello", "Hi {{ agent_name }}")})
    with pytest.raises(PromptCompositionError, match="undeclared"):
        PromptComposer(prompt_tree(MANIFEST, mods)).compose("demo")


def test_declared_but_unknown_variable_is_an_error(prompt_tree: PromptTree) -> None:
    mods = _modules(
        **{"core/hello": module("core/hello", "Hi {{ nickname }}", variables=["nickname"])}
    )
    with pytest.raises(PromptCompositionError, match="no value"):
        PromptComposer(prompt_tree(MANIFEST, mods)).compose("demo")


def test_malformed_placeholder_is_an_error(prompt_tree: PromptTree) -> None:
    mods = _modules(**{"core/hello": module("core/hello", "Hi {{ agent-name }}")})
    with pytest.raises(PromptCompositionError, match="malformed"):
        PromptComposer(prompt_tree(MANIFEST, mods)).compose("demo")


def test_variables_inside_comments_are_ignored(prompt_tree: PromptTree) -> None:
    mods = _modules(**{"core/hello": module("core/hello", "Hi <!-- {{ secret }} -->there")})
    bundle = PromptComposer(prompt_tree(MANIFEST, mods)).compose("demo")
    assert bundle.voice_instructions == "Hi there"


def test_target_mismatch_is_an_error(prompt_tree: PromptTree) -> None:
    mods = _modules(**{"core/hello": module("core/hello", "Hi", target="backend")})
    with pytest.raises(PromptCompositionError, match="targets 'backend'"):
        PromptComposer(prompt_tree(MANIFEST, mods)).compose("demo")


def test_any_target_can_be_used_in_both_lists(prompt_tree: PromptTree) -> None:
    manifest = MANIFEST.replace("backend: [backend/policy]", "backend: [shared/x]")
    mods = _modules(**{"shared/x": module("shared/x", "Shared", target="any")})
    assert PromptComposer(prompt_tree(manifest, mods)).compose("demo").backend_instructions == (
        "Shared"
    )


def test_requires_tools_must_be_enabled(prompt_tree: PromptTree) -> None:
    manifest = MANIFEST.replace("tools: [web_search]", "tools: []")
    with pytest.raises(PromptCompositionError, match="requires tool"):
        PromptComposer(prompt_tree(manifest, _modules())).compose("demo")


def test_missing_module_is_an_error(prompt_tree: PromptTree) -> None:
    manifest = MANIFEST.replace("voice: [core/hello]", "voice: [core/nope]")
    with pytest.raises(PromptCompositionError, match="not found"):
        PromptComposer(prompt_tree(manifest, _modules())).compose("demo")


def test_unknown_profile_is_an_error(prompt_tree: PromptTree) -> None:
    with pytest.raises(PromptCompositionError, match="unknown profile"):
        PromptComposer(prompt_tree(MANIFEST, _modules())).compose("missing")


def test_module_id_must_match_path(prompt_tree: PromptTree) -> None:
    mods = _modules(**{"core/hello": module("core/other", "Hi")})
    with pytest.raises(PromptCompositionError, match="declares id"):
        PromptComposer(prompt_tree(MANIFEST, mods)).compose("demo")


def test_module_id_cannot_escape_modules_dir(prompt_tree: PromptTree) -> None:
    manifest = MANIFEST.replace("voice: [core/hello]", "voice: [../manifest]")
    with pytest.raises(PromptCompositionError, match="invalid module id"):
        PromptComposer(prompt_tree(manifest, _modules())).compose("demo")


def test_fingerprint_is_stable_and_ignores_cosmetic_edits(prompt_tree: PromptTree) -> None:
    a = PromptComposer(prompt_tree(MANIFEST, _modules())).compose("demo")
    b = PromptComposer(prompt_tree(MANIFEST, _modules())).compose("demo")
    assert a.fingerprint == b.fingerprint
    assert len(a.fingerprint) == 64 and a.version == a.fingerprint[:12]

    cosmetic = _modules(
        **{
            "backend/policy": module(
                "backend/policy",
                "<!-- new note -->\nUse tools wisely.   \n\n\n\n",
                target="backend",
                requires_tools=["web_search"],
            )
        }
    )
    c = PromptComposer(prompt_tree(MANIFEST, cosmetic)).compose("demo")
    assert c.fingerprint == a.fingerprint


def test_fingerprint_ignores_runtime_values(prompt_tree: PromptTree) -> None:
    composer = PromptComposer(prompt_tree(MANIFEST, _modules()))
    monday = composer.compose("demo", extra_variables={"today": "Monday"})
    tuesday = composer.compose("demo", extra_variables={"today": "Tuesday"})
    assert monday.voice_instructions != tuesday.voice_instructions
    assert monday.fingerprint == tuesday.fingerprint == composer.compose("demo").fingerprint


def test_fingerprint_is_sensitive_to_content_vars_tools_and_target(
    prompt_tree: PromptTree,
) -> None:
    base = PromptComposer(prompt_tree(MANIFEST, _modules())).compose("demo")

    edited_backend = _modules(
        **{
            "backend/policy": module(
                "backend/policy",
                "Use tools rarely.",
                target="backend",
                requires_tools=["web_search"],
            )
        }
    )
    changed = PromptComposer(prompt_tree(MANIFEST, edited_backend)).compose("demo")
    assert changed.fingerprint != base.fingerprint
    assert changed.fingerprint_for("backend") != base.fingerprint_for("backend")
    assert changed.fingerprint_for("voice") == base.fingerprint_for("voice")

    renamed = PromptComposer(prompt_tree(MANIFEST, _modules())).compose(
        "demo", extra_variables={"agent_name": "Max"}
    )
    assert renamed.fingerprint != base.fingerprint

    more_tools = MANIFEST.replace("tools: [web_search]", "tools: [web_search, other]")
    assert (
        PromptComposer(prompt_tree(more_tools, _modules())).compose("demo").fingerprint
        != base.fingerprint
    )

    with pytest.raises(ValueError):
        base.fingerprint_for("nope")  # type: ignore[arg-type]


def test_trace_attributes_are_flat_strings(prompt_tree: PromptTree) -> None:
    attrs = PromptComposer(prompt_tree(MANIFEST, _modules())).compose("demo").trace_attributes()
    assert attrs["prompt.profile"] == "demo"
    assert all(isinstance(v, str) for v in attrs.values())


def test_normalize_prompt_text() -> None:
    text = "a  \r\n<!-- note\nspanning -->b\n\n\n\n\nc\t\n"
    assert normalize_prompt_text(text) == "a\nb\n\nc"


def test_cli_render_json(prompt_tree: PromptTree, capsys: pytest.CaptureFixture[str]) -> None:
    root = prompt_tree(MANIFEST, _modules())
    assert prompts_cli(["--prompts-dir", str(root), "render", "demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "demo"
    assert payload["voice_instructions"].startswith("Hi, I'm Ava")

    assert prompts_cli(["--prompts-dir", str(root), "render", "missing"]) == 1


def test_prompts_dir_env_is_read_when_the_composer_is_created(
    prompt_tree: PromptTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `.env` is loaded after the package is imported, so the directory must not be frozen then.
    tree = prompt_tree(MANIFEST, _modules())
    monkeypatch.setenv("PROMPTS_DIR", str(tree))
    assert PromptComposer().prompts_dir == tree.resolve()
