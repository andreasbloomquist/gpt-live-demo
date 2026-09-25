"""Loading the versioned rubric and turning dimension scores into an overall score.

The overall score is computed here, in code, from the rubric's weights, and never asked of the
LLM: a model asked for "an overall score" anchors on vibes and drifts between runs, whereas a
weighted average of anchored 1-5 judgements is reproducible and explainable.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import DIMENSIONS, Dimension, DimensionScore

DEFAULT_RUBRIC_PATH = Path(__file__).with_name("rubric.yaml")


class DimensionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    weight: float = Field(gt=0, le=1)
    question: str
    anchors: dict[int, str]
    inverted: bool = False

    @model_validator(mode="after")
    def _anchors_in_range(self) -> DimensionSpec:
        if not self.anchors or not set(self.anchors) <= {1, 2, 3, 4, 5}:
            raise ValueError("anchors must be keyed by scores 1-5")
        return self


class ScoreCap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: Dimension
    at_or_below: int = Field(ge=1, le=5)
    max_overall: int = Field(ge=0, le=100)


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    instructions: str
    agent_policy: str
    dimensions: dict[Dimension, DimensionSpec]
    caps: tuple[ScoreCap, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Rubric:
        missing = set(DIMENSIONS) - set(self.dimensions)
        if missing:
            raise ValueError(f"rubric is missing dimensions: {sorted(missing)}")
        total = sum(d.weight for d in self.dimensions.values())
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"dimension weights must sum to 1.0 (got {total:.4f})")
        return self

    def overall_score(self, scores: Mapping[Dimension, DimensionScore]) -> int | None:
        """Weighted 0-100 score over the dimensions that survived validation.

        Each 1-5 score maps linearly to 0-100 (inverted dimensions count as ``6 - score``).
        Missing dimensions are left out and the remaining weights renormalized, so one discarded
        judgement doesn't drag the total to zero. Caps then apply (e.g. a policy violation caps
        the call at 40 no matter how pleasant it was). Returns ``None`` if nothing was scored.
        """
        weighted = 0.0
        weight_sum = 0.0
        for dim, result in scores.items():
            spec = self.dimensions[dim]
            value = 6 - result.score if spec.inverted else result.score
            weighted += spec.weight * (value - 1) / 4 * 100
            weight_sum += spec.weight
        if weight_sum == 0:
            return None
        overall = round(weighted / weight_sum)
        for cap in self.caps:
            result = scores.get(cap.dimension)
            if result is not None and result.score <= cap.at_or_below:
                overall = min(overall, cap.max_overall)
        return overall


def load_rubric(path: Path | str = DEFAULT_RUBRIC_PATH) -> Rubric:
    """Parse and validate a rubric file. Raises ``ValueError`` with the reason if it's invalid."""
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return Rubric.model_validate(data)


@lru_cache(maxsize=1)
def default_rubric() -> Rubric:
    return load_rubric(DEFAULT_RUBRIC_PATH)
