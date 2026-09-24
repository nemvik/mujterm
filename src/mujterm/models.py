from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AgentKind(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"


class AgentStatus(str, Enum):
    SHELL = "shell"
    WORKING = "working"
    NEEDS_ACTION = "needs_action"
    READY = "ready"
    ERROR = "error"
    ENDED = "ended"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root_path: str
    position: int
    collapsed: bool = False


@dataclass(frozen=True)
class SshConnection:
    project_id: str
    target: str
    port: Optional[int] = None


@dataclass(frozen=True)
class TerminalSession:
    id: str
    project_id: Optional[str]
    name: str
    tmux_name: str
    initial_cwd: str
    last_cwd: str
    position: int


@dataclass(frozen=True)
class ToolboxCommand:
    id: str
    name: str
    command: str
    position: int


@dataclass(frozen=True)
class PaneInfo:
    terminal_id: str
    tmux_name: str
    cwd: str
    command: str
    pane_pid: int
    dead: bool


@dataclass(frozen=True)
class GitInfo:
    root: Optional[str]
    branch: Optional[str]


@dataclass(frozen=True)
class AgentState:
    terminal_id: str
    agent: AgentKind
    status: AgentStatus
    event: str
    timestamp: float
    cwd: Optional[str] = None
    session_id: Optional[str] = None


@dataclass(frozen=True)
class ListeningService:
    port: int
    pid: int


@dataclass(frozen=True)
class AgentRace:
    id: str
    project_id: str
    task: str
    base_commit: str
    codex_branch: str
    claude_branch: str
    codex_path: str
    claude_path: str
    codex_terminal_id: str
    claude_terminal_id: str
    created_at: float


@dataclass(frozen=True)
class TerminalSnapshot:
    terminal_id: str
    cwd: str
    command: str
    branch: Optional[str]
    git_root: Optional[str]
    agent: Optional[AgentKind]
    status: AgentStatus
    cpu_percent: float = 0.0
    memory_bytes: int = 0
    services: tuple[ListeningService, ...] = ()
    dead: bool = False
