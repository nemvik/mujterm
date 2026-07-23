from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Iterable, Optional

from .models import AgentRace, Project, TerminalSession, TimelineEvent
from .paths import data_dir, ensure_private_dir


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    position INTEGER NOT NULL,
    collapsed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS terminals (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    tmux_name TEXT NOT NULL UNIQUE,
    initial_cwd TEXT NOT NULL,
    last_cwd TEXT NOT NULL,
    position INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS terminals_project_position
ON terminals(project_id, position);

CREATE TABLE IF NOT EXISTS timeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    terminal_id TEXT,
    terminal_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS timeline_created_at
ON timeline_events(created_at DESC);

CREATE INDEX IF NOT EXISTS timeline_project_created_at
ON timeline_events(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_races (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    codex_branch TEXT NOT NULL,
    claude_branch TEXT NOT NULL,
    codex_path TEXT NOT NULL,
    claude_path TEXT NOT NULL,
    codex_terminal_id TEXT NOT NULL,
    claude_terminal_id TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS agent_races_project_created_at
ON agent_races(project_id, created_at DESC);
"""


class Database:
    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            path = ensure_private_dir(data_dir()) / "state.db"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def list_projects(self) -> list[Project]:
        rows = self.connection.execute(
            "SELECT * FROM projects ORDER BY position, name COLLATE NOCASE"
        ).fetchall()
        return [self._project(row) for row in rows]

    def get_project(self, project_id: str) -> Optional[Project]:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return self._project(row) if row else None

    def find_project_by_root(self, root_path: str) -> Optional[Project]:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE root_path = ?", (root_path,)
        ).fetchone()
        return self._project(row) if row else None

    def create_project(self, name: str, root_path: str) -> Project:
        project = Project(
            id=str(uuid.uuid4()),
            name=name,
            root_path=root_path,
            position=self._next_position("projects", None),
        )
        self.connection.execute(
            "INSERT INTO projects(id, name, root_path, position, collapsed) VALUES (?, ?, ?, ?, 0)",
            (project.id, project.name, project.root_path, project.position),
        )
        self.connection.commit()
        return project

    def rename_project(self, project_id: str, name: str) -> None:
        self.connection.execute(
            "UPDATE projects SET name = ? WHERE id = ?", (name, project_id)
        )
        self.connection.commit()

    def set_project_collapsed(self, project_id: str, collapsed: bool) -> None:
        self.connection.execute(
            "UPDATE projects SET collapsed = ? WHERE id = ?",
            (int(collapsed), project_id),
        )
        self.connection.commit()

    def reorder_projects(self, ordered_ids: Iterable[str]) -> None:
        with self.connection:
            for position, project_id in enumerate(ordered_ids):
                self.connection.execute(
                    "UPDATE projects SET position = ? WHERE id = ?",
                    (position, project_id),
                )

    def delete_project(self, project_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE terminals SET project_id = NULL WHERE project_id = ?",
                (project_id,),
            )
            self.connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self.normalize_terminal_positions(None)

    def list_terminals(self, project_id: Optional[str] = None) -> list[TerminalSession]:
        if project_id is None:
            rows = self.connection.execute(
                "SELECT * FROM terminals ORDER BY COALESCE(project_id, ''), position, name COLLATE NOCASE"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM terminals WHERE project_id = ? ORDER BY position, name COLLATE NOCASE",
                (project_id,),
            ).fetchall()
        return [self._terminal(row) for row in rows]

    def list_ungrouped_terminals(self) -> list[TerminalSession]:
        rows = self.connection.execute(
            "SELECT * FROM terminals WHERE project_id IS NULL ORDER BY position, name COLLATE NOCASE"
        ).fetchall()
        return [self._terminal(row) for row in rows]

    def get_terminal(self, terminal_id: str) -> Optional[TerminalSession]:
        row = self.connection.execute(
            "SELECT * FROM terminals WHERE id = ?", (terminal_id,)
        ).fetchone()
        return self._terminal(row) if row else None

    def create_terminal(
        self,
        project_id: Optional[str],
        name: str,
        cwd: str,
        terminal_id: Optional[str] = None,
        tmux_name: Optional[str] = None,
    ) -> TerminalSession:
        terminal_id = terminal_id or str(uuid.uuid4())
        tmux_name = tmux_name or f"mujterm-{uuid.UUID(terminal_id).hex}"
        terminal = TerminalSession(
            id=terminal_id,
            project_id=project_id,
            name=name,
            tmux_name=tmux_name,
            initial_cwd=cwd,
            last_cwd=cwd,
            position=self._next_terminal_position(project_id),
        )
        self.connection.execute(
            """
            INSERT INTO terminals(id, project_id, name, tmux_name, initial_cwd, last_cwd, position)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                terminal.id,
                terminal.project_id,
                terminal.name,
                terminal.tmux_name,
                terminal.initial_cwd,
                terminal.last_cwd,
                terminal.position,
            ),
        )
        self.connection.commit()
        return terminal

    def rename_terminal(self, terminal_id: str, name: str) -> None:
        self.connection.execute(
            "UPDATE terminals SET name = ? WHERE id = ?", (name, terminal_id)
        )
        self.connection.commit()

    def update_terminal_cwd(self, terminal_id: str, cwd: str) -> None:
        self.connection.execute(
            "UPDATE terminals SET last_cwd = ? WHERE id = ?", (cwd, terminal_id)
        )
        self.connection.commit()

    def move_terminal(
        self,
        terminal_id: str,
        project_id: Optional[str],
        before_terminal_id: Optional[str] = None,
    ) -> None:
        terminal = self.get_terminal(terminal_id)
        if not terminal:
            return
        old_project_id = terminal.project_id
        destination = (
            self.list_ungrouped_terminals()
            if project_id is None
            else [t for t in self.list_terminals(project_id) if t.project_id == project_id]
        )
        destination = [t for t in destination if t.id != terminal_id]
        insert_at = len(destination)
        if before_terminal_id:
            insert_at = next(
                (i for i, item in enumerate(destination) if item.id == before_terminal_id),
                len(destination),
            )
        destination.insert(insert_at, terminal)
        with self.connection:
            self.connection.execute(
                "UPDATE terminals SET project_id = ? WHERE id = ?",
                (project_id, terminal_id),
            )
            for position, item in enumerate(destination):
                self.connection.execute(
                    "UPDATE terminals SET position = ? WHERE id = ?",
                    (position, item.id),
                )
        if old_project_id != project_id:
            self.normalize_terminal_positions(old_project_id)

    def delete_terminal(self, terminal_id: str) -> None:
        terminal = self.get_terminal(terminal_id)
        self.connection.execute("DELETE FROM terminals WHERE id = ?", (terminal_id,))
        self.connection.commit()
        if terminal:
            self.normalize_terminal_positions(terminal.project_id)

    def append_timeline_event(
        self,
        project_id: Optional[str],
        terminal_id: Optional[str],
        terminal_name: str,
        kind: str,
        summary: str,
        created_at: float,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO timeline_events(
                project_id, terminal_id, terminal_name, kind, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, terminal_id, terminal_name, kind, summary, created_at),
        )
        self.connection.execute(
            """
            DELETE FROM timeline_events
            WHERE id NOT IN (
                SELECT id FROM timeline_events ORDER BY created_at DESC LIMIT 2000
            )
            """
        )
        self.connection.commit()

    def list_timeline_events(
        self,
        project_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[TimelineEvent]:
        if project_id is None:
            rows = self.connection.execute(
                "SELECT * FROM timeline_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM timeline_events
                WHERE project_id = ? ORDER BY created_at DESC LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [
            TimelineEvent(
                id=row["id"],
                project_id=row["project_id"],
                terminal_id=row["terminal_id"],
                terminal_name=row["terminal_name"],
                kind=row["kind"],
                summary=row["summary"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def create_agent_race(self, race: AgentRace) -> None:
        self.connection.execute(
            """
            INSERT INTO agent_races(
                id, project_id, task, base_commit, codex_branch, claude_branch,
                codex_path, claude_path, codex_terminal_id, claude_terminal_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                race.id, race.project_id, race.task, race.base_commit,
                race.codex_branch, race.claude_branch, race.codex_path,
                race.claude_path, race.codex_terminal_id,
                race.claude_terminal_id, race.created_at,
            ),
        )
        self.connection.commit()

    def get_agent_race(self, race_id: str) -> Optional[AgentRace]:
        row = self.connection.execute(
            "SELECT * FROM agent_races WHERE id = ?", (race_id,)
        ).fetchone()
        return self._agent_race(row) if row else None

    def list_agent_races(self, project_id: str, limit: int = 20) -> list[AgentRace]:
        rows = self.connection.execute(
            """
            SELECT * FROM agent_races WHERE project_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
        return [self._agent_race(row) for row in rows]

    def normalize_terminal_positions(self, project_id: Optional[str]) -> None:
        terminals = (
            self.list_ungrouped_terminals()
            if project_id is None
            else [t for t in self.list_terminals(project_id) if t.project_id == project_id]
        )
        with self.connection:
            for position, terminal in enumerate(terminals):
                self.connection.execute(
                    "UPDATE terminals SET position = ? WHERE id = ?",
                    (position, terminal.id),
                )

    def _next_position(self, table: str, _group: Optional[str]) -> int:
        row = self.connection.execute(
            f"SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM {table}"
        ).fetchone()
        return int(row["next_position"])

    def _next_terminal_position(self, project_id: Optional[str]) -> int:
        if project_id is None:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM terminals WHERE project_id IS NULL"
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM terminals WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return int(row["next_position"])

    @staticmethod
    def _project(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            root_path=row["root_path"],
            position=row["position"],
            collapsed=bool(row["collapsed"]),
        )

    @staticmethod
    def _terminal(row: sqlite3.Row) -> TerminalSession:
        return TerminalSession(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            tmux_name=row["tmux_name"],
            initial_cwd=row["initial_cwd"],
            last_cwd=row["last_cwd"],
            position=row["position"],
        )

    @staticmethod
    def _agent_race(row: sqlite3.Row) -> AgentRace:
        return AgentRace(
            id=row["id"], project_id=row["project_id"], task=row["task"],
            base_commit=row["base_commit"], codex_branch=row["codex_branch"],
            claude_branch=row["claude_branch"], codex_path=row["codex_path"],
            claude_path=row["claude_path"],
            codex_terminal_id=row["codex_terminal_id"],
            claude_terminal_id=row["claude_terminal_id"],
            created_at=row["created_at"],
        )
