"""Compose GPT-Live prompts from versioned, reusable modules.

Why a composer instead of string literals in code?

* **GPT-Live splits the agent into two brains.** The *voice* model talks to the caller and its
  instructions are immutable once the session starts; the *backend* Responses model decides
  and calls tools and has its own instructions. Both are assembled here from small modules so
  a skill (e.g. restaurant reservations) can contribute a voice half and a backend half that
  ship together.
* **Prompts are behavior, so they are versioned like code.** Every composition produces a
  deterministic :attr:`PromptBundle.fingerprint`. The agent logs it and attaches it to the
  LiveKit session, and the eval pipeline compares fingerprints between git revisions to decide
  whether a change can alter behavior (and therefore needs paid evals) at all.
* **Strictness catches mistakes before a call does.** Unknown variables, modules used for the
  wrong target, or skills whose tools are not enabled all fail at composition time, in CI,
  instead of producing a subtly broken prompt in production.

Layout (see ``prompts/manifest.yaml``)::

    prompts/
      manifest.yaml               # global variables + profiles (which modules, which tools)
      modules/<id>.md             # YAML front matter + markdown body

Runtime variables (declared under ``runtime_variables`` in the manifest, e.g. ``today``) are
values that legitimately change between sessions. They are rendered with their real values in
the instructions, but with stable placeholders when computing fingerprints, so the fingerprint
identifies the *prompt version* rather than the day the call happened.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

__all__ = [
    "DEFAULT_PROMPTS_DIR",
    "ModuleTarget",
    "PromptBundle",
    "PromptComposer",
    "PromptCompositionError",
    "PromptModule",
    "Target",
    "normalize_prompt_text",
]

Target = Literal["voice", "backend"]
"""Which brain a composed prompt is for."""
ModuleTarget = Literal["voice", "backend", "any"]
"""Which brain(s) a module may be listed under; ``any`` modules can be shared by both."""

_TARGETS: tuple[Target, ...] = ("voice", "backend")
_MODULE_TARGETS: frozenset[str] = frozenset(get_args(ModuleTarget))
_SHORT_FINGERPRINT_CHARS = 12


def _default_prompts_dir() -> Path:
    env = os.environ.get("PROMPTS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    # agent/voice_agent/prompts/composer.py -> parents[3] is the repo root.
    return Path(__file__).resolve().parents[3] / "prompts"


DEFAULT_PROMPTS_DIR: Path = _default_prompts_dir()
"""The prompts directory as of import time. ``PromptComposer()`` re-resolves it on construction,
so a ``PROMPTS_DIR`` loaded from ``.env`` after import still takes effect."""

_VAR_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_VAR_NAME_RE = re.compile(_VAR_NAME)
_VAR_RE = re.compile(r"\{\{\s*(" + _VAR_NAME + r")\s*\}\}")
_LEFTOVER_BRACES_RE = re.compile(r"\{\{|\}\}")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


class PromptCompositionError(ValueError):
    """A manifest or module is invalid, or a profile cannot be composed."""


def normalize_prompt_text(text: str) -> str:
    """Canonicalize prompt text so cosmetic edits do not change what the model sees.

    Strips HTML comments (author notes), trailing whitespace on every line, collapses runs of
    3+ newlines to a single blank line, and trims the ends. Used both before rendering and for
    fingerprinting, so adding a comment or re-wrapping blank lines never triggers paid evals.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HTML_COMMENT_RE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _MANY_NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptModule:
    """One ``prompts/modules/<id>.md`` file, parsed but not yet rendered."""

    id: str
    version: int
    target: ModuleTarget
    description: str
    body: str
    requires_tools: tuple[str, ...] = ()
    variables: tuple[str, ...] = ()
    path: Path | None = None

    def render(self, values: Mapping[str, str]) -> str:
        """Substitute ``{{ var }}`` placeholders. Strict: every used var must be declared."""
        body = normalize_prompt_text(self.body)
        used = set(_VAR_RE.findall(body))
        undeclared = sorted(used - set(self.variables))
        if undeclared:
            raise PromptCompositionError(
                f"module '{self.id}' uses undeclared variable(s) {undeclared}; "
                "add them to its front matter `variables:` list"
            )
        missing = sorted(v for v in used if v not in values)
        if missing:
            raise PromptCompositionError(
                f"module '{self.id}' needs variable(s) {missing} but no value was provided "
                "(set them in manifest `variables`, the profile, or extra_variables)"
            )
        rendered = _VAR_RE.sub(lambda m: values[m.group(1)], body)
        # Anything still looking like a template is a typo such as `{{ agent-name }}`.
        if _LEFTOVER_BRACES_RE.search(_VAR_RE.sub("", body)):
            raise PromptCompositionError(
                f"module '{self.id}' contains a malformed placeholder (use {{{{ name }}}})"
            )
        return rendered


