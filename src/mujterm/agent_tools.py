from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .logging_config import record_runtime_error
from .models import AgentRace, Project
from .terminal_view import resource_text
from .tmux_backend import TmuxError
from .worktrees import WorktreeError, compare_worktree, create_race_worktrees


class AgentToolsMixin:
    def show_agent_races(self) -> None:
        project = self.database.get_project(self.active_project_id) if self.active_project_id else None
        if not project:
            self._error("No active project", "Select a project before starting an agent race.")
            return
        if self.database.get_ssh_connection(project.id):
            self._error(
                "SSH project",
                "Agent races require a local Git project and are not available for SSH projects.",
            )
            return
        dialog = Gtk.Dialog(title=f"Agent races — {project.name}", transient_for=self, modal=True)
        dialog.set_default_size(600, 420)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.add_button("New Codex ↔ Claude race", Gtk.ResponseType.ACCEPT)
        content = dialog.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        races = self.database.list_agent_races(project.id)
        content.pack_start(Gtk.Label(label="RECENT ISOLATED WORKTREES", xalign=0), False, False, 0)
        race_ids: dict[int, str] = {}
        for index, race in enumerate(races):
            response_id = 200 + index
            stamp = datetime.fromtimestamp(race.created_at).strftime("%d.%m. %H:%M")
            button = dialog.add_button(f"{stamp}  ·  {race.task[:56]}", response_id)
            button.set_tooltip_text("Open comparison dashboard")
            race_ids[response_id] = race.id
        if not races:
            content.pack_start(Gtk.Label(label="No races yet.", xalign=0), False, False, 12)
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            self._create_agent_race(project)
        elif response in race_ids:
            self.show_race_dashboard(race_ids[response])

    def _create_agent_race(self, project: Project) -> None:
        task = self._text_prompt("New agent race", "Task for both agents", "")
        if not task:
            return
        try:
            worktrees = create_race_worktrees(project.id, project.root_path, task)
        except WorktreeError as exc:
            record_runtime_error("Agent race worktree creation failed", exc)
            self._error("Could not start agent race", str(exc))
            return
        codex = self.create_terminal(project.id, worktrees.codex_path, activate=True)
        claude = self.create_terminal(project.id, worktrees.claude_path, activate=False)
        if not codex or not claude:
            self._error("Could not start agent race", "The worktrees exist, but terminal creation failed.")
            return
        self.database.rename_terminal(codex.id, "Codex · race")
        self.database.rename_terminal(claude.id, "Claude · race")
        self._split_with_terminal(claude, Gtk.Orientation.HORIZONTAL)
        race = AgentRace(
            id=worktrees.id,
            project_id=project.id,
            task=task,
            base_commit=worktrees.base_commit,
            codex_branch=worktrees.codex_branch,
            claude_branch=worktrees.claude_branch,
            codex_path=worktrees.codex_path,
            claude_path=worktrees.claude_path,
            codex_terminal_id=codex.id,
            claude_terminal_id=claude.id,
            created_at=worktrees.created_at,
        )
        self.database.create_agent_race(race)
        prompt = (
            f"Solve this task independently in the current isolated worktree: {task}. "
            "Inspect existing code, implement the solution, and run relevant verification."
        )
        errors: list[str] = []
        for terminal, executable in ((codex, "codex"), (claude, "claude")):
            try:
                self.backend.send_command(terminal.tmux_name, [executable, prompt])
            except TmuxError as exc:
                record_runtime_error(f"Starting {executable} race terminal failed", exc)
                errors.append(f"{executable}: {exc}")
        self.rebuild_sidebar()
        if errors:
            self._error("Some agents could not be started", "\n".join(errors))
        self.show_race_dashboard(race.id)

    def show_race_dashboard(self, race_id: str) -> None:
        race = self.database.get_agent_race(race_id)
        if not race:
            return
        dialog = Gtk.Dialog(title="Agent race comparison", transient_for=self, modal=True)
        dialog.set_default_size(820, 440)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.add_button("Focus Claude", 302)
        dialog.add_button("Focus Codex", 301)
        content = dialog.get_content_area()
        content.set_border_width(12)
        content.set_spacing(10)
        task = Gtk.Label(label=race.task, xalign=0)
        task.set_line_wrap(True)
        content.pack_start(task, False, False, 0)
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        columns.pack_start(self._race_side("CODEX", race.codex_branch, race.codex_path, race.base_commit, race.codex_terminal_id), True, True, 0)
        columns.pack_start(self._race_side("CLAUDE", race.claude_branch, race.claude_path, race.base_commit, race.claude_terminal_id), True, True, 0)
        content.pack_start(columns, True, True, 0)
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        if response == 301:
            self.select_terminal(race.codex_terminal_id)
        elif response == 302:
            self.select_terminal(race.claude_terminal_id)

    def _race_side(self, title: str, branch: str, path: str, base: str, terminal_id: str) -> Gtk.Widget:
        comparison = compare_worktree(path, base)
        snapshot = self.snapshots.get(terminal_id)
        frame = Gtk.Frame(label=title)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        box.set_border_width(14)
        branch_label = Gtk.Label(label=branch, xalign=0)
        branch_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        box.pack_start(branch_label, False, False, 0)
        box.pack_start(Gtk.Label(label=f"{comparison.files} files   +{comparison.additions} / -{comparison.deletions}", xalign=0), False, False, 0)
        box.pack_start(Gtk.Label(label=f"Working tree: {comparison.dirty_files} entries", xalign=0), False, False, 0)
        box.pack_start(Gtk.Label(label=comparison.summary, xalign=0), False, False, 0)
        if snapshot:
            box.pack_start(Gtk.Label(label=f"{snapshot.status.value.upper()}   {resource_text(snapshot.cpu_percent, snapshot.memory_bytes)}", xalign=0), False, False, 0)
        path_label = Gtk.Label(label=path, xalign=0)
        path_label.set_line_wrap(True)
        path_label.set_selectable(True)
        box.pack_end(path_label, False, False, 0)
        frame.add(box)
        return frame
