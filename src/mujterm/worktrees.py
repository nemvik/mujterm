from __future__ import annotations

import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .paths import data_dir, ensure_private_dir


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class RaceWorktrees:
    id: str
    base_commit: str
    codex_branch: str
    claude_branch: str
    codex_path: str
    claude_path: str
    created_at: float


@dataclass(frozen=True)
class GitComparison:
    files: int
    additions: int
    deletions: int
    dirty_files: int
    summary: str


def create_race_worktrees(project_id: str, project_root: str, task: str) -> RaceWorktrees:
    repo = _git(project_root, "rev-parse", "--show-toplevel")
    if not repo:
        raise WorktreeError("This project is not a Git repository.")
    base = _git(repo, "rev-parse", "HEAD")
    if not base:
        raise WorktreeError("The repository needs at least one commit before starting a race.")

    race_id = uuid.uuid4().hex[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:24] or "task"
    codex_branch = f"mujterm/codex-{slug}-{race_id}"
    claude_branch = f"mujterm/claude-{slug}-{race_id}"
    root = ensure_private_dir(data_dir() / "worktrees" / project_id / race_id)
    codex_path = root / "codex"
    claude_path = root / "claude"

    _add_worktree(repo, codex_path, codex_branch, base)
    try:
        _add_worktree(repo, claude_path, claude_branch, base)
    except WorktreeError:
        _run_git(repo, "worktree", "remove", "--force", str(codex_path))
        _run_git(repo, "branch", "-D", codex_branch)
        raise
    return RaceWorktrees(
        id=race_id,
        base_commit=base,
        codex_branch=codex_branch,
        claude_branch=claude_branch,
        codex_path=str(codex_path),
        claude_path=str(claude_path),
        created_at=time.time(),
    )


def compare_worktree(path: str, base_commit: str) -> GitComparison:
    numstat = _git(path, "diff", "--numstat", base_commit) or ""
    files = additions = deletions = 0
    for line in numstat.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        files += 1
        if fields[0].isdigit():
            additions += int(fields[0])
        if fields[1].isdigit():
            deletions += int(fields[1])
    status = _git(path, "status", "--porcelain", "--untracked-files=normal") or ""
    dirty = len(status.splitlines())
    summary = _git(path, "log", "-1", "--pretty=%h · %s") or "No commit"
    return GitComparison(files, additions, deletions, dirty, summary)


def _add_worktree(repo: str, path: Path, branch: str, base: str) -> None:
    result = _run_git(repo, "worktree", "add", "-b", branch, str(path), base)
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or "Could not create Git worktree.")


def _git(cwd: str, *arguments: str) -> str | None:
    result = _run_git(cwd, *arguments)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _run_git(cwd: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", cwd, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise WorktreeError(f"Git operation failed: {exc}") from exc
