from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from mujterm.database import Database
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