@dataclass(frozen=True)
class PromptBundle:
    """Everything one agent session needs from the prompt system, plus provenance.

    Voice instructions are immutable for the life of a GPT-Live session, so a bundle is
    composed once per session and never mutated; ``fingerprint`` is what gets logged and
    attached to the session so any transcript can be traced back to the exact prompt version.
    """

    profile: str
    voice_instructions: str
    backend_instructions: str
    tools: tuple[str, ...]
    modules: dict[str, tuple[str, ...]]
    fingerprint: str
    greeting: str | None = None
    variables: dict[str, str] = field(default_factory=dict)
    runtime_variables: tuple[str, ...] = ()
    target_fingerprints: dict[str, str] = field(default_factory=dict)

    @property
    def version(self) -> str:
        """Short (12 hex chars) fingerprint, convenient for logs and UI."""
        return self.fingerprint[:_SHORT_FINGERPRINT_CHARS]

    def fingerprint_for(self, target: Target) -> str:
        """Fingerprint of just one brain's instructions (voice includes the greeting)."""
        try:
            return self.target_fingerprints[target]
        except KeyError:
            raise ValueError(f"unknown target {target!r}; expected one of {_TARGETS}") from None

    def short_fingerprint_for(self, target: Target) -> str:
        """:meth:`fingerprint_for` shortened like :attr:`version`, for logs and UI."""
        return self.fingerprint_for(target)[:_SHORT_FINGERPRINT_CHARS]

    def trace_attributes(self) -> dict[str, str]:
        """Flat string attributes suitable for LiveKit participant attributes / log context."""
        return {
            "prompt.profile": self.profile,
            "prompt.fingerprint": self.fingerprint,
            "prompt.version": self.version,
            "prompt.voice": self.short_fingerprint_for("voice"),
            "prompt.backend": self.short_fingerprint_for("backend"),
            "prompt.tools": ",".join(self.tools),
        }


