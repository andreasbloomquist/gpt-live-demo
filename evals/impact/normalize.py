"""Canonical forms used for fingerprinting. Each one answers: *could this edit change behaviour?*

Every normalizer here errs toward "changed" when unsure: a false positive costs a few cents of
evals, a false negative ships an untested behaviour change.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any

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

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_INLINE_WS = re.compile(r"[ \t\f\v]+")
# A line that starts a new markdown block: list item, heading, quote, table row, fence, rule.
_BLOCK_START = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>|\||```|~~~|---|\*\*\*|___)")


def normalize_prompt_for_fingerprint(text: str) -> str:
    """Reduce a *rendered* prompt to the tokens that can plausibly change model behaviour.

    The composer already strips HTML comments and trailing spaces; this goes further and is
    only used for change detection (never for what is sent to the model):

    * runs of spaces/tabs collapse to one space and lines are stripped;
    * any number of blank lines collapse to one paragraph break;
    * **soft-wrapped lines are re-joined**: reflowing a paragraph at a different column is the
      most common "no-op" prompt edit, and it must not trigger paid evals. Lines that begin a
      markdown block (``-``, ``1.``, ``#``, ``>``, ``|``, fences) are kept on their own line,
      so turning a paragraph into a bullet list *is* still a change.

    Wording, punctuation, casing and list structure all survive, because models are sensitive
    to all of them.
    """
    text = _HTML_COMMENT.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[list[str]] = [[]]
    for raw in text.split("\n"):
        line = _INLINE_WS.sub(" ", raw).strip()
        if not line:
            if paragraphs[-1]:
                paragraphs.append([])
            continue
        current = paragraphs[-1]
        if current and not _BLOCK_START.match(line):
            current[-1] = f"{current[-1]} {line}"
        else:
            current.append(line)
    return "\n\n".join("\n".join(p) for p in paragraphs if p)


def line_diff_stats(old: str, new: str) -> tuple[int, int]:
    """(+added, -removed) normalized lines, for human-readable reasons."""
    a = normalize_prompt_for_fingerprint(old).splitlines()
    b = normalize_prompt_for_fingerprint(new).splitlines()
    import difflib

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
    import yaml

    try:
        return json.dumps(yaml.safe_load(text), sort_keys=True, default=str)
    except yaml.YAMLError:
        return "UNPARSEABLE:" + digest(text)


_LOCK_PACKAGE = re.compile(
    r'^\[\[package\]\]\s*\nname\s*=\s*"(?P<name>[^"]+)"\s*\nversion\s*=\s*"(?P<version>[^"]+)"',
    re.MULTILINE,
)


def lockfile_versions(text: str) -> dict[str, str]:
    """``{package: version}`` from a ``uv.lock``.

    A regex rather than ``tomllib`` keeps this working on Python 3.10; uv always writes
    ``name`` then ``version`` directly under ``[[package]]``.
    """
    return {m["name"].lower(): m["version"] for m in _LOCK_PACKAGE.finditer(text)}
