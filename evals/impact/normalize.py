"""Canonical forms used for fingerprinting. Each one answers: *could this edit change behaviour?*

Every normalizer here errs toward "changed" when unsure: a false positive costs a few cents of
evals, a false negative ships an untested behaviour change.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
from typing import Any

import yaml

# --------------------------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------------------------


def digest(value: Any) -> str:
    """Short, stable sha256 of any JSON-serialisable value (dict keys sorted)."""
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------------------------

_INLINE_WS = re.compile(r"[ \t\f\v]+")
# A line that starts a new markdown block: list item, heading, quote, table row, fence, rule.
_BLOCK_START = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>|\||```|~~~|---|\*\*\*|___)")
_FENCE = re.compile(r"^(?:```|~~~)")


def normalize_prompt_for_fingerprint(text: str) -> str:
    """Reduce a *rendered* prompt to the tokens that can plausibly change model behaviour.

    Input is the prompt exactly as sent to the model (the composer has already removed HTML
    comments). This goes further and is only used for change detection, never for what is sent:

    * runs of spaces/tabs collapse to one space and trailing space is dropped;
    * any number of blank lines collapse to one paragraph break;
    * **soft-wrapped lines are re-joined**: reflowing a paragraph at a different column is the
      most common "no-op" prompt edit, and it must not trigger paid evals. Lines that begin a
      markdown block (``-``, ``1.``, ``#``, ``>``, ``|``, fences) are kept on their own line
      *with their indentation*, so turning a paragraph into a bullet list, or un-nesting a
      sub-bullet, *is* still a change;
    * inside fenced code blocks only trailing space is dropped: examples are literal.

    Wording, punctuation, casing and list structure all survive, because models are sensitive
    to all of them. HTML comments are deliberately *not* stripped here: anything that survived
    the composer (e.g. a comment opened in one module and closed in another) reaches the model.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[list[str]] = [[]]
    in_fence = False
    for raw in text.split("\n"):
        if in_fence:
            paragraphs[-1].append(raw.rstrip())
            in_fence = not _FENCE.match(raw.strip())
            continue
        line = _INLINE_WS.sub(" ", raw).strip()
        if not line:
            if paragraphs[-1]:
                paragraphs.append([])
            continue
        current = paragraphs[-1]
        if _BLOCK_START.match(line):
            indent = len(raw.expandtabs(4)) - len(raw.expandtabs(4).lstrip())
            current.append(" " * indent + line)
            in_fence = bool(_FENCE.match(line))
        elif current:
            current[-1] = f"{current[-1]} {line}"
        else:
            current.append(line)
    return "\n\n".join("\n".join(p) for p in paragraphs if p)


def line_diff_stats(old: str, new: str) -> tuple[int, int]:
    """(+added, -removed) normalized lines, for human-readable reasons."""
    a = normalize_prompt_for_fingerprint(old).splitlines()
    b = normalize_prompt_for_fingerprint(new).splitlines()
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


# --------------------------------------------------------------------------------------------
# Python source
# --------------------------------------------------------------------------------------------


class _StripDocstrings(ast.NodeTransformer):
    """Remove module/class/function docstrings.

    Why it is safe to drop *all* docstrings, including those of ``@function_tool`` functions and
    pydantic models whose docstrings LiveKit turns into tool/parameter descriptions: the
    detector fingerprints the **rendered tool JSON schema** separately (``tool.schema:*``),
    so any docstring edit that reaches the model is caught there — and correctly mapped to the
    brain *and* voice tiers — while docstring edits that never reach the model (helpers,
    providers, internals) are ignored. The AST fingerprint therefore only has to answer "did the
    *executed* code change?", which maps to the brain tier.
    """

    def _strip(self, node: Any) -> Any:
        self.generic_visit(node)
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = _strip
    visit_ClassDef = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip


def normalized_python(source: str) -> str:
    """AST dump without comments, formatting, docstrings or line numbers.

    Unparseable source yields a digest of the raw text: a syntax error is certainly a change.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "UNPARSEABLE:" + digest(source)
    tree = _StripDocstrings().visit(tree)
    return ast.dump(tree, annotate_fields=False, include_attributes=False)


# --------------------------------------------------------------------------------------------
# Settings defaults (config.py)
# --------------------------------------------------------------------------------------------


def _is_settings_class(node: ast.ClassDef) -> bool:
    names = {getattr(b, "id", None) or getattr(b, "attr", None) for b in node.bases}
    return "BaseSettings" in names or node.name == "Settings"


def settings_fields(source: str) -> tuple[dict[str, str], str]:
    """Split a settings module into ``({field: normalized annotation+default}, rest)``.

    Model names, voice, reasoning effort… are *behaviour* and are compared field-by-field so the
    detector can route e.g. a ``gpt_live_voice`` change to the voice tier only. Everything else
    in the module (validators, helper methods) becomes one opaque "config logic" fingerprint.
    Defaults are compared as normalized AST, so ``"low"`` → ``'low'`` or re-wrapping a
    ``Field(...)`` call is not a change but ``"low"`` → ``"medium"`` is.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}, "UNPARSEABLE:" + digest(source)
    tree = _StripDocstrings().visit(tree)
    fields: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and _is_settings_class(node)):
            continue
        kept: list[ast.stmt] = []
        for stmt in node.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id != "model_config"
            ):
                value = ast.dump(stmt.value) if stmt.value is not None else "<required>"
                fields[stmt.target.id] = f"{ast.dump(stmt.annotation)}={value}"
            else:
                kept.append(stmt)
        node.body = kept or [ast.Pass()]
    return fields, ast.dump(tree, annotate_fields=False, include_attributes=False)


# --------------------------------------------------------------------------------------------
# YAML / lockfile
# --------------------------------------------------------------------------------------------


def normalized_yaml(text: str) -> str:
    """Canonical JSON of a YAML document: comments, key order, quoting and flow/block style
    are irrelevant to the runner, so they are irrelevant to the fingerprint."""
    try:
        return json.dumps(yaml.safe_load(text), sort_keys=True, default=str)
    except yaml.YAMLError:
        return "UNPARSEABLE:" + digest(text)


_LOCK_BLOCK = re.compile(r"^\[\[package\]\]\s*$", re.MULTILINE)
_LOCK_FIELD = re.compile(r"^(name|version|source)\s*=\s*(.+?)\s*$", re.MULTILINE)


def lockfile_versions(text: str) -> dict[str, str]:
    """``{package: version}`` from a ``uv.lock``.

    The version string also carries the ``source`` when it is not a registry (a git revision or a
    local path can change code without a version bump), and every entry of a package that uv
    resolved more than once (per-marker forks). A regex rather than ``tomllib`` keeps this
    working on Python 3.10; only the top-level ``name``/``version``/``source`` keys of each
    ``[[package]]`` table are read.
    """
    entries: dict[str, list[str]] = {}
    for block in _LOCK_BLOCK.split(text)[1:]:
        block = block.split("\n[", 1)[0]  # stop at the next (sub-)table
        fields = {m[1]: m[2] for m in _LOCK_FIELD.finditer(block)}
        name = fields.get("name", "").strip('"').lower()
        if not name:
            continue
        version = fields.get("version", "?").strip('"')
        source = fields.get("source", "")
        if source and "registry" not in source:
            version = f"{version} ({source})"
        entries.setdefault(name, []).append(version)
    return {name: " | ".join(sorted(versions)) for name, versions in entries.items()}
