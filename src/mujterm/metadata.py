from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .models import (
    AgentKind,
    AgentState,
    AgentStatus,
    GitInfo,
    ListeningService,
    PaneInfo,
    TerminalSnapshot,
)
from .paths import state_dir


@dataclass(frozen=True)
class _ProcessRecord:
    ppid: int
    ticks: int
    rss_bytes: int


class ProcessUsageSampler:
    """Samples resource usage and agents from one process-table scan."""

    def __init__(self) -> None:
        self._previous_ticks: dict[int, int] = {}
        self._previous_time: Optional[float] = None
        self._clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        self._page_size = int(os.sysconf("SC_PAGE_SIZE"))
        self.agents: dict[int, Optional[AgentKind]] = {}

    def sample(
        self, root_pids: Iterable[int]
    ) -> dict[int, tuple[float, int, tuple[ListeningService, ...], tuple[int, ...]]]:
        roots = list(root_pids)
        processes = self._read_processes()
        children: dict[int, list[int]] = defaultdict(list)
        for pid, process in processes.items():
            children[process.ppid].append(pid)

        now = time.monotonic()
        elapsed = now - self._previous_time if self._previous_time is not None else 0.0
        listening_sockets, connected_sockets = self._socket_tables()
        output: dict[int, tuple[float, int, tuple[ListeningService, ...], tuple[int, ...]]] = {}
        relevant: set[int] = set()
        for root in roots:
            tree = self._process_tree(root, children)
            relevant.update(tree)
            memory_bytes = sum(
                processes[pid].rss_bytes for pid in tree if pid in processes
            )
            delta_ticks = sum(
                max(
                    0,
                    processes[pid].ticks
                    - self._previous_ticks.get(pid, processes[pid].ticks),
                )
                for pid in tree
                if pid in processes
            )
            cpu_percent = (
                delta_ticks / self._clock_ticks / elapsed * 100.0
                if elapsed > 0
                else 0.0
            )
            services, connected_ports = self._network_for_tree(
                tree, listening_sockets, connected_sockets
            )
            output[root] = (cpu_percent, memory_bytes, services, connected_ports)

        signatures = self._read_signatures(relevant)
        self.agents = _agents_for_roots(roots, children, signatures)
        self._previous_ticks = {
            pid: processes[pid].ticks
            for pid in relevant
            if pid in processes
        }
        self._previous_time = now
        return output

    def _read_processes(self) -> dict[int, _ProcessRecord]:
        processes: dict[int, _ProcessRecord] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
                close = stat.rfind(")")
                fields = stat[close + 2 :].split()
                pid = int(entry.name)
                ppid = int(fields[1])
                ticks = int(fields[11]) + int(fields[12])
                rss_bytes = max(0, int(fields[21])) * self._page_size
            except (OSError, ValueError, IndexError):
                continue
            processes[pid] = _ProcessRecord(ppid, ticks, rss_bytes)
        return processes

    @staticmethod
    def _read_signatures(pids: Iterable[int]) -> dict[int, str]:
        signatures: dict[int, str] = {}
        for pid in pids:
            process = Path("/proc") / str(pid)
            try:
                cmdline = process.joinpath("cmdline").read_bytes().replace(
                    b"\0", b" "
                ).decode("utf-8", errors="replace")
                comm = process.joinpath("comm").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            signatures[pid] = f"{comm} {cmdline}".lower()
        return signatures

    @staticmethod
    def _socket_tables() -> tuple[dict[str, int], dict[str, int]]:
        listening: dict[str, int] = {}
        connected: dict[str, int] = {}
        for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 10 or fields[3] not in {"0A", "01"}:
                    continue
                try:
                    address = fields[1] if fields[3] == "0A" else fields[2]
                    port = int(address.rsplit(":", 1)[1], 16)
                except (ValueError, IndexError):
                    continue
                if port:
                    (listening if fields[3] == "0A" else connected)[fields[9]] = port
        return listening, connected

    @staticmethod
    def _network_for_tree(
        tree: set[int], listening_sockets: dict[str, int], connected_sockets: dict[str, int]
    ) -> tuple[tuple[ListeningService, ...], tuple[int, ...]]:
        by_port: dict[int, ListeningService] = {}
        connected_ports: set[int] = set()
        for pid in tree:
            try:
                descriptors = (Path("/proc") / str(pid) / "fd").iterdir()
                for descriptor in descriptors:
                    try:
                        target = os.readlink(descriptor)
                    except OSError:
                        continue
                    match = re.fullmatch(r"socket:\[(\d+)\]", target)
                    if not match:
                        continue
                    port = listening_sockets.get(match.group(1))
                    if port and port not in by_port:
                        by_port[port] = ListeningService(port=port, pid=pid)
                    remote_port = connected_sockets.get(match.group(1))
                    if remote_port:
                        connected_ports.add(remote_port)
            except OSError:
                continue
        return (
            tuple(by_port[port] for port in sorted(by_port)),
            tuple(sorted(connected_ports)),
        )

    @staticmethod
    def _process_tree(root: int, children: dict[int, list[int]]) -> set[int]:
        tree: set[int] = set()
        queue = deque([root])
        while queue:
            pid = queue.popleft()
            if pid in tree:
                continue
            tree.add(pid)
            queue.extend(children.get(pid, ()))
        return tree


def git_info(cwd: str) -> GitInfo:
    path = Path(cwd)
    if not path.is_dir():
        return GitInfo(None, None)
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return GitInfo(None, None)
    branch = _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        commit = _git(cwd, "rev-parse", "--short", "HEAD")
        branch = f"detached@{commit}" if commit else "detached"
    return GitInfo(root, branch)


