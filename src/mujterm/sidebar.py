from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import cairo
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
from .status_display import (
    CPU_HISTORY_SECONDS,
    PILL_CLASSES,
    SPARKLINE_TONES,
    StatusPill,
    row_location,
    row_status_pill,
    sparkline_points,
    sparkline_tone,
    under_pressure,
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


class CpuSparkline(Gtk.DrawingArea):
    """Tiny CPU chart of the last minute; its CSS colour follows the state."""

    WIDTH = 40
    HEIGHT = 14

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(self.WIDTH, self.HEIGHT)
        self.set_valign(Gtk.Align.CENTER)
        self.get_style_context().add_class("cpu-sparkline")
        self._samples: tuple[tuple[float, float], ...] = ()
        self._now = 0.0
        self._tone = ""
        self.set_tone("spark-idle")
        self.connect("draw", self._draw)

    def set_tone(self, tone: str) -> None:
        if tone == self._tone:
            return
        self._tone = tone
        context = self.get_style_context()
        for candidate in SPARKLINE_TONES:
            context.remove_class(candidate)
        context.add_class(tone)
        self.queue_draw()

    def set_samples(self, samples: tuple[tuple[float, float], ...], now: float) -> None:
        self._samples = samples
        self._now = now
        self.queue_draw()

    def _draw(self, _widget: Gtk.Widget, cr: Any) -> bool:
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        color = self.get_style_context().get_color(self.get_state_flags())
        # Keep half the stroke inside the widget at 0% and 100%.
        points = [
            (x, y + 1.0)
            for x, y in sparkline_points(
                self._samples, self._now, CPU_HISTORY_SECONDS, width, height - 2.0
            )
        ]
        cr.set_line_width(1.5)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if len(points) < 2:
            cr.set_source_rgba(color.red, color.green, color.blue, 0.6)
            cr.set_dash([2.0, 2.0])
            cr.move_to(0, height - 1.0)
            cr.line_to(width, height - 1.0)
            cr.stroke()
            return False
        if self._tone != "spark-idle":
            cr.move_to(points[0][0], height)
            for x, y in points:
                cr.line_to(x, y)
            cr.line_to(points[-1][0], height)
            cr.close_path()
            cr.set_source_rgba(color.red, color.green, color.blue, 0.18)
            cr.fill()
        cr.move_to(*points[0])
        for x, y in points[1:]:
            cr.line_to(x, y)
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        cr.stroke()
        return False


def status_tooltip(snapshot: Optional[TerminalSnapshot]) -> str:
    agent = snapshot.agent.value.title() if snapshot and snapshot.agent else None
    status = snapshot.status if snapshot else AgentStatus.SHELL
    who = agent or "Agent"
    if status == AgentStatus.WORKING:
        return f"{who} is working"
    if status == AgentStatus.NEEDS_ACTION:
        return f"{who} needs your input"
    if status == AgentStatus.ENDED:
        return "Terminal ended"
    if status == AgentStatus.ERROR:
        return f"{who} stopped with an error"
    if status == AgentStatus.UNKNOWN:
        return f"{who} detected; waiting for hook events"
    if snapshot and under_pressure(snapshot):
        return "High CPU or memory use"
    if status == AgentStatus.READY:
        return f"{who} is ready"
    return agent or "Shell"


class TerminalRow(Gtk.ListBoxRow):
    def __init__(
        self,
        window: "MainWindow",
        session: TerminalSession,
        project_root: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.window = window
        self.session = session
        self.project_root = project_root
        self._last_active: Optional[bool] = None
        self._last_metadata: Optional[str] = None
        self._last_branch: Optional[str] = None
        self._last_resources: Optional[str] = None
        self._last_cpu: Optional[tuple[str, bool]] = None
        self._last_services: Optional[tuple[ListeningService, ...]] = None
        self._last_status: Optional[tuple[AgentStatus, Optional[str]]] = None
        self._last_pill: Optional[tuple[Optional[StatusPill], str]] = None
        self.get_style_context().add_class("mujterm-terminal-row")
        layout = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.click_target = click_target = Gtk.EventBox()
        click_target.set_visible_window(False)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.title = Gtk.Label(label=session.name, xalign=0)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.get_style_context().add_class("terminal-row-title")
        # The pill names the state in words; the glyph and spinner inside it
        # keep the state readable without relying on colour.
        self.status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.status_box.set_valign(Gtk.Align.CENTER)
        self.status_box.get_style_context().add_class("status-pill")
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        self.indicator = Gtk.Label()
        self.indicator.set_no_show_all(True)
        self.status_label = Gtk.Label()
        self.status_box.pack_start(self.spinner, False, False, 0)
        self.status_box.pack_start(self.indicator, False, False, 0)
        self.status_box.pack_start(self.status_label, False, False, 0)
        heading.pack_start(self.title, True, True, 0)
        heading.pack_end(self.status_box, False, False, 0)
        details = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.metadata = Gtk.Label(label=self._location(session.last_cwd), xalign=0)
        # Trim the start so the folder name and branch stay readable next to
        # the sparkline.
        self.metadata.set_ellipsize(Pango.EllipsizeMode.START)
        self.metadata.get_style_context().add_class("mujterm-path")
        self.branch = Gtk.Label(xalign=0)
        self.branch.set_ellipsize(Pango.EllipsizeMode.END)
        self.branch.set_max_width_chars(12)
        self.branch.set_no_show_all(True)
        self.branch.get_style_context().add_class("terminal-row-branch")
        self.sparkline = CpuSparkline()
        self.cpu_label = Gtk.Label(xalign=1)
        self.cpu_label.set_width_chars(4)
        self.cpu_label.get_style_context().add_class("terminal-row-cpu")
        details.pack_start(self.metadata, False, True, 0)
        details.pack_start(self.branch, False, True, 0)
        details.pack_end(self.cpu_label, False, False, 0)
        details.pack_end(self.sparkline, False, False, 0)
        self.ports_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        text.pack_start(heading, False, False, 0)
        text.pack_start(details, False, False, 0)
        text.pack_start(self.ports_box, False, False, 2)
        click_target.add(text)
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
        self.sparkline.set_tone(sparkline_tone(snapshot))
        if not snapshot:
            metadata = self._location(self.session.last_cwd)
            if metadata != self._last_metadata:
                self._last_metadata = metadata
                self.metadata.set_text(metadata)
            self._update_branch(None)
            self._update_cpu(None)
            self._update_services(())
            self._set_status(None)
            return
        metadata = self._location(snapshot.cwd)
        if metadata != self._last_metadata:
            self._last_metadata = metadata
            self.metadata.set_text(metadata)
        self._update_branch(snapshot.branch)
        resources = resource_text(snapshot.cpu_percent, snapshot.memory_bytes)
        location = display_path(snapshot.cwd)
        if snapshot.branch:
            location += f" · {snapshot.branch}"
        tooltip = f"{location}\n{resources}"
        if tooltip != self._last_resources:
            self._last_resources = tooltip
            self.click_target.set_tooltip_text(tooltip)
        self._update_cpu(snapshot)
        self._update_services(snapshot.services)
        self._set_status(snapshot)

    def _location(self, cwd: str) -> str:
        return row_location(cwd, self.project_root)

    def _update_branch(self, branch: Optional[str]) -> None:
        if branch == self._last_branch:
            return
        self._last_branch = branch
        self.branch.set_text(f"⎇ {branch}" if branch else "")
        self.branch.set_visible(bool(branch))

    def update_cpu_history(
        self, samples: tuple[tuple[float, float], ...], now: float
    ) -> None:
        self.sparkline.set_samples(samples, now)

    def _update_cpu(self, snapshot: Optional[TerminalSnapshot]) -> None:
        if snapshot is None or snapshot.dead:
            state = ("–", False)
        else:
            state = (f"{snapshot.cpu_percent:.0f}%", under_pressure(snapshot))
        if state == self._last_cpu:
            return
        self._last_cpu = state
        text, hot = state
        self.cpu_label.set_text(text)
        context = self.cpu_label.get_style_context()
        if hot:
            context.add_class("hot")
        else:
            context.remove_class("hot")

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

    def _set_status(self, snapshot: Optional[TerminalSnapshot]) -> None:
        pill = row_status_pill(snapshot)
        tooltip = status_tooltip(snapshot)
        self._last_status = (
            snapshot.status if snapshot else AgentStatus.SHELL,
            snapshot.agent.value.title() if snapshot and snapshot.agent else None,
        )
        if (pill, tooltip) == self._last_pill:
            self._sync_status_animation()
            return
        self._last_pill = (pill, tooltip)
        context = self.status_box.get_style_context()
        for class_name in PILL_CLASSES:
            context.remove_class(class_name)
        if pill is None:
            self.indicator.set_text("")
            self.status_box.set_no_show_all(True)
            self.status_box.hide()
            self._sync_status_animation()
            return
        context.add_class(pill.css_class)
        self.indicator.set_text(pill.glyph)
        self.status_label.set_text(pill.label)
        self.status_box.set_tooltip_text(tooltip)
        self.status_box.set_no_show_all(False)
        self.status_box.show()
        self.status_label.show()
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
        if self.indicator.get_text():
            self.indicator.show()
        else:
            self.indicator.hide()

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
        self.alert.set_no_show_all(True)
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
            row = TerminalRow(
                window, terminal, project.root_path if project else None
            )
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
        self._sync_alert_visibility()

    def _sync_alert_visibility(self) -> None:
        # Expanded rows carry their own status pills; the summary only has
        # to speak for a collapsed project.
        self.alert.set_visible(bool(self._summary_text) and not self.rows.get_visible())

    def _header_click(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button == 3 and self.project:
            self.window.show_project_menu(self.project.id, event)
            return True
        if event.button == 1:
            visible = self.rows.get_visible()
            self.rows.set_no_show_all(visible)
            self.rows.set_visible(not visible)
            self.chevron.set_text("▾" if not visible else "▸")
            self._sync_alert_visibility()
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
