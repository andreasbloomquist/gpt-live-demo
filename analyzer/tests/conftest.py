"""Shared fixtures. Nothing here touches the network or needs an API key."""

from __future__ import annotations

import pytest

from call_analyzer.analysis import CallAnalyzer
from call_analyzer.providers.heuristic import HeuristicProvider
from call_analyzer.rubric import Rubric, default_rubric


@pytest.fixture
def rubric() -> Rubric:
    return default_rubric()


@pytest.fixture
def heuristic_analyzer(rubric: Rubric) -> CallAnalyzer:
    return CallAnalyzer(HeuristicProvider(), rubric)