class PromptComposer:
    """Loads ``manifest.yaml`` + modules from a prompts directory and composes profiles."""

    def __init__(self, prompts_dir: Path | str | None = None) -> None:
        self.prompts_dir = Path(prompts_dir) if prompts_dir is not None else _default_prompts_dir()
        self.modules_dir = self.prompts_dir / "modules"
        self._manifest = self._load_manifest()
        self._module_cache: dict[str, PromptModule] = {}

    # ---------------------------------------------------------------- public API
    @property
    def manifest(self) -> dict[str, Any]:
        """The parsed manifest (read-only by convention)."""
        return self._manifest

    def list_profiles(self) -> list[str]:
        """Profile names defined in the manifest, sorted."""
        return sorted(self._manifest["profiles"])

    def load_module(self, module_id: str) -> PromptModule:
        """Parse ``modules/<module_id>.md`` (cached)."""
        if module_id not in self._module_cache:
            self._module_cache[module_id] = self._parse_module(module_id)
        return self._module_cache[module_id]

    def compose(self, profile: str, extra_variables: dict[str, str] | None = None) -> PromptBundle:
        """Render a profile into a :class:`PromptBundle`.

        ``extra_variables`` override manifest/profile values and supply runtime variables
        (``today``, ``timezone``). Runtime variables that are not supplied render as stable
        ``<runtime:name>`` placeholders, which keeps offline tooling (CLI, evals, change
        detection) deterministic.
        """
        profiles = self._manifest["profiles"]
        if profile not in profiles:
            raise PromptCompositionError(
                f"unknown profile '{profile}'; available: {', '.join(self.list_profiles())}"
            )
        spec = profiles[profile]
        tools = tuple(spec.get("tools") or ())
        runtime_names = tuple(self._manifest.get("runtime_variables") or {})

        base: dict[str, str] = {}
        base.update(_stringify(self._manifest.get("variables") or {}, "manifest variables"))
        base.update(_stringify(spec.get("variables") or {}, f"profile '{profile}' variables"))
        extras = _stringify(extra_variables or {}, "extra_variables")

        placeholders = {name: f"<runtime:{name}>" for name in runtime_names}
        # Actual values: defaults < profile < extras; unsupplied runtime vars get placeholders.
        actual = {**placeholders, **base, **extras}
        # Fingerprint values: identical, except runtime vars are always placeholders.
        stable = {**actual, **placeholders}

        module_ids: dict[str, tuple[str, ...]] = {}
        rendered: dict[str, str] = {}
        rendered_stable: dict[str, str] = {}
        for target in _TARGETS:
            ids = tuple(spec.get(target) or ())
            if len(set(ids)) != len(ids):
                raise PromptCompositionError(
                    f"profile '{profile}' lists a {target} module more than once"
                )
            modules = [self._checked_module(mid, target, profile, tools) for mid in ids]
            module_ids[target] = ids
            rendered[target] = "\n\n".join(m.render(actual) for m in modules)
            rendered_stable[target] = "\n\n".join(m.render(stable) for m in modules)

        if not rendered["voice"]:
            raise PromptCompositionError(f"profile '{profile}' has no voice modules")

        greeting = greeting_stable = None
        greeting_template = spec.get("greeting")
        if greeting_template is not None:
            greeting_module = _greeting_module(profile, str(greeting_template))
            greeting = greeting_module.render(actual)
            greeting_stable = greeting_module.render(stable)

        return PromptBundle(
            profile=profile,
            voice_instructions=rendered["voice"],
            backend_instructions=rendered["backend"],
            tools=tools,
            modules=module_ids,
            fingerprint=_bundle_fingerprint(rendered_stable, greeting_stable, tools),
            greeting=greeting,
            variables=actual,
            runtime_variables=runtime_names,
            target_fingerprints=_target_fingerprints(rendered_stable, greeting_stable),
        )

    # ---------------------------------------------------------------- internals
    def _load_manifest(self) -> dict[str, Any]:
        path = self.prompts_dir / "manifest.yaml"
        if not path.is_file():
            raise PromptCompositionError(f"manifest not found: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PromptCompositionError(f"invalid YAML in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PromptCompositionError(f"{path} must be a mapping")
        if data.get("version") != 1:
            raise PromptCompositionError(
                f"{path}: unsupported manifest version {data.get('version')!r}"
            )
        profiles = data.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            raise PromptCompositionError(f"{path}: `profiles` must be a non-empty mapping")
        for name, spec in profiles.items():
            if not isinstance(spec, dict):
                raise PromptCompositionError(f"profile '{name}' must be a mapping")
            for key in ("voice", "backend", "tools"):
                value = spec.get(key)
                if value is not None and not (
                    isinstance(value, list) and all(isinstance(v, str) for v in value)
                ):
                    raise PromptCompositionError(
                        f"profile '{name}': `{key}` must be a list of strings"
                    )
        runtime = data.get("runtime_variables")
        if runtime is not None and not isinstance(runtime, dict):
            raise PromptCompositionError(
                f"{path}: `runtime_variables` must map names to descriptions"
            )
        return data

    def _checked_module(
        self, module_id: str, target: Target, profile: str, tools: tuple[str, ...]
    ) -> PromptModule:
        module = self.load_module(module_id)
        if module.target not in (target, "any"):
            raise PromptCompositionError(
                f"profile '{profile}': module '{module_id}' targets '{module.target}' "
                f"but is listed under '{target}'"
            )
        missing_tools = [t for t in module.requires_tools if t not in tools]
        if missing_tools:
            raise PromptCompositionError(
                f"profile '{profile}': module '{module_id}' requires tool(s) {missing_tools} "
                "which the profile does not enable"
            )
        return module

    def _parse_module(self, module_id: str) -> PromptModule:
        path = self.modules_dir / f"{module_id}.md"
        # Guard against ids like "../../etc/passwd" escaping the modules directory.
        if self.modules_dir.resolve() not in path.resolve().parents:
            raise PromptCompositionError(f"invalid module id '{module_id}'")
        if not path.is_file():
            raise PromptCompositionError(f"module '{module_id}' not found at {path}")
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        match = _FRONT_MATTER_RE.match(text)
        if not match:
            raise PromptCompositionError(f"module '{module_id}' is missing YAML front matter")
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise PromptCompositionError(f"module '{module_id}': bad front matter: {exc}") from exc
        if not isinstance(meta, dict):
            raise PromptCompositionError(f"module '{module_id}': front matter must be a mapping")

        declared_id = meta.get("id")
        if declared_id != module_id:
            raise PromptCompositionError(
                f"module at {path} declares id {declared_id!r}; expected '{module_id}'"
            )
        target = meta.get("target")
        if target not in _MODULE_TARGETS:
            raise PromptCompositionError(
                f"module '{module_id}': target must be one of {sorted(_MODULE_TARGETS)}"
            )
        version = meta.get("version")
        if not isinstance(version, int):
            raise PromptCompositionError(f"module '{module_id}': `version` must be an integer")
        body = text[match.end() :]
        if not normalize_prompt_text(body):
            raise PromptCompositionError(f"module '{module_id}' has an empty body")
        return PromptModule(
            id=module_id,
            version=version,
            target=target,
            description=str(meta.get("description") or ""),
            body=body,
            requires_tools=_str_tuple(meta.get("requires_tools"), module_id, "requires_tools"),
            variables=_str_tuple(meta.get("variables"), module_id, "variables"),
            path=path,
        )


def _greeting_module(profile: str, template: str) -> PromptModule:
    """Wrap a profile's greeting in a module so it renders under the same strict rules."""
    return PromptModule(
        id=f"{profile}#greeting",
        version=1,
        target="voice",
        description="profile greeting",
        body=template,
        variables=tuple(set(_VAR_RE.findall(template))),
    )


def _target_fingerprints(
    rendered_stable: Mapping[str, str], greeting: str | None
) -> dict[str, str]:
    """Per-brain fingerprints; the greeting is spoken by the voice model, so it counts there."""
    return {
        "voice": _sha256(json.dumps([rendered_stable["voice"], greeting], ensure_ascii=False)),
        "backend": _sha256(rendered_stable["backend"]),
    }


def _bundle_fingerprint(
    rendered_stable: Mapping[str, str], greeting: str | None, tools: tuple[str, ...]
) -> str:
    """Fingerprint of everything that can change behavior: both prompts, greeting, tools."""
    return _sha256(
        json.dumps(
            {
                "voice": rendered_stable["voice"],
                "backend": rendered_stable["backend"],
                "tools": sorted(tools),
                "greeting": greeting,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )


def _str_tuple(value: object, module_id: str, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PromptCompositionError(f"module '{module_id}': `{key}` must be a list of strings")
    return tuple(value)


def _stringify(values: Mapping[str, object], where: str) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise PromptCompositionError(f"{where} must be a mapping")
    out: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _VAR_NAME_RE.fullmatch(key):
            raise PromptCompositionError(f"{where}: invalid variable name {key!r}")
        if value is None or isinstance(value, (dict, list)):
            raise PromptCompositionError(f"{where}: variable '{key}' must be a scalar")
        out[key] = str(value)
    return out
