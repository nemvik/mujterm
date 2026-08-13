from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from .logging_config import record_runtime_error
from .models import AgentKind, AgentRace, Project, TerminalSession
from .terminal_view import resource_text
from .tmux_backend import TmuxError
from .worktrees import WorktreeError, compare_worktree, create_race_worktrees


class AgentToolsMixin:
    def show_handoff(self) -> None:
        terminal = (
            self.database.get_terminal(self.active_terminal_id)
            if self.active_terminal_id
            else None
        )
        if not terminal:
            self._error("No active terminal", "Select a terminal before creating a handoff.")
            return
        card = self._handoff_card(terminal)
        dialog = Gtk.Dialog(title="Agent handoff", transient_for=self, modal=True)
        dialog.set_default_size(700, 520)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.add_button("Copy", Gtk.ResponseType.APPLY)
        dialog.add_button("Start Claude", 102)
        dialog.add_button("Start Codex", 101)
        content = dialog.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        note = Gtk.Label(
            label="Portable context only — terminal output and prompt history are not included.",
            xalign=0,
        )
        note.set_line_wrap(True)
        content.pack_start(note, False, False, 0)
        scrolled = Gtk.ScrolledWindow()
        text_view = Gtk.TextView()
        text_view.set_editable(False)
        text_view.set_monospace(True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.get_buffer().set_text(card)
        scrolled.add(text_view)
        content.pack_start(scrolled, True, True, 0)
        dialog.show_all()
        while True:
            response = dialog.run()
            if response == Gtk.ResponseType.APPLY:
                clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                clipboard.set_text(card, -1)
                clipboard.store()
                continue
            if response in (101, 102):
                agent = AgentKind.CODEX if response == 101 else AgentKind.CLAUDE
                dialog.destroy()
                self._start_handoff_agent(terminal, agent, card)
                return
            break
        dialog.destroy()

    def _handoff_card(self, terminal: TerminalSession) -> str:
        snapshot = self.snapshots.get(terminal.id)
        cwd = snapshot.cwd if snapshot else self._terminal_cwd(terminal)
        project = self.database.get_project(terminal.project_id) if terminal.project_id else None
        comparison = compare_worktree(cwd, "HEAD")
        services = ", ".join(f":{item.port}" for item in (snapshot.services if snapshot else ())) or "none"
        events = [
            event for event in self.database.list_timeline_events(terminal.project_id, 30)
            if event.terminal_id == terminal.id
        ][:6]
        timeline = "\n".join(
            f"- {datetime.fromtimestamp(event.created_at).strftime('%H:%M')} {event.summary}"
            for event in reversed(events)
        ) or "- no recent events"
        branch = snapshot.branch if snapshot and snapshot.branch else "not a Git worktree"
        status = snapshot.status.value if snapshot else "unknown"
        return (
            "# MujTerm agent handoff\n\n"
            f"Project: {project.name if project else 'Ungrouped'}\n"
            f"Working directory: {cwd}\n"
            f"Branch: {branch}\n"
            f"Current state: {status}\n"
            f"Changes: {comparison.files} files, +{comparison.additions}/-{comparison.deletions} "
            f"({comparison.dirty_files} working-tree entries)\n"
            f"Services: {services}\n"
            "Tests: not recorded — inspect and run the relevant suite\n\n"
            f"Recent activity:\n{timeline}\n\n"
            "Continue from this context. Inspect the repository before changing files, "
            "preserve existing work, and verify your result."
        )

    def _start_handoff_agent(
        self, source: TerminalSession, agent: AgentKind, card: str
    ) -> None:
        if source.project_id and self.database.get_ssh_connection(source.project_id):
            self._error(
                "SSH project",
                "Automatic agent startup is disabled for SSH projects. "
                "Copy the handoff card and paste it after connecting instead.",
            )
            return
        terminal = self.create_terminal(
            source.project_id, self._terminal_cwd(source), activate=True
        )
        if not terminal:
            return
        self.database.rename_terminal(terminal.id, f"{agent.value.title()} handoff")
        try:
            self.backend.send_command(terminal.tmux_name, [agent.value, card])
        except TmuxError as exc:
            record_runtime_error(f"Starting {agent.value} handoff failed", exc)
            self._error(f"Could not start {agent.value}", str(exc))
        self._record_event(terminal, "handoff", f"Started {agent.value} from {source.name}")
        self.rebuild_sidebar()

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
        self._record_event(codex, "race", f"Started Codex ↔ Claude: {task}")
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

    def show_service_map(self) -> None:
        terminals = {item.id: item for item in self.database.list_terminals()}
        listeners: dict[int, list[str]] = {}
        for terminal_id, snapshot in self.snapshots.items():
            for service in snapshot.services:
                listeners.setdefault(service.port, []).append(terminal_id)
        edges: list[tuple[str, str, int]] = []
        for source_id, snapshot in self.snapshots.items():
            for port in snapshot.connected_ports:
                for target_id in listeners.get(port, ()):
                    if source_id != target_id:
                        edges.append((source_id, target_id, port))
        dialog = Gtk.Dialog(title="Service dependency map", transient_for=self, modal=True)
        dialog.set_default_size(720, 500)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        content = dialog.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        explanation = Gtk.Label(label="LIVE PROCESS CONNECTIONS  ·  source → listening service", xalign=0)
        content.pack_start(explanation, False, False, 0)
        scrolled = Gtk.ScrolledWindow()
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        targets: dict[int, str] = {}
        if edges:
            for index, (source_id, target_id, port) in enumerate(sorted(set(edges), key=lambda item: (item[2], item[0]))):
                source = terminals.get(source_id)
                target = terminals.get(target_id)
                label = f"{source.name if source else source_id[:8]}   →   {target.name if target else target_id[:8]}   :{port}"
                button = Gtk.Button(label=label)
                button.set_tooltip_text("Focus the service terminal")
                response_id = 500 + index
                targets[response_id] = target_id
                button.connect("clicked", lambda _button, value=response_id: dialog.response(value))
                rows.pack_start(button, False, False, 0)
        else:
            rows.pack_start(Gtk.Label(label="No cross-terminal TCP dependencies detected right now.", xalign=0), False, False, 8)
        for port, terminal_ids in sorted(listeners.items()):
            for terminal_id in terminal_ids:
                terminal = terminals.get(terminal_id)
                rows.pack_start(Gtk.Label(label=f"● {terminal.name if terminal else terminal_id[:8]} listens on :{port}", xalign=0), False, False, 0)
        scrolled.add(rows)
        content.pack_start(scrolled, True, True, 0)
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        if response in targets:
            self.select_terminal(targets[response])

