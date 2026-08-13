from __future__ import annotations

import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .diagnostics import diagnostic_report
from .integrations import IntegrationError
from .logging_config import record_runtime_error
from .metadata import git_info
from .models import (
    ListeningService,
    Project,
    SshConnection,
    ToolboxCommand,
)
from .tmux_backend import TmuxError


class WindowDialogsMixin:
    def _show_toolbox_editor(
        self, item: Optional[ToolboxCommand] = None
    ) -> None:
        self.toolbox_popover.popdown()
        title = "Edit toolbox command" if item else "Add toolbox command"
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.ACCEPT
        )
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        content = dialog.get_content_area()
        content.set_spacing(7)
        content.set_border_width(12)

        content.add(Gtk.Label(label="Name", xalign=0))
        name_entry = Gtk.Entry(text=item.name if item else "")
        name_entry.set_max_length(80)
        name_entry.set_placeholder_text("Start development server")
        name_entry.set_activates_default(True)
        content.add(name_entry)

        content.add(Gtk.Label(label="Command", xalign=0))
        command_entry = Gtk.Entry(text=item.command if item else "")
        command_entry.set_width_chars(48)
        command_entry.set_placeholder_text("pnpm dev")
        command_entry.set_activates_default(True)
        content.add(command_entry)

        error = Gtk.Label(xalign=0)
        error.set_line_wrap(True)
        error.get_style_context().add_class("toolbox-error")
        content.add(error)

        dialog.show_all()
        error.hide()
        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.ACCEPT:
                break
            try:
                if item:
                    self.database.update_toolbox_command(
                        item.id, name_entry.get_text(), command_entry.get_text()
                    )
                else:
                    self.database.create_toolbox_command(
                        name_entry.get_text(), command_entry.get_text()
                    )
                break
            except ValueError as exc:
                error.set_text(str(exc))
                error.show()
                name_entry.grab_focus()

        dialog.destroy()
        GLib.idle_add(self._reopen_toolbox)

    def _delete_toolbox_command(self, item: ToolboxCommand) -> None:
        self.toolbox_popover.popdown()
        if self._confirm(
            "Delete toolbox command?",
            f'"{item.name}" will be removed from the command toolbox.',
        ):
            self.database.delete_toolbox_command(item.id)
        GLib.idle_add(self._reopen_toolbox)

    def _reopen_toolbox(self) -> bool:
        self._rebuild_toolbox()
        self.toolbox_popover.popup()
        return False


    def open_project_dialog(self) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Open Project",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.ACCEPT)
        dialog.set_current_folder(str(Path.home()))
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            selected = dialog.get_filename()
            if selected:
                info = git_info(selected)
                root = info.root or str(Path(selected).resolve())
                project = self.database.find_project_by_root(root)
                if not project:
                    project = self.database.create_project(Path(root).name or root, root)
                self.active_project_id = project.id
                if not [t for t in self.database.list_terminals(project.id) if t.project_id == project.id]:
                    self.create_terminal(project.id)
                else:
                    self.rebuild_sidebar()
        dialog.destroy()

    def open_ssh_project_dialog(self, project_id: Optional[str] = None) -> None:
        project = self.database.get_project(project_id) if project_id else None
        connection = (
            self.database.get_ssh_connection(project_id) if project_id else None
        )
        if project_id and (not project or not connection):
            return

        title = f"Edit SSH Connection — {project.name}" if project else "New SSH Project"
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_buttons(
            "Cancel",
            Gtk.ResponseType.CANCEL,
            "Save" if project else "Connect",
            Gtk.ResponseType.ACCEPT,
        )
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        content = dialog.get_content_area()
        content.set_spacing(7)
        content.set_border_width(12)

        name_entry: Optional[Gtk.Entry] = None
        if not project:
            content.add(Gtk.Label(label="Project name", xalign=0))
            name_entry = Gtk.Entry()
            name_entry.set_placeholder_text("Production VPS")
            name_entry.set_max_length(80)
            name_entry.set_activates_default(True)
            content.add(name_entry)

        content.add(Gtk.Label(label="SSH target", xalign=0))
        target_entry = Gtk.Entry(text=connection.target if connection else "")
        target_entry.set_placeholder_text("root@example.com or my-vps")
        target_entry.set_activates_default(True)
        content.add(target_entry)

        content.add(Gtk.Label(label="Port (optional)", xalign=0))
        port_entry = Gtk.Entry(
            text=str(connection.port) if connection and connection.port else ""
        )
        port_entry.set_placeholder_text("Default from SSH config")
        port_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        port_entry.set_activates_default(True)
        content.add(port_entry)

        note = Gtk.Label(
            label=(
                "Authentication uses OpenSSH, ~/.ssh/config and ssh-agent. "
                "Passwords and private keys are never stored by MujTerm."
            ),
            xalign=0,
        )
        note.set_line_wrap(True)
        note.set_max_width_chars(54)
        note.get_style_context().add_class("toolbox-command")
        content.add(note)

        error = Gtk.Label(xalign=0)
        error.set_line_wrap(True)
        error.get_style_context().add_class("toolbox-error")
        content.add(error)

        saved_project: Optional[Project] = None
        dialog.show_all()
        error.hide()
        if name_entry:
            name_entry.grab_focus()
        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.ACCEPT:
                break
            try:
                port = self._parse_ssh_port(port_entry.get_text())
                if project:
                    self.database.update_ssh_connection(
                        project.id, target_entry.get_text(), port
                    )
                    saved_project = project
                else:
                    saved_project = self.database.create_ssh_project(
                        name_entry.get_text() if name_entry else "",
                        target_entry.get_text(),
                        port,
                        str(Path.home()),
                    )
                break
            except ValueError as exc:
                error.set_text(str(exc))
                error.show()

        dialog.destroy()
        if not saved_project:
            return
        if project:
            self.rebuild_sidebar()
            return

        self.active_project_id = saved_project.id
        if not self.create_terminal(saved_project.id):
            self.database.delete_project(saved_project.id)
            self.active_project_id = None
            self.rebuild_sidebar()

    @staticmethod
    def _parse_ssh_port(value: str) -> Optional[int]:
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError("SSH port must be a number between 1 and 65535.") from exc

    @staticmethod
    def _ssh_connection_label(connection: SshConnection) -> str:
        if connection.port:
            return f"{connection.target}:{connection.port}"
        return connection.target

    @staticmethod
    def _ssh_arguments(connection: SshConnection) -> list[str]:
        arguments = ["ssh"]
        if connection.port:
            arguments.extend(("-p", str(connection.port)))
        arguments.append(connection.target)
        return arguments


    def open_uri(self, uri: str) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            record_runtime_error("Opening external link failed", exc)
            self._error("Could not open link", str(exc))

    def open_service(self, port: int) -> None:
        self.open_uri(f"http://localhost:{port}")

    def service_button_press(self, service: ListeningService, event: Gdk.EventButton) -> bool:
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label=f"Open localhost:{service.port}")
        open_item.connect("activate", lambda *_args: self.open_service(service.port))
        copy_item = Gtk.MenuItem(label="Copy URL")
        copy_item.connect("activate", lambda *_args: self._copy_service_url(service.port))
        stop_item = Gtk.MenuItem(label=f"Stop service (PID {service.pid})")
        stop_item.connect("activate", lambda *_args: self._stop_service(service))
        menu.append(open_item)
        menu.append(copy_item)
        menu.append(stop_item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    @staticmethod
    def _copy_service_url(port: int) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(f"http://localhost:{port}", -1)
        clipboard.store()

    def _stop_service(self, service: ListeningService) -> None:
        if not self._confirm(
            f"Stop service on port {service.port}?",
            f"Process {service.pid} will receive SIGTERM.",
        ):
            return
        try:
            os.kill(service.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            record_runtime_error("Stopping local service failed", exc)
            self._error("Could not stop service", str(exc))

    def show_timeline(self) -> None:
        project = self.database.get_project(self.active_project_id) if self.active_project_id else None
        events = self.database.list_timeline_events(project.id if project else None)
        title = f"Timeline — {project.name}" if project else "Timeline — All Projects"
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.set_default_size(680, 520)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        content = dialog.get_content_area()
        content.set_border_width(12)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        timeline = Gtk.ListBox()
        timeline.set_selection_mode(Gtk.SelectionMode.NONE)
        row_terminals: dict[Gtk.ListBoxRow, Optional[str]] = {}
        for event in events:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_border_width(8)
            kind = Gtk.Label(label=event.kind.upper(), xalign=0)
            kind.get_style_context().add_class("timeline-kind")
            summary = Gtk.Label(label=event.summary, xalign=0)
            summary.set_line_wrap(True)
            summary.get_style_context().add_class("timeline-summary")
            stamp = datetime.fromtimestamp(event.created_at).strftime("%d.%m. %H:%M:%S")
            meta = Gtk.Label(label=f"{stamp}  ·  {event.terminal_name}", xalign=0)
            meta.get_style_context().add_class("timeline-meta")
            box.pack_start(kind, False, False, 0)
            box.pack_start(summary, False, False, 0)
            box.pack_start(meta, False, False, 0)
            row.add(box)
            row_terminals[row] = event.terminal_id
            timeline.add(row)
        if not events:
            timeline.add(Gtk.Label(label="No recorded events yet.", margin=24))
        timeline.connect(
            "row-activated",
            lambda _list, row: self._timeline_row_activated(dialog, row_terminals.get(row)),
        )
        scrolled.add(timeline)
        content.pack_start(scrolled, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _timeline_row_activated(
        self, dialog: Gtk.Dialog, terminal_id: Optional[str]
    ) -> None:
        if terminal_id and self.database.get_terminal(terminal_id):
            dialog.response(Gtk.ResponseType.CLOSE)
            self.select_terminal(terminal_id)

    def show_terminal_menu(self, terminal_id: str, event: Gdk.EventButton) -> None:
        menu = Gtk.Menu()
        duplicate = Gtk.MenuItem(label="Duplicate from Current Directory")
        duplicate.connect("activate", lambda *_args: self.duplicate_terminal(terminal_id))
        rename = Gtk.MenuItem(label="Rename")
        rename.connect("activate", lambda *_args: self._rename_terminal(terminal_id))
        terminal = self.database.get_terminal(terminal_id)
        if terminal and not self.backend.has_session(terminal.tmux_name):
            restart = Gtk.MenuItem(label="Restart Terminal")
            restart.connect("activate", lambda *_args: self.restart_terminal(terminal_id))
            menu.append(restart)
        close = Gtk.MenuItem(label="Close Terminal")
        close.connect("activate", lambda *_args: self.close_terminal(terminal_id))
        menu.append(duplicate)
        menu.append(rename)
        menu.append(close)
        menu.show_all()
        menu.popup_at_pointer(event)

    def restart_terminal(self, terminal_id: str) -> None:
        terminal = self.database.get_terminal(terminal_id)
        if not terminal:
            return
        try:
            self.backend.create_session(terminal, self._recovery_cwd(terminal))
            self._start_project_connection(terminal)
        except TmuxError as exc:
            record_runtime_error("Terminal restart failed", exc)
            self.backend.kill_session(terminal.tmux_name)
            self._error("Could not restart terminal", str(exc))
            return
        old_view = self.terminal_views.pop(terminal_id, None)
        host = self.pane_hosts.get(terminal_id)
        if old_view:
            if host and old_view.get_parent() is host:
                host.remove(old_view)
            old_view.destroy()
        self.snapshots.pop(terminal_id, None)
        terminal = self.database.get_terminal(terminal_id)
        if terminal and host:
            view = self._ensure_terminal_view(terminal)
            host.pack_start(view, True, True, 0)
            host.show_all()
            view.terminal.grab_focus()
        else:
            self.select_terminal(terminal_id)

    def show_project_menu(self, project_id: str, event: Gdk.EventButton) -> None:
        menu = Gtk.Menu()
        if self.database.get_ssh_connection(project_id):
            edit_ssh = Gtk.MenuItem(label="Edit SSH Connection")
            edit_ssh.connect(
                "activate", lambda *_args: self.open_ssh_project_dialog(project_id)
            )
            menu.append(edit_ssh)
        rename = Gtk.MenuItem(label="Rename Project")
        rename.connect("activate", lambda *_args: self._rename_project(project_id))
        remove = Gtk.MenuItem(label="Remove Project")
        remove.connect("activate", lambda *_args: self._remove_project(project_id))
        menu.append(rename)
        menu.append(remove)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _rename_terminal(self, terminal_id: str) -> None:
        terminal = self.database.get_terminal(terminal_id)
        if not terminal:
            return
        value = self._text_prompt("Rename Terminal", "Name", terminal.name)
        if value:
            self.database.rename_terminal(terminal_id, value)
            self._record_event(terminal, "terminal", f"Renamed to {value}")
            self.rebuild_sidebar()

    def _rename_project(self, project_id: str) -> None:
        project = self.database.get_project(project_id)
        if not project:
            return
        value = self._text_prompt("Rename Project", "Name", project.name)
        if value:
            self.database.rename_project(project_id, value)
            self.database.append_timeline_event(
                project.id,
                None,
                value,
                "project",
                f"Renamed project from {project.name} to {value}",
                time.time(),
            )
            self.rebuild_sidebar()

    def _remove_project(self, project_id: str) -> None:
        if self._confirm("Remove project?", "Its terminals will remain available under Ungrouped."):
            self.database.delete_project(project_id)
            if self.active_project_id == project_id:
                self.active_project_id = None
            self.rebuild_sidebar()

    def show_integration_dialog(self) -> None:
        status = self.integrations.status()
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.NONE,
            text="Agent status integrations",
        )
        state = f"Codex: {'enabled' if status.codex else 'disabled'}\nClaude Code: {'enabled' if status.claude else 'disabled'}"
        dialog.format_secondary_text(
            state
            + "\n\nThe hooks only report lifecycle state for agents launched inside MujTerm. "
            "They never approve actions or capture prompts. Codex will ask you to review the hook once via /hooks."
        )
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.add_button("Disable", Gtk.ResponseType.REJECT)
        dialog.add_button("Enable", Gtk.ResponseType.ACCEPT)
        response = dialog.run()
        dialog.destroy()
        try:
            if response == Gtk.ResponseType.ACCEPT:
                self.integrations.install()
                self._set_banner_visible(False)
            elif response == Gtk.ResponseType.REJECT:
                self.integrations.uninstall()
                self._show_integration_banner_if_needed()
        except IntegrationError as exc:
            record_runtime_error("Agent integration update failed", exc)
            self._error("Could not update agent configuration", str(exc))

    def show_diagnostics(self) -> None:
        report = diagnostic_report(self.backend, database=self.database)
        dialog = Gtk.Dialog(
            title="About / Diagnostics", transient_for=self, modal=True
        )
        dialog.set_default_size(680, 520)
        dialog.add_button("Copy diagnostics", Gtk.ResponseType.APPLY)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)

        content = dialog.get_content_area()
        content.set_border_width(14)
        content.set_spacing(10)

        heading = Gtk.Label(xalign=0)
        heading.set_markup("<big><b>MujTerm</b></big>  ·  persistent terminal workspace")
        content.pack_start(heading, False, False, 0)

        explanation = Gtk.Label(
            label=(
                "Runtime identity and terminal input status. Copy this report when "
                "diagnosing an installation or compatibility problem."
            ),
            xalign=0,
        )
        explanation.set_line_wrap(True)
        content.pack_start(explanation, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        report_view = Gtk.TextView()
        report_view.set_editable(False)
        report_view.set_cursor_visible(False)
        report_view.set_monospace(True)
        report_view.set_wrap_mode(Gtk.WrapMode.NONE)
        report_view.set_left_margin(10)
        report_view.set_right_margin(10)
        report_view.set_top_margin(10)
        report_view.set_bottom_margin(10)
        report_view.get_buffer().set_text(report)
        scrolled.add(report_view)
        content.pack_start(scrolled, True, True, 0)

        dialog.show_all()
        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.APPLY:
                break
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(report, -1)
            clipboard.store()
            button = dialog.get_widget_for_response(Gtk.ResponseType.APPLY)
            if isinstance(button, Gtk.Button):
                button.set_label("Copied")
        dialog.destroy()


    def _text_prompt(self, title: str, label: str, value: str) -> Optional[str]:
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.ACCEPT)
        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)
        content.add(Gtk.Label(label=label, xalign=0))
        entry = Gtk.Entry(text=value)
        entry.set_activates_default(True)
        content.add(entry)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        dialog.show_all()
        response = dialog.run()
        result = entry.get_text().strip() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        return result

    def _confirm(self, title: str, message: str) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CANCEL,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.add_button("Continue", Gtk.ResponseType.ACCEPT)
        result = dialog.run() == Gtk.ResponseType.ACCEPT
        dialog.destroy()
        return result

    def _error(self, title: str, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
