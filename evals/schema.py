"""Typed model of an eval suite (``evals/suites/*.yaml``).

Suites are *data*, validated by pydantic so a typo in a YAML key fails loudly in the free
``unit`` tier instead of silently turning a paid eval into a no-op. ``extra="forbid"``
everywhere is the important bit: an unknown key is almost always a misspelled assertion.

The JSON Schema in ``evals/suite.schema.json`` is generated from these models
(``python -m evals schema``) so editors can autocomplete suites; a unit test keeps it in sync.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evals.paths import SUITES_DIR

Tier = Literal["brain", "voice"]
TIERS: tuple[Tier, ...] = ("brain", "voice")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedToolCall(_Strict):
    """A tool call the agent must make.

    ``args_subset`` only constrains the keys it lists, because the model may legitimately add
    optional arguments (e.g. ``city``). Values are matched leniently (see
    :func:`evals.runners.assertions.match_value`): strings compare case/whitespace-insensitively,
    and operator dicts ``{"$regex": ...}``, ``{"$in": [...]}``, ``{"$contains": ...}``,
    ``{"$exists": true}`` express the "any reasonable value" cases.
    """

    name: str
    args_subset: dict[str, Any] = Field(default_factory=dict)


class Expect(_Strict):
    """What a case asserts. Deterministic checks run first; the LLM judge only runs if they pass
    (cheaper, and a judge should never be asked to excuse a wrong tool call)."""

    tool_calls: list[ExpectedToolCall] = Field(default_factory=list)
    ordered: bool = Field(
        default=False, description="Require expected tool calls to appear in this order."
    )
    no_tool_calls: bool = Field(default=False, description="Assert that no tool is called.")
    forbidden_tools: list[str] = Field(default_factory=list)
    max_words: int | None = Field(
        default=None, gt=0, description="Upper bound on words in the final spoken reply."
    )
    must_not_match: list[str] = Field(
        default_factory=list,
        description=(
            "Regexes (case-insensitive, multiline) that no assistant speech after the first "
            "user turn may match, e.g. URLs. The greeting is excluded."
        ),
    )
    judge: str | None = Field(
        default=None, description="Rubric for the LLM judge, phrased as pass criteria."
    )

    @field_validator("must_not_match")
    @classmethod
    def _regexes_compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from None
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Expect:
        if self.no_tool_calls and self.tool_calls:
            raise ValueError("`no_tool_calls: true` contradicts a non-empty `tool_calls` list")
        forbidden = set(self.forbidden_tools) & {c.name for c in self.tool_calls}
        if forbidden:
            raise ValueError(f"tools both expected and forbidden: {sorted(forbidden)}")
        return self


class Case(_Strict):
    """One scenario. Either a single ``user`` utterance or a multi-turn ``turns`` script; the
    expectations apply to the agent's behaviour over the *whole* case, judged after the last turn.
    """

    id: Annotated[str, Field(pattern=_ID_RE.pattern)]
    description: str | None = None
    user: str | None = None
    turns: list[str] | None = None
    tiers: list[Tier] | None = Field(
        default=None, description="Restrict this case to a subset of the suite's tiers."
    )
    trials: int | None = Field(default=None, ge=1, le=20)
    expect: Expect

    @model_validator(mode="after")
    def _one_input(self) -> Case:
        if (self.user is None) == (self.turns is None):
            raise ValueError(f"case {self.id!r}: set exactly one of `user` or `turns`")
        if self.turns is not None and not self.turns:
            raise ValueError(f"case {self.id!r}: `turns` must not be empty")
        return self

    @property
    def user_turns(self) -> list[str]:
        return list(self.turns) if self.turns is not None else [self.user or ""]

    def runs_on(self, tier: Tier) -> bool:
        return self.tiers is None or tier in self.tiers


class Suite(_Strict):
    """A named group of cases sharing a prompt profile and tier configuration.

    ``tools`` declares which tools the suite exercises. The change-impact detector uses it to
    decide that a change to ``check_restaurant_availability``'s implementation must re-run the
    restaurant suite's brain tier but not the web-search suite's.
    """

    name: Annotated[str, Field(pattern=_ID_RE.pattern)]
    description: str | None = None
    profile: str
    tiers: list[Tier] = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)
    trials: int = Field(default=3, ge=1, le=20, description="Default trials per case.")
    pass_threshold: float = Field(
        default=0.66,
        gt=0,
        le=1,
        description="Minimum per-case pass rate across trials for the case to pass.",
    )
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Suite:
        ids = [c.id for c in self.cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate case ids: {dupes}")
        if len(set(self.tiers)) != len(self.tiers):
            raise ValueError("duplicate tiers")
        for case in self.cases:
            if case.tiers and not set(case.tiers) <= set(self.tiers):
                raise ValueError(f"case {case.id!r} lists tiers not enabled on the suite")
            used = {c.name for c in case.expect.tool_calls}
            undeclared = used - set(self.tools)
            if undeclared:
                raise ValueError(
                    f"case {case.id!r} expects tools not declared in suite `tools`: "
                    f"{sorted(undeclared)}"
                )
        return self

    def cases_for(self, tier: Tier) -> list[Case]:
        return [c for c in self.cases if c.runs_on(tier)]

    def trials_for(self, case: Case) -> int:
        return case.trials or self.trials


class SuiteLoadError(ValueError):
    """A suite file failed to parse or validate; the message names the file."""


def load_suite(path: Path) -> Suite:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        suite = Suite.model_validate(data)
    except Exception as exc:  # yaml.YAMLError | pydantic.ValidationError
        raise SuiteLoadError(f"{path}: {exc}") from exc
    if suite.name != path.stem:
        raise SuiteLoadError(f"{path}: suite name {suite.name!r} must match file name")
    return suite


def suite_paths(suites_dir: Path = SUITES_DIR) -> list[Path]:
    return sorted(p for p in suites_dir.glob("*.yaml") if not p.name.startswith("_"))


def load_suites(
    suites_dir: Path = SUITES_DIR, names: Iterable[str] | None = None
) -> dict[str, Suite]:
    """Load (and validate) all suites, optionally filtered by name. Unknown names are an error
    so a typo in ``--suite`` or a workflow input can never mean "run nothing, report green"."""
    suites = {s.name: s for s in (load_suite(p) for p in suite_paths(suites_dir))}
    if names is None:
        return suites
    wanted = list(names)
    missing = sorted(set(wanted) - set(suites))
    if missing:
        raise SuiteLoadError(f"unknown suite(s): {missing}; available: {sorted(suites)}")
    return {n: suites[n] for n in wanted}


def suite_json_schema() -> dict[str, Any]:
    schema = Suite.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "GPT-Live eval suite"
    return schema
