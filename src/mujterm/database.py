from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .models import (
    AgentRace,
    Project,
    SshConnection,
    TerminalSession,
    TimelineEvent,
    ToolboxCommand,
)
from .paths import data_dir, ensure_private_dir


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        root_path TEXT NOT NULL,
        position INTEGER NOT NULL,
        collapsed INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ssh_projects (
        project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        target TEXT NOT NULL,
        port INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS terminals (
        id TEXT PRIMARY KEY,
        project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        tmux_name TEXT NOT NULL UNIQUE,
        initial_cwd TEXT NOT NULL,
        last_cwd TEXT NOT NULL,
        position INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS terminals_project_position
    ON terminals(project_id, position)
    """,
    """
    CREATE TABLE IF NOT EXISTS toolbox_commands (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        command TEXT NOT NULL,
        position INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS toolbox_commands_position
    ON toolbox_commands(position)
    """,
    """
    CREATE TABLE IF NOT EXISTS timeline_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT,
        terminal_id TEXT,
        terminal_name TEXT NOT NULL,
        kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS timeline_created_at
    ON timeline_events(created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS timeline_project_created_at
    ON timeline_events(project_id, created_at DESC)
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS agent_races_project_created_at
    ON agent_races(project_id, created_at DESC)
    """,
)
MIGRATIONS = {1: SCHEMA_STATEMENTS}
# Kept as a convenient representation for fixtures and external diagnostics.
SCHEMA = ";\n".join(statement.strip() for statement in SCHEMA_STATEMENTS) + ";\n"


class DatabaseError(RuntimeError):
    """The state database could not be opened or migrated safely."""


class Database:
    def __init__(self, path: Optional[Path] = None) -> None:
        try:
            if path is None:
                path = ensure_private_dir(data_dir()) / "state.db"
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
            database_existed = path.exists() and path.stat().st_size > 0
        except OSError as exc:
            raise DatabaseError(f"Could not prepare state database: {exc}") from exc
        self.path = path
        self.last_backup_path: Optional[Path] = None
        self.integrity_status = "unchecked"
        try:
            self.connection = sqlite3.connect(path, timeout=5)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            self.integrity_status = self._check_integrity()
            if self.integrity_status != "ok":
                raise DatabaseError(
                    f"Database integrity check failed: {self.integrity_status}"
                )
            current_version = self._read_schema_version()
            if current_version > SCHEMA_VERSION:
                raise DatabaseError(
                    "The state database was created by a newer MujTerm version "
                    f"(schema {current_version}; supported {SCHEMA_VERSION})."
                )
            if current_version < SCHEMA_VERSION:
                if database_existed:
                    self.last_backup_path = self._create_migration_backup(
                        current_version, SCHEMA_VERSION
                    )
                self._apply_migrations(current_version)
            self.schema_version = self._read_schema_version()
            self.integrity_status = self._check_integrity()
            if self.integrity_status != "ok":
                raise DatabaseError(
                    "Database integrity check failed after migration: "
                    f"{self.integrity_status}"
                )
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except DatabaseError:
            if hasattr(self, "connection"):
                self.connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise DatabaseError(f"Could not open state database: {exc}") from exc

    def _read_schema_version(self) -> int:
        row = self.connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def _check_integrity(self) -> str:
        rows = self.connection.execute("PRAGMA quick_check").fetchall()
        messages = [str(row[0]) for row in rows]
        return "ok" if messages == ["ok"] else "; ".join(messages)

    def _create_migration_backup(
        self, current_version: int, target_version: int
    ) -> Path:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        stem = (
            f"{self.path.name}.backup-v{current_version}-to-v{target_version}-{stamp}"
        )
        backup_path = self.path.with_name(stem)
        suffix = 1
        while backup_path.exists():
            backup_path = self.path.with_name(f"{stem}-{suffix}")
            suffix += 1
        destination: Optional[sqlite3.Connection] = None
        try:
            destination = sqlite3.connect(backup_path)
            self.connection.backup(destination)
            destination.close()
            destination = None
            backup_path.chmod(0o600)
        except (OSError, sqlite3.Error) as exc:
            if destination is not None:
                destination.close()
            try:
                backup_path.unlink()
            except FileNotFoundError:
                pass
            raise DatabaseError(
                f"Could not create migration backup at {backup_path}: {exc}"
            ) from exc
        LOGGER.info(
            "Created database migration backup %s (schema %s -> %s)",
            backup_path,
            current_version,
            target_version,
        )
        return backup_path

    def _apply_migrations(self, current_version: int) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for target_version in range(current_version + 1, SCHEMA_VERSION + 1):
                statements = MIGRATIONS.get(target_version)
                if statements is None:
                    raise DatabaseError(
                        f"Missing database migration for schema {target_version}."
                    )
                for statement in statements:
                    self.connection.execute(statement)
                self.connection.execute(f"PRAGMA user_version = {target_version}")
            self.connection.commit()
        except Exception as exc:
            self.connection.rollback()
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError(
                f"Could not migrate database from schema {current_version} "
                f"to {SCHEMA_VERSION}: {exc}"
            ) from exc
        LOGGER.info(
            "Migrated database %s from schema %s to %s",
            self.path,
            current_version,
            SCHEMA_VERSION,
        )

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
            """
            SELECT projects.* FROM projects
            LEFT JOIN ssh_projects ON ssh_projects.project_id = projects.id
            WHERE projects.root_path = ? AND ssh_projects.project_id IS NULL
            """,
            (root_path,),
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

    def create_ssh_project(
        self,
        name: str,
        target: str,
        port: Optional[int],
        root_path: str,
    ) -> Project:
        name = name.strip()
        if not name:
            raise ValueError("Project name is required.")
        target, port = self._validate_ssh_connection(target, port)
        project = Project(
            id=str(uuid.uuid4()),
            name=name,
            root_path=root_path,
            position=self._next_position("projects", None),
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO projects(id, name, root_path, position, collapsed) VALUES (?, ?, ?, ?, 0)",
                (project.id, project.name, project.root_path, project.position),
            )
            self.connection.execute(
                "INSERT INTO ssh_projects(project_id, target, port) VALUES (?, ?, ?)",
                (project.id, target, port),
            )
        return project

    def get_ssh_connection(self, project_id: str) -> Optional[SshConnection]:
        row = self.connection.execute(
            "SELECT * FROM ssh_projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        return self._ssh_connection(row) if row else None

    def update_ssh_connection(
        self, project_id: str, target: str, port: Optional[int]
    ) -> SshConnection:
        target, port = self._validate_ssh_connection(target, port)
        cursor = self.connection.execute(
            "UPDATE ssh_projects SET target = ?, port = ? WHERE project_id = ?",
            (target, port, project_id),
        )
        self.connection.commit()
        if cursor.rowcount == 0:
            raise ValueError("SSH project no longer exists.")
        connection = self.get_ssh_connection(project_id)
        if not connection:
            raise ValueError("SSH project no longer exists.")
        return connection

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

    def update_terminal_cwds(self, updates: dict[str, str]) -> None:
        """Persist changed terminal directories in a single transaction."""
        if not updates:
            return
        with self.connection:
            self.connection.executemany(
                "UPDATE terminals SET last_cwd = ? WHERE id = ?",
                ((cwd, terminal_id) for terminal_id, cwd in updates.items()),
            )

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

    def list_toolbox_commands(self) -> list[ToolboxCommand]:
        rows = self.connection.execute(
            "SELECT * FROM toolbox_commands ORDER BY position, name COLLATE NOCASE"
        ).fetchall()
        return [self._toolbox_command(row) for row in rows]

    def get_toolbox_command(self, command_id: str) -> Optional[ToolboxCommand]:
        row = self.connection.execute(
            "SELECT * FROM toolbox_commands WHERE id = ?", (command_id,)
        ).fetchone()
        return self._toolbox_command(row) if row else None

    def create_toolbox_command(self, name: str, command: str) -> ToolboxCommand:
        name, command = self._validate_toolbox_command(name, command)
        item = ToolboxCommand(
            id=str(uuid.uuid4()),
            name=name,
            command=command,
            position=self._next_position("toolbox_commands", None),
        )
        try:
            self.connection.execute(
                "INSERT INTO toolbox_commands(id, name, command, position) VALUES (?, ?, ?, ?)",
                (item.id, item.name, item.command, item.position),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise ValueError(f'A command named "{item.name}" already exists.') from exc
        return item

    def update_toolbox_command(
        self, command_id: str, name: str, command: str
    ) -> ToolboxCommand:
        name, command = self._validate_toolbox_command(name, command)
        try:
            cursor = self.connection.execute(
                "UPDATE toolbox_commands SET name = ?, command = ? WHERE id = ?",
                (name, command, command_id),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise ValueError(f'A command named "{name}" already exists.') from exc
        if cursor.rowcount == 0:
            raise ValueError("Toolbox command no longer exists.")
        item = self.get_toolbox_command(command_id)
        if not item:
            raise ValueError("Toolbox command no longer exists.")
        return item

    def delete_toolbox_command(self, command_id: str) -> None:
        self.connection.execute(
            "DELETE FROM toolbox_commands WHERE id = ?", (command_id,)
        )
        self.connection.commit()
        self._normalize_toolbox_positions()

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

    def _normalize_toolbox_positions(self) -> None:
        with self.connection:
            for position, item in enumerate(self.list_toolbox_commands()):
                self.connection.execute(
                    "UPDATE toolbox_commands SET position = ? WHERE id = ?",
                    (position, item.id),
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
    def _ssh_connection(row: sqlite3.Row) -> SshConnection:
        return SshConnection(
            project_id=row["project_id"],
            target=row["target"],
            port=row["port"],
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
    def _toolbox_command(row: sqlite3.Row) -> ToolboxCommand:
        return ToolboxCommand(
            id=row["id"],
            name=row["name"],
            command=row["command"],
            position=row["position"],
        )

    @staticmethod
    def _validate_toolbox_command(name: str, command: str) -> tuple[str, str]:
        name = name.strip()
        if not name:
            raise ValueError("Name is required.")
        if not command.strip():
            raise ValueError("Command is required.")
        if "\n" in command or "\r" in command:
            raise ValueError("Commands must fit on one line.")
        return name, command

    @staticmethod
    def _validate_ssh_connection(
        target: str, port: Optional[int]
    ) -> tuple[str, Optional[int]]:
        target = target.strip()
        if not target:
            raise ValueError("SSH target is required.")
        if target.startswith("-") or any(character.isspace() for character in target):
            raise ValueError("SSH target must be a host, user@host, or SSH config alias.")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535.")
        return target, port

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
