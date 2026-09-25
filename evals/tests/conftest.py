"""Fixtures: throwaway git repositories that mirror this repo's behaviour-relevant layout.

The template repo is built once per session from the *real* ``agent/``, ``prompts/``,
``evals/`` and ``uv.lock`` (so the detector is exercised against the real PromptComposer and
tool registry), then copied per test so each test can mutate its own working tree.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from evals.paths import REPO_ROOT

_COPY = (
    "agent/voice_agent",
    "prompts",
    "evals/suites",
    "evals/runners",
    "evals/schema.py",
    "evals/toolschema.py",
    "evals/cli.py",
    "evals/__init__.py",
    "uv.lock",
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="session")
def template_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not (REPO_ROOT / "agent/voice_agent/prompts/composer.py").is_file():
        pytest.skip("voice_agent composer not present yet")
    repo = tmp_path_factory.mktemp("template") / "repo"
    repo.mkdir()
    for rel in _COPY:
        src = REPO_ROOT / rel
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif src.is_file():
            shutil.copy2(src, dst)
    (repo / "docs").mkdir()
    (repo / "docs" / "architecture.md").write_text("# Architecture\n")
    (repo / "frontend").mkdir()
    (repo / "frontend" / "page.tsx").write_text("export default function Page() {}\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "evals@example.com")
    git(repo, "config", "user.name", "evals")
    git(repo, "config", "commit.gpgsign", "false")
    commit_all(repo, "base")
    return repo


@pytest.fixture
def repo(template_repo: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "repo"
    shutil.copytree(template_repo, dst)
    return dst


@pytest.fixture
def edit(repo: Path) -> Callable[[str, Callable[[str], str]], None]:
    """``edit(relpath, fn)`` rewrites a file in the fixture repo's working tree."""

    def _edit(rel: str, fn: Callable[[str], str]) -> None:
        path = repo / rel
        if not path.is_file():
            pytest.skip(f"{rel} not present in this checkout")
        new = fn(path.read_text(encoding="utf-8"))
        assert new != path.read_text(encoding="utf-8"), f"edit to {rel} was a no-op"
        path.write_text(new, encoding="utf-8")

    return _edit
