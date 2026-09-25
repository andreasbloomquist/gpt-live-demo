from __future__ import annotations

import hashlib

import pytest
import yaml
from pydantic import ValidationError

from call_analyzer.models import DIMENSIONS, DimensionScore
from call_analyzer.rubric import DEFAULT_RUBRIC_PATH, Rubric

# Pinned content hash per rubric version. If this test fails you changed rubric.yaml: bump its
# `version` (old analyses must stay distinguishable from new ones), then update both values.
PINNED_VERSION = "1"
PINNED_SHA256 = "f42f0e7fa2b5b15d8fbfe1f34abc827f08de4cec597239f74ab1e9727e14bc9e"


def _scores(value: int, **overrides: int) -> dict:
    scores = {d: DimensionScore(score=value, rationale="r") for d in DIMENSIONS}
    for dim, score in overrides.items():
        scores[dim] = DimensionScore(score=score, rationale="r")
    return scores


def test_rubric_content_changes_require_a_version_bump(rubric: Rubric) -> None:
    # Hash text with normalized newlines so a Windows (CRLF) checkout doesn't trip it.
    text = DEFAULT_RUBRIC_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert (rubric.version, digest) == (PINNED_VERSION, PINNED_SHA256), (
        "rubric.yaml changed: bump `version` in it and update PINNED_* in this test"
    )


def test_rubric_covers_every_dimension(rubric: Rubric) -> None:
    assert set(rubric.dimensions) == set(DIMENSIONS)
    assert rubric.dimensions["customer_frustration"].inverted


def test_overall_score_extremes(rubric: Rubric) -> None:
    # Best case: everything 5 except frustration, which is inverted (1 = none).
    assert rubric.overall_score(_scores(5, customer_frustration=1)) == 100
    assert rubric.overall_score(_scores(1, customer_frustration=5)) == 0
    assert rubric.overall_score({}) is None


def test_overall_score_renormalizes_missing_dimensions(rubric: Rubric) -> None:
    full = _scores(4, customer_frustration=2)
    partial = dict(full)
    del partial["efficiency"]
    assert rubric.overall_score(full) == rubric.overall_score(partial) == 75


def test_policy_violation_caps_the_score(rubric: Rubric) -> None:
    assert rubric.overall_score(_scores(5, customer_frustration=1, policy_adherence=1)) == 40


def test_weights_must_sum_to_one() -> None:
    data = yaml.safe_load(DEFAULT_RUBRIC_PATH.read_text())
    data["dimensions"]["resolution"]["weight"] = 0.5
    with pytest.raises(ValidationError, match=r"sum to 1\.0"):
        Rubric.model_validate(data)


def test_missing_dimension_is_rejected() -> None:
    data = yaml.safe_load(DEFAULT_RUBRIC_PATH.read_text())
    del data["dimensions"]["efficiency"]
    with pytest.raises(ValidationError, match="missing dimensions"):
        Rubric.model_validate(data)
