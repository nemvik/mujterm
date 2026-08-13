from __future__ import annotations

import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mujterm.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Database,
    DatabaseError,
)
from mujterm.models import AgentRace, SshConnection


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "state.db")

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def test_project_and_terminal_lifecycle(self) -> None:
        first = self.database.create_project("First", "/tmp/first")
        second = self.database.create_project("Second", "/tmp/second")
        terminal = self.database.create_terminal(first.id, "Terminal 1", "/tmp/first")
        self.database.move_terminal(terminal.id, second.id)
        moved = self.database.get_terminal(terminal.id)
        self.assertEqual(moved.project_id if moved else None, second.id)
        self.database.delete_project(second.id)
        ungrouped = self.database.get_terminal(terminal.id)
        self.assertIsNone(ungrouped.project_id if ungrouped else "missing")

    def test_new_database_uses_current_schema_without_a_backup(self) -> None:
        self.assertEqual(self.database.schema_version, SCHEMA_VERSION)
        self.assertEqual(self.database.integrity_status, "ok")
        self.assertIsNone(self.database.last_backup_path)

    def test_legacy_database_is_backed_up_and_migrated_without_data_loss(self) -> None:
        path = Path(self.temporary.name) / "legacy.db"
        legacy = sqlite3.connect(path)
        legacy.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                root_path TEXT NOT NULL,
                position INTEGER NOT NULL,
                collapsed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        legacy.execute(
            "INSERT INTO projects(id, name, root_path, position, collapsed) "
            "VALUES ('legacy-project', 'Legacy', '/tmp/legacy', 0, 0)"
        )
        legacy.commit()
        legacy.close()

        migrated = Database(path)
        try:
            self.assertEqual(migrated.schema_version, SCHEMA_VERSION)
            self.assertEqual(migrated.get_project("legacy-project").name, "Legacy")
            self.assertEqual(migrated.list_toolbox_commands(), [])
            self.assertIsNone(migrated.get_ssh_connection("legacy-project"))
            backup = migrated.last_backup_path
            self.assertIsNotNone(backup)
            self.assertTrue(backup.exists())
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        finally:
            migrated.close()

        backup_database = sqlite3.connect(backup)
        try:
            version = backup_database.execute("PRAGMA user_version").fetchone()[0]
            name = backup_database.execute(
                "SELECT name FROM projects WHERE id = 'legacy-project'"
            ).fetchone()[0]
        finally:
            backup_database.close()
        self.assertEqual(version, 0)
        self.assertEqual(name, "Legacy")

    def test_newer_database_schema_is_rejected_without_modification(self) -> None:
        path = Path(self.temporary.name) / "future.db"
        future = sqlite3.connect(path)
        future.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
        future.close()

        with self.assertRaisesRegex(DatabaseError, "newer MujTerm version"):
            Database(path)

        unchanged = sqlite3.connect(path)
        try:
            version = unchanged.execute("PRAGMA user_version").fetchone()[0]
        finally:
            unchanged.close()
        self.assertEqual(version, SCHEMA_VERSION + 5)
        self.assertFalse(list(path.parent.glob("future.db.backup-*")))

    def test_failed_migration_rolls_back_the_complete_transaction(self) -> None:
        path = Path(self.temporary.name) / "rollback.db"
        legacy = sqlite3.connect(path)
        legacy.execute("CREATE TABLE preserved(value TEXT NOT NULL)")
        legacy.execute("INSERT INTO preserved VALUES ('safe')")
        legacy.commit()
        legacy.close()
        broken_migration = (
            "CREATE TABLE must_be_rolled_back(value TEXT)",
            "THIS IS NOT VALID SQL",
        )

        with patch.dict(MIGRATIONS, {1: broken_migration}, clear=True):
            with self.assertRaisesRegex(DatabaseError, "Could not migrate"):
                Database(path)

        unchanged = sqlite3.connect(path)
        try:
            version = unchanged.execute("PRAGMA user_version").fetchone()[0]
            preserved = unchanged.execute("SELECT value FROM preserved").fetchone()[0]
            transient = unchanged.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'must_be_rolled_back'"
            ).fetchone()
        finally:
            unchanged.close()
        self.assertEqual(version, 0)
        self.assertEqual(preserved, "safe")
        self.assertIsNone(transient)

    def test_corrupt_database_is_rejected(self) -> None:
        path = Path(self.temporary.name) / "corrupt.db"
        path.write_bytes(b"not a sqlite database")

        with self.assertRaisesRegex(DatabaseError, "Could not open state database"):
            Database(path)

    def test_terminal_reordering(self) -> None:
        project = self.database.create_project("Project", "/tmp/project")
        first = self.database.create_terminal(project.id, "One", "/tmp/project")
        second = self.database.create_terminal(project.id, "Two", "/tmp/project")
        self.database.move_terminal(second.id, project.id, first.id)
        terminals = [item for item in self.database.list_terminals(project.id) if item.project_id == project.id]
        self.assertEqual([item.id for item in terminals], [second.id, first.id])

    def test_ssh_project_connection_lifecycle(self) -> None:
        project = self.database.create_ssh_project(
            "Production", "root@example.com", 2222, "/tmp"
        )
        self.assertIsNone(self.database.find_project_by_root("/tmp"))
        self.assertEqual(
            self.database.get_ssh_connection(project.id),
            SshConnection(project.id, "root@example.com", 2222),
        )

        updated = self.database.update_ssh_connection(
            project.id, "production-vps", None
        )
        self.assertEqual(updated, SshConnection(project.id, "production-vps"))

        terminal = self.database.create_terminal(project.id, "SSH 1", "/tmp")
        self.database.delete_project(project.id)
        self.assertIsNone(self.database.get_ssh_connection(project.id))
        ungrouped = self.database.get_terminal(terminal.id)
        self.assertIsNone(ungrouped.project_id if ungrouped else "missing")

    def test_ssh_project_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Project name"):
            self.database.create_ssh_project(" ", "server", None, "/tmp")
        with self.assertRaisesRegex(ValueError, "SSH target"):
            self.database.create_ssh_project("VPS", "-oProxyCommand=bad", None, "/tmp")
        with self.assertRaisesRegex(ValueError, "SSH target"):
            self.database.create_ssh_project("VPS", "bad host", None, "/tmp")
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            self.database.create_ssh_project("VPS", "server", 70000, "/tmp")

    def test_toolbox_command_lifecycle_and_persistence(self) -> None:
        first = self.database.create_toolbox_command("Dev server", "pnpm dev")
        second = self.database.create_toolbox_command(
            "Inspect process", "ps aux | rg '$USER'"
        )
        self.assertEqual(self.database.list_toolbox_commands(), [first, second])

        updated = self.database.update_toolbox_command(
            first.id, "Development server", "pnpm dev --host"
        )
        self.assertEqual(updated.name, "Development server")
        self.assertEqual(updated.command, "pnpm dev --host")

        path = self.database.path
        self.database.close()
        self.database = Database(path)
        self.assertEqual(
            [item.name for item in self.database.list_toolbox_commands()],
            ["Development server", "Inspect process"],
        )

        self.database.delete_toolbox_command(first.id)
        remaining = self.database.list_toolbox_commands()
        self.assertEqual([item.id for item in remaining], [second.id])
        self.assertEqual(remaining[0].position, 0)

    def test_toolbox_command_validation_and_unique_names(self) -> None:
        self.database.create_toolbox_command("Deploy", "./deploy.sh")

        with self.assertRaisesRegex(ValueError, "already exists"):
            self.database.create_toolbox_command("deploy", "./deploy-staging.sh")
        with self.assertRaisesRegex(ValueError, "Name is required"):
            self.database.create_toolbox_command("  ", "pwd")
        with self.assertRaisesRegex(ValueError, "Command is required"):
            self.database.create_toolbox_command("Working directory", "  ")
        with self.assertRaisesRegex(ValueError, "one line"):
            self.database.create_toolbox_command("Two commands", "pwd\nls")

    def test_timeline_events_are_persisted_and_filtered_by_project(self) -> None:
        first = self.database.create_project("First", "/tmp/first")
        second = self.database.create_project("Second", "/tmp/second")
        terminal = self.database.create_terminal(first.id, "API", "/tmp/first")
        self.database.append_timeline_event(
            first.id, terminal.id, terminal.name, "service", "Started :3000", time.time()
        )
        self.database.append_timeline_event(
            second.id, None, "Second", "project", "Renamed", time.time() + 1
        )
        events = self.database.list_timeline_events(first.id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].terminal_id, terminal.id)
        self.assertEqual(events[0].summary, "Started :3000")

    def test_agent_race_round_trip(self) -> None:
        project = self.database.create_project("Race", "/tmp/race")
        race = AgentRace(
            id="race-1", project_id=project.id, task="Improve parser",
            base_commit="abc123", codex_branch="mujterm/codex-parser",
            claude_branch="mujterm/claude-parser", codex_path="/tmp/codex",
            claude_path="/tmp/claude", codex_terminal_id="terminal-codex",
            claude_terminal_id="terminal-claude", created_at=time.time(),
        )
        self.database.create_agent_race(race)
        self.assertEqual(self.database.get_agent_race(race.id), race)
        self.assertEqual(self.database.list_agent_races(project.id), [race])


if __name__ == "__main__":
    unittest.main()
