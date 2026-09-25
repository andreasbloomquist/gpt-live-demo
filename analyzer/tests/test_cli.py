from __future__ import annotations

import json
from pathlib import Path

import pytest

from call_analyzer.__main__ import main
from tests.factories import DEMO_DIR, demo_files


@pytest.fixture(autouse=True)
def offline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "calls.db"
    monkeypatch.setenv("ANALYZER_DB_PATH", str(db))
    monkeypatch.setenv("ANALYZER_PROVIDER", "heuristic")
    monkeypatch.chdir(tmp_path)  # no stray .env file
    return db


def test_analyze_prints_analysis_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["analyze", str(demo_files()[0])]) == 0
    analysis = json.loads(capsys.readouterr().out)
    assert analysis["status"] == "done"
    assert analysis["analyzer"]["provider"] == "heuristic"


def test_seed_loads_and_analyzes_the_demo_calls(
    capsys: pytest.CaptureFixture[str], offline_env: Path
) -> None:
    assert main(["seed", "--dir", str(DEMO_DIR)]) == 0
    out = capsys.readouterr().out
    assert out.count("created") == len(demo_files())
    assert out.count(" done ") == len(demo_files())
    assert offline_env.exists()

    # Seeding again is a no-op; --reanalyze re-runs the analyses.
    assert main(["seed", "--dir", str(DEMO_DIR), "--reanalyze"]) == 0
    out = capsys.readouterr().out
    assert out.count("duplicate") == len(demo_files())
    assert out.count(" done ") == len(demo_files())


def test_openai_without_key_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANALYZER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["analyze", str(demo_files()[0]), "--provider", "openai"]) == 2
    assert "ANALYZER_API_KEY" in capsys.readouterr().err


def test_invalid_record_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 1}')
    assert main(["analyze", str(bad)]) == 1
    assert "Invalid call record" in capsys.readouterr().err
