from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class GitFileState:
    status: str
    size: int
    modified_ns: int


@dataclass(frozen=True)
class GitImpactSnapshot:
    root: str
    branch: Optional[str]
    head: Optional[str]
    files: tuple[tuple[str, GitFileState], ...]
    truncated: bool = False


@dataclass(frozen=True)
class GitImpact:
    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()
    branch_before: Optional[str] = None
    branch_after: Optional[str] = None
    head_before: Optional[str] = None
    head_after: Optional[str] = None
    repository_before: Optional[str] = None
    repository_after: Optional[str] = None
    truncated: bool = False

    @property
    def changed_files(self) -> tuple[str, ...]:
        return self.created + self.modified + self.deleted + self.resolved

    @property
    def branch_changed(self) -> bool:
        return self.branch_before != self.branch_after

    @property
    def commit_changed(self) -> bool:
        return bool(
            self.head_before
            and self.head_after
            and self.head_before != self.head_after
        )

    @property
    def repository_changed(self) -> bool:
        return self.repository_before != self.repository_after


def _git(
    cwd: str,
    arguments: list[str],
    timeout: float,
) -> Optional[bytes]:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _discover_root(cwd: str, timeout: float) -> Optional[str]:
    output = _git(cwd, ["rev-parse", "--show-toplevel"], timeout)
    if output is None:
        return None
    value = _decode(output).strip()
    return value if value and Path(value).is_dir() else None


def _branch_and_head(root: str, timeout: float) -> tuple[Optional[str], Optional[str]]:
    output = _git(root, ["rev-parse", "--abbrev-ref", "HEAD", "HEAD"], timeout)
    if output is None:
        return None, None
    lines = [line.strip() for line in _decode(output).splitlines() if line.strip()]
    if len(lines) < 2:
        return None, None
    branch = None if lines[0] == "HEAD" else lines[0]
    return branch, lines[1]


def _status_entries(output: bytes) -> list[tuple[str, str]]:
    fields = output.split(b"\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        raw = fields[index]
        index += 1
        if not raw:
            continue
        text = _decode(raw)
        if len(text) < 4:
            continue
        status = text[:2]
        path = text[3:]
        entries.append((path, status))
        if "R" in status or "C" in status:
            if index < len(fields) and fields[index]:
                entries.append((_decode(fields[index]), "D "))
                index += 1
    return entries


def capture_git_impact(
    cwd: str,
    known_root: Optional[str] = None,
    max_files: int = 300,
    timeout: float = 0.6,
) -> Optional[GitImpactSnapshot]:
    """Capture bounded Git metadata without reading or retaining file contents."""
    root = known_root if known_root and Path(known_root).is_dir() else None
    root = root or _discover_root(cwd, timeout)
    if not root:
        return None
    status = _git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=normal"],
        timeout,
    )
    if status is None:
        return None
    branch, head = _branch_and_head(root, timeout)
    entries = _status_entries(status)
    truncated = len(entries) > max_files
    states: list[tuple[str, GitFileState]] = []
    for path, file_status in sorted(entries)[:max_files]:
        target = Path(root) / path
        try:
            stat = target.lstat()
            size = stat.st_size
            modified_ns = stat.st_mtime_ns
        except OSError:
            size = -1
            modified_ns = -1
        states.append((path, GitFileState(file_status, size, modified_ns)))
    return GitImpactSnapshot(
        root=str(Path(root).resolve()),
        branch=branch,
        head=head,
        files=tuple(states),
        truncated=truncated,
    )


def compare_git_impact(
    before: Optional[GitImpactSnapshot],
    after: Optional[GitImpactSnapshot],
) -> GitImpact:
    before_files = dict(before.files) if before else {}
    after_files = dict(after.files) if after else {}
    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    resolved: list[str] = []
    for path in sorted(before_files.keys() | after_files.keys()):
        old = before_files.get(path)
        new = after_files.get(path)
        if old == new:
            continue
        if new is None:
            resolved.append(path)
        elif "?" in new.status or "A" in new.status:
            created.append(path)
        elif "D" in new.status:
            deleted.append(path)
        else:
            modified.append(path)
    return GitImpact(
        created=tuple(created),
        modified=tuple(modified),
        deleted=tuple(deleted),
        resolved=tuple(resolved),
        branch_before=before.branch if before else None,
        branch_after=after.branch if after else None,
        head_before=before.head if before else None,
        head_after=after.head if after else None,
        repository_before=before.root if before else None,
        repository_after=after.root if after else None,
        truncated=bool((before and before.truncated) or (after and after.truncated)),
    )
