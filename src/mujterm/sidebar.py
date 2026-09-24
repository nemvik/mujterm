from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from .models import (
    AgentStatus,
    ListeningService,
    Project,
    TerminalSession,
    TerminalSnapshot,
)
from .terminal_view import display_path, resource_text

if TYPE_CHECKING:
    from .ui import MainWindow


PROJECT_TARGET = Gtk.TargetEntry.new(
    "application/x-mujterm-project", Gtk.TargetFlags.SAME_APP, 1
)
TERMINAL_TARGET = Gtk.TargetEntry.new(
    "application/x-mujterm-terminal", Gtk.TargetFlags.SAME_APP, 2
)


class TerminalRow(Gtk.ListBoxRow):
    def __init__(self, window: "MainWindow", session: TerminalSession) -> None:
        super().__init__()
        self.window = window
        self.session = session
        self._last_active: Optional[bool] = None
        self._last_metadata: Optional[str] = None
        self._last_resources: Optional[str] = None
        self._last_services: Optional[tuple[ListeningService, ...]] = None
        self._last_status: Optional[tuple[AgentStatus, Optional[str]]] = None
        self.get_style_context().add_class("mujterm-terminal-row")
        layout = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.click_target = click_target = Gtk.EventBox()
        click_target.set_visible_window(False)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.title = Gtk.Label(label=session.name, xalign=0)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.get_style_context().add_class("terminal-row-title")
        self.metadata = Gtk.Label(label=display_path(session.last_cwd), xalign=0)
        self.metadata.set_ellipsize(Pango.EllipsizeMode.END)
        self.metadata.get_style_context().add_class("mujterm-path")
        self.ports_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        text.pack_start(self.title, False, False, 0)
        text.pack_start(self.metadata, False, False, 0)
        text.pack_start(self.ports_box, False, False, 2)
        content.pack_start(text, True, True, 0)
        self.status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        self.agent_label = Gtk.Label()
        self.indicator = Gtk.Label(label="●")
        self.status_box.pack_start(self.spinner, False, False, 0)
        self.status_box.pack_start(self.agent_label, False, False, 0)
        self.status_box.pack_start(self.indicator, False, False, 0)
        content.pack_end(self.status_box, False, False, 0)
        click_target.add(content)
        click_target.connect("button-release-event", self._button_release)
        click_target.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [TERMINAL_TARGET], Gdk.DragAction.MOVE)
        click_target.connect("drag-data-get", self._drag_data_get)
        layout.pack_start(click_target, True, True, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_valign(Gtk.Align.CENTER)
        close.get_style_context().add_class("terminal-row-action")
        close.set_tooltip_text("Close terminal")
        close.connect("clicked", lambda *_args: self.window.close_terminal(self.session.id))
        layout.pack_end(close, False, False, 0)
        self.add(layout)
        self.drag_dest_set(Gtk.DestDefaults.ALL, [TERMINAL_TARGET], Gdk.DragAction.MOVE)
        self.connect("drag-data-received", self._drag_data_received)
        self.connect("map", self._sync_status_animation)
        self.connect("unmap", self._sync_status_animation)
        self.update(None, False)

    def update(self, snapshot: Optional[TerminalSnapshot], active: bool) -> None:
        if active != self._last_active:
            self._last_active = active
            context = self.get_style_context()
            if active:
                context.add_class("active")
            else:
                context.remove_class("active")
            self._sync_status_animation()
        if not snapshot:
            metadata = display_path(self.session.last_cwd)
            if metadata != self._last_metadata:
                self._last_metadata = metadata
                self.metadata.set_text(metadata)
            self._update_services(())
            self._set_status(AgentStatus.SHELL, None)
            return
        metadata = display_path(snapshot.cwd)
        if snapshot.branch:
            metadata += f" · {snapshot.branch}"
        if metadata != self._last_metadata:
            self._last_metadata = metadata
            self.metadata.set_text(metadata)
        resources = resource_text(snapshot.cpu_percent, snapshot.memory_bytes)
        tooltip = f"{metadata}\n{resources}"
        if tooltip != self._last_resources:
            self._last_resources = tooltip
            self.click_target.set_tooltip_text(tooltip)
        self._update_services(snapshot.services)
        self._set_status(snapshot.status, snapshot.agent.value.title() if snapshot.agent else None)

    def _update_services(self, services: tuple[ListeningService, ...]) -> None:
        if services == self._last_services:
            return
        self._last_services = services
        for child in self.ports_box.get_children():
            self.ports_box.remove(child)
        for service in services[:4]:
            button = Gtk.Button(label=f":{service.port}")
            button.get_style_context().add_class("port-chip")
            button.set_tooltip_text(f"Open http://localhost:{service.port}")
            button.connect(
                "clicked",
                lambda _button, port=service.port: self.window.open_service(port),
            )
            button.connect(
                "button-press-event",
                lambda _button, event, item=service: self.window.service_button_press(item, event),
            )
            self.ports_box.pack_start(button, False, False, 0)
        self.ports_box.show_all()

    def _set_status(self, status: AgentStatus, agent: Optional[str]) -> None:
        state = (status, agent)
        if state == self._last_status:
            return
        self._last_status = state
        context = self.status_box.get_style_context()
        for class_name in ("status-working", "status-action", "status-ready", "status-error", "status-shell"):
            context.remove_class(class_name)
        self.spinner.stop()
        self.spinner.hide()
        self.indicator.show()
        self.agent_label.set_text(agent or "")
        # The status box starts hidden, so show_all() never reached this label.
        self.agent_label.set_visible(bool(agent))
        if status == AgentStatus.SHELL and not agent:
            self.status_box.set_no_show_all(True)
            self.status_box.hide()
            self._sync_status_animation()
            return
        self.status_box.set_no_show_all(False)
        self.status_box.show()
        if status == AgentStatus.WORKING:
            context.add_class("status-working")
            self.indicator.set_text("◌")
            tooltip = f"{agent or 'Agent'} is working"
        elif status == AgentStatus.NEEDS_ACTION:
            context.add_class("status-action")
            self.indicator.set_text("!")
            tooltip = f"{agent or 'Agent'} needs your input"
        elif status == AgentStatus.READY:
            context.add_class("status-ready")
            self.indicator.set_text("●")
            tooltip = f"{agent or 'Agent'} is ready"
        elif status in (AgentStatus.ERROR, AgentStatus.ENDED):
            context.add_class("status-error")
            self.indicator.set_text("×")
            tooltip = "Terminal ended" if status == AgentStatus.ENDED else f"{agent or 'Agent'} stopped with an error"
        elif status == AgentStatus.UNKNOWN:
            context.add_class("status-working")
            self.indicator.set_text("◌")
            tooltip = f"{agent or 'Agent'} detected; waiting for hook events"
        else:
            context.add_class("status-shell")
            self.indicator.set_text("●")
            tooltip = "Shell"
        self.status_box.set_tooltip_text(tooltip)
        self._sync_status_animation()

    def _sync_status_animation(self, *_args: object) -> None:
        working = bool(
            self._last_status and self._last_status[0] == AgentStatus.WORKING
        )
        # Keep at most one animated indicator in the whole sidebar. Background
        # agents remain clearly marked by the static working ring.
        if working and self._last_active and self.get_mapped():
            self.indicator.hide()
            self.spinner.show()
            self.spinner.start()
            return
        self.spinner.stop()
        self.spinner.hide()
        self.indicator.show()

    def _button_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button == 3:
            self.window.show_terminal_menu(self.session.id, event)
            return True
        if event.button == 1:
            self.window.select_terminal(self.session.id)
            return True
        return False

    def _drag_data_get(self, _widget: Gtk.Widget, _context: Gdk.DragContext, data: Gtk.SelectionData, _info: int, _time: int) -> None:
        data.set_text(f"terminal:{self.session.id}", -1)

    def _drag_data_received(self, _widget: Gtk.Widget, context: Gdk.DragContext, _x: int, _y: int, data: Gtk.SelectionData, _info: int, time_value: int) -> None:
        payload = data.get_text() or ""
        if payload.startswith("terminal:"):
            source_id = payload.split(":", 1)[1]
            if source_id != self.session.id:
                self.window.move_terminal(source_id, self.session.project_id, self.session.id)
            Gtk.drag_finish(context, True, True, time_value)


class ProjectSection(Gtk.Box):
    def __init__(self, window: "MainWindow", project: Optional[Project], terminals: list[TerminalSession]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window = window
        self.project = project
        self.project_id = project.id if project else None
        self._summary_text: Optional[str] = None
        self.header = Gtk.EventBox()
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        header_box.get_style_context().add_class("mujterm-project-header")
        self.chevron = Gtk.Label(label="▾")
        self.chevron.get_style_context().add_class("project-chevron")
        title = Gtk.Label(label=project.name if project else "Ungrouped", xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("mujterm-project-title")
        ssh_connection = (
            window.database.get_ssh_connection(project.id) if project else None
        )
        ssh_badge: Optional[Gtk.Label] = None
        if ssh_connection:
            ssh_badge = Gtk.Label(label="SSH")
            ssh_badge.get_style_context().add_class("project-ssh")
            ssh_badge.set_tooltip_text(
                window._ssh_connection_label(ssh_connection)
            )
        count = Gtk.Label(label=str(len(terminals)))
        count.get_style_context().add_class("project-count")
        self.alert = Gtk.Label()
        self.alert.get_style_context().add_class("project-alert")
        add_button = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.MENU)
        add_button.set_relief(Gtk.ReliefStyle.NONE)
        add_button.get_style_context().add_class("sidebar-action")
        add_button.set_tooltip_text("New terminal in this project")
        add_button.connect("clicked", lambda *_args: self.window.create_terminal(self.project_id))
        header_box.pack_start(self.chevron, False, False, 0)
        header_box.pack_start(title, True, True, 0)
        if ssh_badge:
            header_box.pack_start(ssh_badge, False, False, 0)
        header_box.pack_start(self.alert, False, False, 0)
        header_box.pack_start(count, False, False, 0)
        header_box.pack_start(add_button, False, False, 0)
        self.header.add(header_box)
        self.header.connect("button-press-event", self._header_click)
        self.pack_start(self.header, False, False, 0)
        self.rows = Gtk.ListBox()
        self.rows.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.rows.set_activate_on_single_click(True)
        self.rows.connect("row-activated", self._row_activated)
        for terminal in terminals:
            row = TerminalRow(window, terminal)
            window.terminal_rows[terminal.id] = row
            self.rows.add(row)
        self.pack_start(self.rows, False, False, 0)
        collapsed = project.collapsed if project else False
        self.rows.set_no_show_all(collapsed)
        self.rows.set_visible(not collapsed)
        self.chevron.set_text("▸" if collapsed else "▾")
        self.drag_dest_set(Gtk.DestDefaults.ALL, [PROJECT_TARGET, TERMINAL_TARGET], Gdk.DragAction.MOVE)
        self.connect("drag-data-received", self._drag_data_received)
        if project:
            self.header.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [PROJECT_TARGET], Gdk.DragAction.MOVE)
            self.header.connect("drag-data-get", self._project_drag_data_get)

    def _row_activated(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if isinstance(row, TerminalRow):
            self.window.select_terminal(row.session.id)

    def update_summary(self, snapshots: dict[str, TerminalSnapshot]) -> None:
        terminal_ids = [
            child.session.id
            for child in self.rows.get_children()
            if isinstance(child, TerminalRow)
        ]
        needs_action = sum(
            1
            for terminal_id in terminal_ids
            if snapshots.get(terminal_id)
            and self.window._status_overrides.get(
                terminal_id, snapshots[terminal_id].status
            ) == AgentStatus.NEEDS_ACTION
        )
        working = sum(
            1
            for terminal_id in terminal_ids
            if snapshots.get(terminal_id)
            and self.window._status_overrides.get(
                terminal_id, snapshots[terminal_id].status
            ) == AgentStatus.WORKING
        )
        if needs_action:
            summary = f"! {needs_action}"
        elif working:
            summary = f"◉ {working}"
        else:
            summary = ""
        if summary != self._summary_text:
            self._summary_text = summary
            self.alert.set_text(summary)

    def _header_click(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button == 3 and self.project:
            self.window.show_project_menu(self.project.id, event)
            return True
        if event.button == 1:
            visible = self.rows.get_visible()
            self.rows.set_no_show_all(visible)
            self.rows.set_visible(not visible)
            self.chevron.set_text("▾" if not visible else "▸")
            if self.project:
                self.window.database.set_project_collapsed(self.project.id, visible)
            self.window.active_project_id = self.project_id
            return True
        return False

    def _project_drag_data_get(self, _widget: Gtk.Widget, _context: Gdk.DragContext, data: Gtk.SelectionData, _info: int, _time: int) -> None:
        if self.project:
            data.set_text(f"project:{self.project.id}", -1)

    def _drag_data_received(self, _widget: Gtk.Widget, context: Gdk.DragContext, _x: int, _y: int, data: Gtk.SelectionData, _info: int, time_value: int) -> None:
        payload = data.get_text() or ""
        success = False
        if payload.startswith("terminal:"):
            self.window.move_terminal(payload.split(":", 1)[1], self.project_id)
            success = True
        elif payload.startswith("project:") and self.project:
            self.window.move_project(payload.split(":", 1)[1], self.project.id)
            success = True
        Gtk.drag_finish(context, success, success, time_value)
