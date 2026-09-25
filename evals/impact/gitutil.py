"""Thin git helpers. Trees are exported with ``git archive`` rather than ``git worktree`` because
an export needs no cleanup bookkeeping in the repo (``.git/worktrees``) and works on the
shallow-but-sufficient clones CI produces."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path) -> Path:
    return Path(git(start, "rev-parse", "--show-toplevel").strip())


def rev_parse(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def merge_base(repo: Path, a: str, b: str) -> str:
    """Where the PR branched off: diffing against it (not the base branch tip) means commits
    that landed on main after branching are not attributed to the PR."""
    return git(repo, "merge-base", a, b).strip()


def export_tree(repo: Path, ref: str, dest: Path) -> Path:
    """Materialise ``ref`` into ``dest`` (tracked files only, no ``.git``)."""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["git", "archive", "--format=tar", ref], cwd=repo, stdout=subprocess.PIPE
    )
    assert proc.stdout is not None
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # Python < 3.11.4
            tar.extractall(dest)  # trusted input: our own repository
    if proc.wait() != 0:
        raise GitError(f"git archive {ref} failed")
    return dest


def changed_files(repo: Path, base: str, head: str | None) -> list[str]:
    """Files differing between ``base`` and ``head`` (or the working tree, incl. untracked)."""
    if head is not None:
        out = git(repo, "diff", "--name-only", "--no-renames", base, head)
    else:
        out = git(repo, "diff", "--name-only", "--no-renames", base)
        out += git(repo, "ls-files", "--others", "--exclude-standard")
    return sorted({line for line in out.splitlines() if line.strip()})