class GitInfoCache:
    """Short-lived Git metadata cache shared by consecutive refreshes."""

    def __init__(self, ttl: float = 3.0, max_entries: int = 256) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: dict[str, tuple[float, GitInfo]] = {}

    def get(self, cwd: str) -> GitInfo:
        now = time.monotonic()
        cached = self._entries.get(cwd)
        if cached and now - cached[0] < self.ttl:
            return cached[1]
        info = git_info(cwd)
        self._entries[cwd] = (now, info)
        if len(self._entries) > self.max_entries:
            oldest = min(self._entries, key=lambda path: self._entries[path][0])
            self._entries.pop(oldest, None)
        return info

    def invalidate(self, cwd: Optional[str] = None) -> None:
        if cwd is None:
            self._entries.clear()
        else:
            self._entries.pop(cwd, None)


def _git(cwd: str, *arguments: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *arguments],
            check=False,
            text=True,
            capture_output=True,
            timeout=0.75,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def load_agent_state(terminal_id: str, root: Optional[Path] = None) -> Optional[AgentState]:
    path = (root or state_dir()) / "agents" / f"{terminal_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AgentState(
            terminal_id=terminal_id,
            agent=AgentKind(payload["agent"]),
            status=AgentStatus(payload["status"]),
            event=str(payload.get("event", "")),
            timestamp=float(payload["timestamp"]),
            cwd=payload.get("cwd"),
            session_id=payload.get("session_id"),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def remove_agent_state(terminal_id: str, root: Optional[Path] = None) -> None:
    path = (root or state_dir()) / "agents" / f"{terminal_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def process_agents(root_pids: Iterable[int]) -> dict[int, Optional[AgentKind]]:
    roots = list(root_pids)
    children: dict[int, list[int]] = defaultdict(list)
    signatures: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            close = stat.rfind(")")
            fields = stat[close + 2 :].split()
            ppid = int(fields[1])
            pid = int(entry.name)
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except (OSError, ValueError, IndexError):
            continue
        children[ppid].append(pid)
        signatures[pid] = f"{comm} {cmdline}".lower()

    return _agents_for_roots(roots, children, signatures)


def _agents_for_roots(
    roots: Iterable[int],
    children: dict[int, list[int]],
    signatures: dict[int, str],
) -> dict[int, Optional[AgentKind]]:
    output: dict[int, Optional[AgentKind]] = {}
    for root in roots:
        found: Optional[AgentKind] = None
        queue = deque([root])
        visited: set[int] = set()
        while queue:
            pid = queue.popleft()
            if pid in visited:
                continue
            visited.add(pid)
            signature = signatures.get(pid, "")
            if _is_codex(signature):
                found = AgentKind.CODEX
            elif _is_claude(signature):
                found = AgentKind.CLAUDE
            queue.extend(children.get(pid, ()))
        output[root] = found
    return output


def _is_codex(signature: str) -> bool:
    return (
        bool(re.search(r"(^|[\s/])codex(?:-linux[^\s/]*)?($|\s)", signature))
        or "@openai/codex" in signature
    ) and "mujterm-agent-hook" not in signature


def _is_claude(signature: str) -> bool:
    return (
        bool(re.search(r"(^|[\s/])claude($|\s)", signature))
        or "@anthropic-ai/claude-code" in signature
    ) and "mujterm-agent-hook" not in signature


def collect_snapshots(
    panes: dict[str, PaneInfo],
    usage: Optional[
        dict[int, tuple[float, int, tuple[ListeningService, ...], tuple[int, ...]]]
    ] = None,
    detected_agents: Optional[dict[int, Optional[AgentKind]]] = None,
    git_cache: Optional[GitInfoCache] = None,
) -> dict[str, TerminalSnapshot]:
    detected = detected_agents
    if detected is None:
        detected = process_agents(
            pane.pane_pid for pane in panes.values() if pane.pane_pid
        )
    local_git_cache: dict[str, GitInfo] = {}
    snapshots: dict[str, TerminalSnapshot] = {}
    now = time.time()
    usage = usage or {}
    for terminal_id, pane in panes.items():
        if git_cache is not None:
            info = git_cache.get(pane.cwd)
        elif pane.cwd in local_git_cache:
            info = local_git_cache[pane.cwd]
        else:
            info = git_info(pane.cwd)
            local_git_cache[pane.cwd] = info
        process_agent = detected.get(pane.pane_pid)
        if not process_agent and pane.command.lower().startswith("codex"):
            process_agent = AgentKind.CODEX
        elif not process_agent and pane.command.lower().startswith("claude"):
            process_agent = AgentKind.CLAUDE
        saved = load_agent_state(terminal_id)
        cpu_percent, memory_bytes, services, connected_ports = usage.get(
            pane.pane_pid, (0.0, 0, (), ())
        )
        agent = process_agent
        status = AgentStatus.SHELL
        if pane.dead:
            status = AgentStatus.ENDED
        elif agent and saved and saved.agent == agent:
            status = saved.status
        elif agent:
            status = AgentStatus.UNKNOWN
        elif saved and now - saved.timestamp < 5:
            agent = saved.agent
            status = saved.status
        snapshots[terminal_id] = TerminalSnapshot(
            terminal_id=terminal_id,
            cwd=pane.cwd,
            command=pane.command,
            branch=info.branch,
            git_root=info.root,
            agent=agent,
            status=status,
            cpu_percent=cpu_percent,
            memory_bytes=memory_bytes,
            services=services,
            connected_ports=connected_ports,
            dead=pane.dead,
        )
    return snapshots


def valid_terminal_id(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False
