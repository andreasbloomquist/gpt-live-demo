"""Thin git helpers. Trees are exported with ``git archive`` rather than ``git worktree`` because
an export needs no cleanup bookkeeping in the repo (``.git/worktrees``) and works on the
shallow-but-sufficient clones CI produces."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path


class GitError(RuntimeError):
    """A git command failed; the message carries git's stderr."""


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path) -> Path:
    return Path(git(start, "rev-parse", "--show-toplevel").strip())


def rev_parse(repo: Path, ref: str) -> str:
    if ref.startswith("-"):  # would be parsed as an option, not a revision
        raise GitError(f"invalid ref {ref!r}")
    return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()


def merge_base(repo: Path, a: str, b: str) -> str:
    """Where the PR branched off: diffing against it (not the base branch tip) means commits
    that landed on main after branching are not attributed to the PR."""
    return git(repo, "merge-base", a, b).strip()


def export_tree(repo: Path, ref: str, dest: Path) -> Path:
    """Materialise ``ref`` into ``dest`` (tracked files only, no ``.git``)."""
    dest.mkdir(parents=True, exist_ok=True)
    with subprocess.Popen(
        ["git", "archive", "--format=tar", ref],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        assert proc.stdout is not None and proc.stderr is not None  # both are PIPEs
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                _extract_all(tar, dest)
        except tarfile.TarError as exc:
            proc.kill()
            raise GitError(f"git archive {ref} failed: {_stderr(proc)}") from exc
        finally:
            proc.stdout.close()
        if proc.wait() != 0:
            raise GitError(f"git archive {ref} failed: {_stderr(proc)}")
    return dest


def _extract_all(tar: tarfile.TarFile, dest: Path) -> None:
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")
    else:
        # Python < 3.11.4 has no extraction filters. The tree is PR-controlled, but GitHub
        # fscks pushed trees (no "..", ".git" or absolute entries), and a tree cannot hold both
        # a symlink ``x`` and a file ``x/y`` to write through it.
        tar.extractall(dest)


def _stderr(proc: subprocess.Popen[bytes]) -> str:
    assert proc.stderr is not None
    return proc.stderr.read().decode().strip()


def changed_files(repo: Path, base: str, head: str | None) -> list[str]:
    """Files differing between ``base`` and ``head`` (or the working tree, incl. untracked).

    ``-z`` matters: without it git C-quotes unusual paths (``"agent/caf\\303\\251.py"``), and a
    quoted path no longer starts with ``agent/``, so the fast path would call it irrelevant.
    """
    if head is not None:
        out = git(repo, "diff", "-z", "--name-only", "--no-renames", base, head)
    else:
        out = git(repo, "diff", "-z", "--name-only", "--no-renames", base)
        out += git(repo, "ls-files", "-z", "--others", "--exclude-standard")
    return sorted({path for path in out.split("\0") if path})
