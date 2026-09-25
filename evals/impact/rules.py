"""The routing table: which fingerprint components can change which (suite, tier).

Kept as plain data + one pure function so the whole policy is reviewable on one screen.

=====================  =============================  ======================================
component key          produced from                  re-runs
=====================  =============================  ======================================
prompt.voice:<prof>    rendered voice instructions    voice tier of suites on <prof>
prompt.backend:<prof>  rendered backend instructions  brain + voice of suites on <prof>
profile.tools:<prof>   tool list of the profile       brain + voice of suites on <prof>
tool.schema:<tool>     tool JSON schema (model view)  brain + voice of suites whose *profile*
                                                      exposes <tool> (the model sees it even
                                                      when a case doesn't call it)
tool.impl:<tool>       normalized AST of tool code    brain tier of suites that *declare*
                                                      <tool> in ``tools``
code.core              composer / registry AST        everything
code.other:<path>      any other agent module AST     everything (unknown ⇒ conservative)
code.backend_runtime   model.py AST                   brain + voice of every suite
                                                      (``build_responses_options`` there is
                                                      the brain tier's backend config)
code.voice_runtime     agent.py / main.py AST         voice tier of every suite
config.voice:<field>   voice-only settings default    voice tier of every suite
config.shared:<field>  other behavioural defaults     brain + voice of every suite
config.logic           rest of config.py AST          brain + voice of every suite
suite:<name>           normalized suite YAML          that suite's tiers
runner:<tier|shared>   eval runner code AST           that tier (shared: both) of every suite
deps:<package>         uv.lock version                brain + voice of every suite
=====================  =============================  ======================================

Tool *implementation* changes only re-run the brain tier: tools execute identically (against
the deterministic mock provider) in both tiers, and the brain tier is where their outputs are
asserted. The voice tier adds acoustic/persona risk, which a tool refactor cannot change.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from evals.schema import Tier

AGENT_PKG = "agent/voice_agent"

# Code whose change can affect *every* rendered prompt or every tool: re-run everything.
CORE_CODE = (
    f"{AGENT_PKG}/__init__.py",
    f"{AGENT_PKG}/prompts/",
    f"{AGENT_PKG}/tools/__init__.py",
    f"{AGENT_PKG}/tools/registry.py",
)
# Model construction: GPT-Live voice settings *and* ``build_responses_options``, which the brain
# tier reuses verbatim as its backend request config. So it can change both tiers.
BACKEND_RUNTIME_CODE = (f"{AGENT_PKG}/model.py",)
# Code only on the voice path (Agent subclass / session wiring, entrypoint).
VOICE_RUNTIME_CODE = (
    f"{AGENT_PKG}/agent.py",
    f"{AGENT_PKG}/main.py",
)
CONFIG_FILE = f"{AGENT_PKG}/config.py"

# Eval runner code, grouped by the tier whose *results* it can change. ``evals/impact`` and
# ``evals/report.py`` are deliberately absent: they decide what runs / how it is displayed,
# not how the agent behaves or is graded.
RUNNER_CODE: dict[str, tuple[str, ...]] = {
    "brain": ("evals/runners/brain.py",),
    "voice": ("evals/runners/voice.py",),
    "shared": (
        "evals/runners/",  # prefix; the two tier-specific files above are matched first
        "evals/schema.py",
        "evals/toolschema.py",
        "evals/cli.py",
    ),
}

# Settings that never change agent behaviour in evals (credentials, endpoints, logging, and
# the OpenTable provider, which evals replace with the deterministic mock).
CONFIG_IGNORED = re.compile(r"(api_key|secret|token|_url$|^log_level$|^livekit_|^opentable_)")
# Settings consumed only by the GPT-Live voice model.
CONFIG_VOICE_ONLY = re.compile(r"^gpt_live_(model|voice)$")

# uv.lock packages whose version bump can change model I/O. Everything else (e.g. httpx) is
# covered by unit tests.
WATCHED_PACKAGE = re.compile(r"^(livekit(-.*)?|openai)$")

# Paths that can never change behaviour: used for the cheap "nothing relevant changed" fast
# path before any tree is exported or rendered.
RELEVANT_PREFIXES = (f"{AGENT_PKG}/", "prompts/", "evals/", "uv.lock")
IRRELEVANT_PREFIXES = ("evals/tests/", "evals/impact/", "evals/README.md", "evals/.results/")


def is_relevant_path(path: str) -> bool:
    if path.startswith(IRRELEVANT_PREFIXES):
        return False
    return path == "uv.lock" or path.startswith(RELEVANT_PREFIXES)


def runner_group(path: str) -> str | None:
    for group in ("brain", "voice", "shared"):
        for prefix in RUNNER_CODE[group]:
            if path == prefix or (prefix.endswith("/") and path.startswith(prefix)):
                return group
    return None


def config_kind(field: str) -> str | None:
    """``"voice"`` | ``"shared"`` | ``None`` (ignored). Unknown fields default to shared:
    a new setting is presumed behavioural until someone adds it to an allow-list."""
    if CONFIG_IGNORED.search(field):
        return None
    if CONFIG_VOICE_ONLY.match(field):
        return "voice"
    return "shared"


@dataclass(frozen=True)
class SuiteTarget:
    """The facts about a suite the routing rules need."""

    name: str
    profile: str
    tools: Collection[str]
    profile_tools: Collection[str]  # union of the profile's tools at base and head


def affects(key: str, suite: SuiteTarget, tier: Tier) -> bool:
    """Does a change in component ``key`` require re-running ``suite`` on ``tier``?"""
    kind, _, arg = key.partition(":")
    if kind == "prompt.voice":
        return arg == suite.profile and tier == "voice"
    if kind in ("prompt.backend", "profile.tools", "profile.error"):
        return arg == suite.profile
    if kind == "tool.schema":
        return arg in suite.profile_tools
    if kind == "tool.impl":
        return arg in suite.tools and tier == "brain"
    if kind in ("code.voice_runtime", "config.voice"):
        return tier == "voice"
    if kind == "suite":
        return arg == suite.name
    if kind == "runner":
        return arg in (tier, "shared")
    # code.core, code.backend_runtime, code.other, config.shared, config.logic, deps, and
    # anything new: both tiers (conservative).
    return True
