"""Filesystem anchors shared by the runner, reporter and change-impact detector."""

from __future__ import annotations

import os
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
SUITES_DIR = EVALS_DIR / "suites"
SUITE_SCHEMA_PATH = EVALS_DIR / "suite.schema.json"
RESULTS_DIR = Path(os.environ.get("EVALS_RESULTS_DIR", EVALS_DIR / ".results"))
