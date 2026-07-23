from __future__ import annotations

import concurrent.futures
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # noqa: E402

from .database import Database
from .integrations import IntegrationError, IntegrationManager
from .metadata import (
    GitInfoCache,
    ProcessUsageSampler,
    collect_snapshots,
    git_info,
    remove_agent_state,
)
from .models import (
    AgentKind,
    AgentRace,
    AgentStatus,
    ListeningService,
    Project,
    TerminalSession,
    TerminalSnapshot,
)
from .tmux_backend import TmuxBackend, TmuxError
from .worktrees import WorktreeError, compare_worktree, create_race_worktrees


PROJECT_TARGET = Gtk.TargetEntry.new("application/x-mujterm-project", Gtk.TargetFlags.SAME_APP, 1)
TERMINAL_TARGET = Gtk.TargetEntry.new("application/x-mujterm-terminal", Gtk.TargetFlags.SAME_APP, 2)


CSS = b"""
.mujterm-window, .mujterm-root {
  background: #070b14;
  color: #dce8fa;
}
.mujterm-header {
  min-height: 48px;
  padding: 4px 10px;
  background-image: linear-gradient(to right, #0a1020, #111a2c 52%, #081923);
  color: #e8f4ff;
  border-bottom: 1px solid #21445a;
  box-shadow: 0 3px 16px rgba(0, 0, 0, 0.48);
}
.brand-mark {
  color: #55e7ff;
  font-family: Monospace;
  font-size: 1.45em;
  font-weight: bold;
}
.brand-title { color: #f2f8ff; font-weight: bold; font-size: 1.05em; }
.brand-subtitle { color: #67849e; font-family: Monospace; font-size: 0.72em; }
.hud-button {
  min-width: 34px;
  min-height: 30px;
  color: #91abc4;
  background: rgba(45, 77, 105, 0.18);
  border: 1px solid rgba(100, 180, 220, 0.17);
  border-radius: 8px;
  box-shadow: none;
}
.hud-button:hover {
  color: #ffffff;
  background: rgba(57, 215, 239, 0.16);
  border-color: #2bb8d1;
}
.split-button {
  min-width: 38px;
  font-family: Monospace;
  font-size: 1.1em;
  font-weight: bold;
}
.keyboard-mode-on {
  color: #161225;
  background-image: linear-gradient(to right, #fde68a, #f0abfc);
  border-color: #fff2b2;
  font-weight: bold;
}
.mujterm-sidebar {
  background-image: linear-gradient(to bottom, #0a101c, #080d17);
  border-right: 1px solid #1a3447;
}
.sidebar-hud {
  padding: 18px 15px 14px 15px;
  background: rgba(15, 28, 45, 0.72);
  border-bottom: 1px solid #172f42;
}
.sidebar-kicker { color: #4ddff3; font-family: Monospace; font-size: 0.76em; font-weight: bold; }
.sidebar-title { color: #f0f7ff; font-size: 1.18em; font-weight: bold; }
.sidebar-stat { color: #6f8ca7; font-family: Monospace; font-size: 0.75em; }
.sidebar-footer {
  padding: 8px 14px;
  color: #3f657d;
  background: #070c15;
  border-top: 1px solid #142a3a;
  font-family: Monospace;
  font-size: 0.72em;
}
.mujterm-project-header { padding: 12px 10px 6px 12px; }
.mujterm-project-title { color: #91a8c0; font-weight: bold; font-size: 0.86em; }
.project-chevron { color: #35d3eb; font-family: Monospace; }
.project-count {
  color: #5d7892;
  background: rgba(69, 113, 143, 0.16);
  border: 1px solid rgba(86, 145, 180, 0.18);
  border-radius: 9px;
  padding: 1px 6px;
  font-family: Monospace;
  font-size: 0.72em;
}
.project-alert { color: #ffd76a; font-family: Monospace; font-weight: bold; }
.mujterm-terminal-row {
  color: #c9d8e8;
  background: rgba(14, 24, 39, 0.72);
  border: 1px solid rgba(52, 90, 119, 0.22);
  border-radius: 8px;
  padding: 9px 9px;
  margin: 2px 7px;
}
.mujterm-terminal-row:hover {
  color: #f3fbff;
  background: rgba(28, 55, 76, 0.66);
  border-color: rgba(73, 194, 220, 0.38);
}
.mujterm-terminal-row.active {
  color: #ffffff;
  background-image: linear-gradient(to right, rgba(25, 129, 157, 0.34), rgba(18, 45, 69, 0.80));
  border-color: #2d9fb5;
  box-shadow: inset 3px 0 #4ee6fa;
}
.terminal-row-action {
  min-width: 24px;
  min-height: 24px;
  padding: 0;
  color: #5f7b93;
  background: transparent;
  border: 0;
  box-shadow: none;
}
.terminal-row-action:hover { color: #61e3f4; background: rgba(61, 211, 235, 0.12); }
.terminal-row-close:hover { color: #ff7189; background: rgba(239, 67, 99, 0.13); }
.mujterm-path { color: #638099; font-family: Monospace; font-size: 0.78em; }
.mujterm-resources { color: #a9b3cc; font-family: Monospace; font-size: 0.72em; }
.port-chip {
  min-height: 19px;
  padding: 0 6px;
  color: #bbf7d0;
  background: rgba(134, 239, 172, 0.10);
  border: 1px solid rgba(134, 239, 172, 0.38);
  border-radius: 8px;
  font-family: Monospace;
  font-size: 0.68em;
}
.port-chip:hover { color: #ffffff; background: rgba(134, 239, 172, 0.20); }
.attention-panel { padding: 8px 10px; background: #211e34; border-bottom: 1px solid #5f567c; }
.attention-title { color: #fef3c7; font-weight: bold; font-size: 0.78em; }
.attention-item {
  color: #fff7d6;
  background: rgba(253, 230, 138, 0.09);
  border: 1px solid rgba(253, 230, 138, 0.30);
  border-radius: 7px;
  padding: 4px 7px;
  font-size: 0.78em;
}
.attention-item:hover { background: rgba(253, 230, 138, 0.18); border-color: #fde68a; }
.timeline-kind { color: #c4b5fd; font-family: Monospace; font-size: 0.76em; }
.timeline-summary { color: #f5f3ff; }
.timeline-meta { color: #9ca3b8; font-family: Monospace; font-size: 0.75em; }
.status-working, .status-action, .status-ready, .status-error, .status-shell {
  border-radius: 9px;
  padding: 2px 6px;
  font-family: Monospace;
  font-size: 0.72em;
}
.status-working { color: #62deff; background: rgba(36, 156, 208, 0.14); border: 1px solid rgba(63, 200, 242, 0.28); }
.status-action { color: #ffe07b; background: rgba(217, 154, 28, 0.15); border: 1px solid rgba(255, 198, 60, 0.36); font-weight: bold; }
.status-ready { color: #66f2a6; background: rgba(31, 190, 113, 0.13); border: 1px solid rgba(71, 230, 151, 0.28); }
.status-error { color: #ff778c; background: rgba(218, 50, 83, 0.14); border: 1px solid rgba(255, 91, 118, 0.30); }
.status-shell { color: #617b93; background: rgba(52, 78, 100, 0.12); border: 1px solid rgba(78, 112, 139, 0.18); }
.terminal-view { background: #050810; }
.terminal-hud {
  min-height: 41px;
  padding: 7px 13px;
  color: #bed1e4;
  background-image: linear-gradient(to right, #0c1421, #0a1722);
  border-bottom: 1px solid #17374a;
}
.terminal-hud-title { color: #eaf5ff; font-weight: bold; }
.terminal-hud-path { color: #5f809b; font-family: Monospace; font-size: 0.78em; }
.terminal-hud-chip {
  color: #60dff4;
  background: rgba(39, 154, 183, 0.12);
  border: 1px solid rgba(63, 191, 220, 0.25);
  border-radius: 9px;
  padding: 2px 8px;
  font-family: Monospace;
  font-size: 0.74em;
}
.terminal-shell { padding: 8px 10px 10px 10px; background: #050810; }
.mujterm-workspace { background: #050810; }
.welcome-glyph { color: #4ce3f5; font-family: Monospace; font-size: 3.4em; font-weight: bold; }
.welcome-status {
  color: #66f2a6;
  background: rgba(31, 190, 113, 0.11);
  border: 1px solid rgba(71, 230, 151, 0.25);
  border-radius: 10px;
  padding: 3px 10px;
  font-family: Monospace;
  font-size: 0.72em;
}
.welcome-title { color: #eff8ff; font-size: 1.8em; font-weight: bold; }
.welcome-copy { color: #647f99; }
.primary-action {
  color: #061217;
  background-image: linear-gradient(to right, #47def2, #68f3b2);
  border: 0;
  border-radius: 8px;
  padding: 8px 18px;
  font-weight: bold;
  box-shadow: 0 3px 12px rgba(41, 213, 228, 0.18);
}
.primary-action:hover { background-image: linear-gradient(to right, #77edfb, #8affca); }
.mujterm-infobar { background: #102131; color: #b9d9ef; border-bottom: 1px solid #24516a; }

/* High-contrast pastel theme */
.mujterm-window, .mujterm-root { background: #090d18; color: #f8fafc; }
.mujterm-header {
  background-image: linear-gradient(to right, #13182b, #20203a 52%, #142438);
  border-bottom-color: #7569a8;
}
.brand-mark, .sidebar-kicker, .project-chevron { color: #8be9fd; }
.brand-title, .sidebar-title, .welcome-title, .terminal-hud-title { color: #ffffff; }
.brand-subtitle, .sidebar-stat { color: #b9b8dc; }
.hud-button {
  color: #d8d5ff;
  background: rgba(177, 164, 255, 0.10);
  border-color: rgba(196, 181, 253, 0.34);
}
.hud-button:hover {
  color: #ffffff;
  background: rgba(196, 181, 253, 0.22);
  border-color: #c4b5fd;
}
.mujterm-sidebar {
  background-image: linear-gradient(to bottom, #121628, #0d1220);
  border-right-color: #4b5276;
}
.sidebar-hud { background: #181d33; border-bottom-color: #454c71; }
.sidebar-footer { color: #a6a7c5; background: #0c101d; border-top-color: #353b5a; }
.mujterm-project-title { color: #f1efff; font-size: 0.9em; }
.project-count {
  color: #e9d5ff;
  background: rgba(216, 180, 254, 0.12);
  border-color: rgba(216, 180, 254, 0.38);
}
.project-alert { color: #fde68a; }
.mujterm-terminal-row {
  color: #f3f4f6;
  background: #171c2e;
  border-color: #414966;
}
.mujterm-terminal-row:hover {
  color: #ffffff;
  background: #202741;
  border-color: #a5b4fc;
}
.mujterm-terminal-row.active {
  color: #ffffff;
  background-image: linear-gradient(to right, #3a315d, #253651);
  border-color: #c4b5fd;
  box-shadow: inset 4px 0 #f0abfc;
}
.mujterm-path, .terminal-hud-path { color: #b6c2d9; }
.terminal-row-action { color: #b9c1d9; }
.terminal-row-action:hover { color: #a5f3fc; background: rgba(165, 243, 252, 0.13); }
.terminal-row-close:hover { color: #fda4af; background: rgba(253, 164, 175, 0.14); }
.status-working { color: #bae6fd; background: rgba(125, 211, 252, 0.14); border-color: rgba(125, 211, 252, 0.48); }
.status-action { color: #fef3c7; background: rgba(253, 230, 138, 0.14); border-color: rgba(253, 230, 138, 0.52); }
.status-ready { color: #bbf7d0; background: rgba(134, 239, 172, 0.13); border-color: rgba(134, 239, 172, 0.48); }
.status-error { color: #fecdd3; background: rgba(253, 164, 175, 0.14); border-color: rgba(253, 164, 175, 0.50); }
.status-shell { color: #d8d5ff; background: rgba(196, 181, 253, 0.10); border-color: rgba(196, 181, 253, 0.34); }
.terminal-view, .terminal-shell, .mujterm-workspace { background: #080b14; }
.terminal-hud {
  color: #e5e7eb;
  background-image: linear-gradient(to right, #171b2e, #162238);
  border-bottom-color: #535b83;
}
.terminal-hud-chip { color: #bae6fd; background: rgba(125, 211, 252, 0.10); border-color: rgba(125, 211, 252, 0.38); }
.welcome-glyph { color: #c4b5fd; }
.welcome-status { color: #bbf7d0; background: rgba(134, 239, 172, 0.10); border-color: rgba(134, 239, 172, 0.38); }
.welcome-copy { color: #c4c7d8; }
.primary-action {
  color: #161225;
  background-image: linear-gradient(to right, #c4b5fd, #f0abfc 52%, #a5f3fc);
  box-shadow: 0 4px 18px rgba(196, 181, 253, 0.24);
}
.primary-action:hover { background-image: linear-gradient(to right, #ddd6fe, #f5d0fe 52%, #cffafe); }
.mujterm-infobar { background: #24213a; color: #f5f3ff; border-bottom-color: #7c6faf; }
"""


def display_path(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def resource_text(cpu_percent: float, memory_bytes: int) -> str:
    memory_mib = memory_bytes / (1024 * 1024)
    return f"CPU {cpu_percent:.1f}%  ·  RAM {memory_mib:.0f} MiB"


class TerminalView(Gtk.Box):
    def __init__(
        self,
        session: TerminalSession,
        backend: TmuxBackend,
        on_input: Callable[[str], None],
        on_exit: Callable[[str], None],
        on_focus: Callable[[str], None],
        on_key: Callable[[Gdk.EventKey], bool],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_style_context().add_class("terminal-view")
        self.session = session
        self.backend = backend
        self.on_input = on_input
        self.on_exit = on_exit
        self.on_focus = on_focus
        self.on_key = on_key
        self._build_hud()
        terminal_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        terminal_shell.get_style_context().add_class("terminal-shell")
        self.terminal = Vte.Terminal()
        self.terminal.set_scrollback_lines(50_000)
        self.terminal.set_scroll_on_output(False)
        self.terminal.set_scroll_on_keystroke(True)
        self.terminal.set_mouse_autohide(True)
        self.terminal.set_allow_hyperlink(True)
        self.terminal.set_font(Pango.FontDescription("Monospace 11"))
        self._apply_terminal_palette()
        self.terminal.connect("event", self._on_pointer_event)
        self.terminal.connect("button-press-event", self._on_button_press)
        self.terminal.connect("key-press-event", self._on_key_press)
        self.terminal.connect("button-press-event", self._on_pointer_input)
        self.terminal.connect("focus-in-event", self._on_focus_in)
        self.terminal.connect("child-exited", lambda *_args: self.on_exit(self.session.id))
        terminal_shell.pack_start(self.terminal, True, True, 0)
        self.pack_start(terminal_shell, True, True, 0)
        self._spawn()

    def _build_hud(self) -> None:
        hud = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hud.get_style_context().add_class("terminal-hud")
        hud.connect("size-allocate", self._hud_size_allocate)
        identity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.hud_title = Gtk.Label(label=self.session.name, xalign=0)
        self.hud_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.hud_title.get_style_context().add_class("terminal-hud-title")
        self.hud_path = Gtk.Label(label=display_path(self.session.last_cwd), xalign=0)
        self.hud_path.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.hud_path.get_style_context().add_class("terminal-hud-path")
        identity.pack_start(self.hud_title, False, False, 0)
        identity.pack_start(self.hud_path, False, False, 0)
        self.hud_branch = Gtk.Label(label="NO REPOSITORY")
        self.hud_branch.get_style_context().add_class("terminal-hud-chip")
        self.hud_resources = Gtk.Label(label="CPU 0.0%  ·  RAM 0 MiB")
        self.hud_resources.get_style_context().add_class("terminal-hud-chip")
        self.hud_services = Gtk.Label(label="NO SERVICES")
        self.hud_services.get_style_context().add_class("terminal-hud-chip")
        self.hud_agent = Gtk.Label()
        self.hud_agent.get_style_context().add_class("status-shell")
        self.hud_agent.set_no_show_all(True)
        hud.pack_start(identity, True, True, 0)
        hud.pack_end(self.hud_agent, False, False, 0)
        hud.pack_end(self.hud_resources, False, False, 0)
        hud.pack_end(self.hud_services, False, False, 0)
        hud.pack_end(self.hud_branch, False, False, 0)
        self.pack_start(hud, False, False, 0)

    def _hud_size_allocate(self, _hud: Gtk.Widget, allocation: Gdk.Rectangle) -> None:
        self._set_responsive_visibility(self.hud_branch, allocation.width >= 760)
        self._set_responsive_visibility(self.hud_services, allocation.width >= 660)
        self._set_responsive_visibility(self.hud_resources, allocation.width >= 500)

    @staticmethod
    def _set_responsive_visibility(widget: Gtk.Widget, visible: bool) -> None:
        widget.set_no_show_all(not visible)
        if visible:
            widget.show()
        else:
            widget.hide()

    def update_snapshot(self, snapshot: Optional[TerminalSnapshot], title: Optional[str] = None) -> None:
        if title:
            self.hud_title.set_text(title)
        if not snapshot:
            self.hud_agent.set_no_show_all(True)
            self.hud_agent.hide()
            return
        self.hud_path.set_text(display_path(snapshot.cwd))
        self.hud_path.set_tooltip_text(snapshot.cwd)
        self.hud_branch.set_text(f"GIT // {snapshot.branch}" if snapshot.branch else "NO REPOSITORY")
        self.hud_resources.set_text(resource_text(snapshot.cpu_percent, snapshot.memory_bytes))
        ports = " ".join(f":{service.port}" for service in snapshot.services)
        self.hud_services.set_text(f"PORTS // {ports}" if ports else "NO SERVICES")
        context = self.hud_agent.get_style_context()
        for class_name in ("status-working", "status-action", "status-ready", "status-error", "status-shell"):
            context.remove_class(class_name)
        agent = snapshot.agent.value.upper() if snapshot.agent else "SHELL"
        if snapshot.status == AgentStatus.SHELL and not snapshot.agent:
            self.hud_agent.set_no_show_all(True)
            self.hud_agent.hide()
            return
        self.hud_agent.set_no_show_all(False)
        self.hud_agent.show()
        if snapshot.status == AgentStatus.WORKING:
            context.add_class("status-working")
            text = f"{agent} // RUNNING"
        elif snapshot.status == AgentStatus.NEEDS_ACTION:
            context.add_class("status-action")
            text = f"{agent} // ACTION REQUIRED"
        elif snapshot.status == AgentStatus.READY:
            context.add_class("status-ready")
            text = f"{agent} // READY"
        elif snapshot.status in (AgentStatus.ERROR, AgentStatus.ENDED):
            context.add_class("status-error")
            text = f"{agent} // {'ENDED' if snapshot.status == AgentStatus.ENDED else 'ERROR'}"
        elif snapshot.status == AgentStatus.UNKNOWN:
            context.add_class("status-working")
            text = f"{agent} // LINKING"
        else:
            context.add_class("status-shell")
            text = "SHELL // STANDBY"
        self.hud_agent.set_text(text)

    def _apply_terminal_palette(self) -> None:
        foreground = self._color("#f1f5f9")
        background = self._color("#080b14")
        palette = [
            self._color(value)
            for value in (
                "#151a2b", "#fb7185", "#86efac", "#fde68a",
                "#93c5fd", "#c4b5fd", "#67e8f9", "#e5e7eb",
                "#64748b", "#fda4af", "#bbf7d0", "#fef3c7",
                "#bfdbfe", "#e9d5ff", "#a5f3fc", "#ffffff",
            )
        ]
        self.terminal.set_colors(foreground, background, palette)
        self.terminal.set_color_cursor(self._color("#f0abfc"))
        self.terminal.set_color_highlight(self._color("#3a315d"))

    @staticmethod
    def _color(value: str) -> Gdk.RGBA:
        color = Gdk.RGBA()
        color.parse(value)
        return color

    def _spawn(self) -> None:
        environment = [f"{key}={value}" for key, value in os.environ.items()]
        environment.append("COLORTERM=truecolor")
        argv = self.backend.attach_command(self.session.tmux_name)
        try:
            self.terminal.spawn_sync(
                Vte.PtyFlags.DEFAULT,
                self.session.last_cwd,
                argv,
                environment,
                GLib.SpawnFlags.SEARCH_PATH,
                None,
                None,
                None,
            )
        except GLib.Error as error:
            self.terminal.feed(f"\r\nMujTerm could not attach to tmux: {error}\r\n".encode())

    def _on_key_press(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if self.on_key(event):
            return True
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        if control and shift and event.keyval in (Gdk.KEY_C, Gdk.KEY_c):
            self.terminal.copy_clipboard_format(Vte.Format.TEXT)
            return True
        if control and shift and event.keyval in (Gdk.KEY_V, Gdk.KEY_v):
            self.terminal.paste_clipboard()
            self.on_input(self.session.id)
            return True
        if control and event.keyval in (Gdk.KEY_plus, Gdk.KEY_equal):
            self.terminal.set_font_scale(min(2.5, self.terminal.get_font_scale() + 0.1))
            return True
        if control and event.keyval in (Gdk.KEY_minus, Gdk.KEY_underscore):
            self.terminal.set_font_scale(max(0.5, self.terminal.get_font_scale() - 0.1))
            return True
        if event.keyval not in (
            Gdk.KEY_Shift_L,
            Gdk.KEY_Shift_R,
            Gdk.KEY_Control_L,
            Gdk.KEY_Control_R,
            Gdk.KEY_Alt_L,
            Gdk.KEY_Alt_R,
            Gdk.KEY_Page_Up,
            Gdk.KEY_Page_Down,
        ):
            self.on_input(self.session.id)
        return False

    def _on_pointer_input(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button == 1:
            self.on_input(self.session.id)
        return False

    def _on_pointer_event(self, _widget: Gtk.Widget, event: Gdk.Event) -> bool:
        """Keep plain left-button dragging available for native VTE selection."""
        is_left_button = getattr(event, "button", 0) == 1
        is_left_drag = bool(
            getattr(event, "state", 0) & Gdk.ModifierType.BUTTON1_MASK
        )
        if is_left_button or is_left_drag:
            event.state = getattr(event, "state", 0) | Gdk.ModifierType.SHIFT_MASK
        return False

    def _on_button_press(
        self, _widget: Gtk.Widget, event: Gdk.EventButton
    ) -> bool:
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        copy_item = Gtk.MenuItem(label="Copy")
        copy_item.set_sensitive(self.terminal.get_has_selection())
        copy_item.connect(
            "activate",
            lambda *_args: self.terminal.copy_clipboard_format(Vte.Format.TEXT),
        )
        paste_item = Gtk.MenuItem(label="Paste")
        paste_item.connect("activate", lambda *_args: self.terminal.paste_clipboard())
        select_all_item = Gtk.MenuItem(label="Select All")
        select_all_item.connect("activate", lambda *_args: self.terminal.select_all())
        menu.append(copy_item)
        menu.append(paste_item)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(select_all_item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _on_focus_in(self, *_args: Any) -> bool:
        self.on_focus(self.session.id)
        return False


class TerminalRow(Gtk.ListBoxRow):
    def __init__(self, window: "MainWindow", session: TerminalSession) -> None:
        super().__init__()
        self.window = window
        self.session = session
        self.get_style_context().add_class("mujterm-terminal-row")
        layout = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        click_target = Gtk.EventBox()
        click_target.set_visible_window(False)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.title = Gtk.Label(label=session.name, xalign=0)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.metadata = Gtk.Label(label=display_path(session.last_cwd), xalign=0)
        self.metadata.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.metadata.get_style_context().add_class("mujterm-path")
        self.resources = Gtk.Label(label="CPU 0.0%  ·  RAM 0 MiB", xalign=0)
        self.resources.get_style_context().add_class("mujterm-resources")
        self.ports_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        text.pack_start(self.title, False, False, 0)
        text.pack_start(self.metadata, False, False, 0)
        text.pack_start(self.resources, False, False, 0)
        text.pack_start(self.ports_box, False, False, 1)
        content.pack_start(text, True, True, 0)
        self.status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.spinner = Gtk.Spinner()
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
        duplicate = Gtk.Button.new_from_icon_name("edit-copy-symbolic", Gtk.IconSize.MENU)
        duplicate.set_relief(Gtk.ReliefStyle.NONE)
        duplicate.get_style_context().add_class("terminal-row-action")
        duplicate.set_tooltip_text("Duplicate from current directory")
        duplicate.connect("clicked", lambda *_args: self.window.duplicate_terminal(self.session.id))
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.get_style_context().add_class("terminal-row-action")
        close.get_style_context().add_class("terminal-row-close")
        close.set_tooltip_text("Close terminal")
        close.connect("clicked", lambda *_args: self.window.close_terminal(self.session.id))
        layout.pack_end(close, False, False, 0)
        layout.pack_end(duplicate, False, False, 0)
        self.add(layout)
        self.drag_dest_set(Gtk.DestDefaults.ALL, [TERMINAL_TARGET], Gdk.DragAction.MOVE)
        self.connect("drag-data-received", self._drag_data_received)
        self.update(None, False)

    def update(self, snapshot: Optional[TerminalSnapshot], active: bool) -> None:
        context = self.get_style_context()
        if active:
            context.add_class("active")
        else:
            context.remove_class("active")
        if not snapshot:
            self.metadata.set_text(display_path(self.session.last_cwd))
            self._update_services(())
            self._set_status(AgentStatus.SHELL, None)
            return
        metadata = display_path(snapshot.cwd)
        if snapshot.branch:
            metadata += f"  ·  {snapshot.branch}"
        self.metadata.set_text(metadata)
        self.metadata.set_tooltip_text(metadata)
        self.resources.set_text(resource_text(snapshot.cpu_percent, snapshot.memory_bytes))
        self._update_services(snapshot.services)
        self._set_status(snapshot.status, snapshot.agent.value.title() if snapshot.agent else None)

    def _update_services(self, services: tuple[ListeningService, ...]) -> None:
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
        context = self.status_box.get_style_context()
        for class_name in ("status-working", "status-action", "status-ready", "status-error", "status-shell"):
            context.remove_class(class_name)
        self.spinner.stop()
        self.spinner.hide()
        self.indicator.show()
        self.agent_label.set_text(agent or "")
        if status == AgentStatus.SHELL and not agent:
            self.status_box.set_no_show_all(True)
            self.status_box.hide()
            return
        self.status_box.set_no_show_all(False)
        self.status_box.show()
        if status == AgentStatus.WORKING:
            context.add_class("status-working")
            self.spinner.show()
            self.spinner.start()
            self.indicator.hide()
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
        self.header = Gtk.EventBox()
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        header_box.get_style_context().add_class("mujterm-project-header")
        self.chevron = Gtk.Label(label="▾")
        self.chevron.get_style_context().add_class("project-chevron")
        title = Gtk.Label(label=project.name if project else "Ungrouped", xalign=0)
        title.get_style_context().add_class("mujterm-project-title")
        count = Gtk.Label(label=str(len(terminals)))
        count.get_style_context().add_class("project-count")
        self.alert = Gtk.Label()
        self.alert.get_style_context().add_class("project-alert")
        rename_button: Optional[Gtk.Button] = None
        if project:
            rename_button = Gtk.Button.new_from_icon_name("document-edit-symbolic", Gtk.IconSize.MENU)
            rename_button.set_relief(Gtk.ReliefStyle.NONE)
            rename_button.get_style_context().add_class("terminal-row-action")
            rename_button.set_tooltip_text("Rename project")
            rename_button.connect("clicked", lambda *_args: self.window._rename_project(project.id))
        add_button = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.MENU)
        add_button.set_relief(Gtk.ReliefStyle.NONE)
        add_button.get_style_context().add_class("hud-button")
        add_button.set_tooltip_text("New terminal")
        add_button.connect("clicked", lambda *_args: self.window.create_terminal(self.project_id))
        header_box.pack_start(self.chevron, False, False, 0)
        header_box.pack_start(title, True, True, 0)
        header_box.pack_start(self.alert, False, False, 0)
        header_box.pack_start(count, False, False, 0)
        if rename_button:
            header_box.pack_start(rename_button, False, False, 0)
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
            self.alert.set_text(f"! {needs_action}")
        elif working:
            self.alert.set_text(f"◉ {working}")
        else:
            self.alert.set_text("")

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


class MainWindow(Gtk.ApplicationWindow):
    def __init__(
        self,
        application: Gtk.Application,
        database: Database,
        backend: TmuxBackend,
        integrations: IntegrationManager,
    ) -> None:
        super().__init__(application=application, title="MujTerm")
        self.database = database
        self.backend = backend
        self.integrations = integrations
        self.active_terminal_id: Optional[str] = None
        self.active_project_id: Optional[str] = None
        self.terminal_rows: dict[str, TerminalRow] = {}
        self.terminal_views: dict[str, TerminalView] = {}
        self.pane_hosts: dict[str, Gtk.Box] = {}
        self.workspaces: dict[str, Gtk.Box] = {}
        self.terminal_workspaces: dict[str, str] = {}
        self.workspace_focus: dict[str, str] = {}
        self.active_workspace_id: Optional[str] = None
        self.keyboard_mode = False
        self.project_sections: list[ProjectSection] = []
        self.snapshots: dict[str, TerminalSnapshot] = {}
        self._snapshot_future: Optional[concurrent.futures.Future[dict[str, TerminalSnapshot]]] = None
        self._usage_sampler = ProcessUsageSampler()
        self._git_cache = GitInfoCache()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="metadata")
        self._snapshot_timer_id: Optional[int] = None
        self._closing = False
        self._status_overrides: dict[str, AgentStatus] = {}
        self.get_style_context().add_class("mujterm-window")
        self.set_default_size(1180, 760)
        self.set_size_request(760, 480)
        self.connect("delete-event", self._delete_event)
        self._install_css()
        self.backend.reload_config()
        self._build_header()
        self._build_body()
        self._restore_sessions()
        self.rebuild_sidebar()
        terminals = self.database.list_terminals()
        if terminals:
            self.select_terminal(terminals[0].id)
        self._show_integration_banner_if_needed()
        self._snapshot_timer_id = GLib.timeout_add(250, self._snapshot_tick)

    def shutdown(self) -> None:
        self._closing = True
        if self._snapshot_timer_id is not None:
            GLib.source_remove(self._snapshot_timer_id)
            self._snapshot_timer_id = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _build_header(self) -> None:
        header = Gtk.HeaderBar(show_close_button=True)
        header.get_style_context().add_class("mujterm-header")
        identity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        mark = Gtk.Label(label=">_")
        mark.get_style_context().add_class("brand-mark")
        identity_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label="MUJTERM", xalign=0)
        title.get_style_context().add_class("brand-title")
        subtitle = Gtk.Label(label="LOCAL SESSION MATRIX", xalign=0)
        subtitle.get_style_context().add_class("brand-subtitle")
        identity_copy.pack_start(title, False, False, 0)
        identity_copy.pack_start(subtitle, False, False, 0)
        identity.pack_start(mark, False, False, 0)
        identity.pack_start(identity_copy, False, False, 0)
        header.set_custom_title(identity)
        project_button = Gtk.Button.new_from_icon_name("folder-new-symbolic", Gtk.IconSize.BUTTON)
        project_button.get_style_context().add_class("hud-button")
        project_button.set_tooltip_text("Open project")
        project_button.connect("clicked", lambda *_args: self.open_project_dialog())
        terminal_button = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        terminal_button.get_style_context().add_class("hud-button")
        terminal_button.set_tooltip_text("New terminal (Ctrl+Shift+T)")
        terminal_button.connect("clicked", lambda *_args: self.create_terminal_from_active())
        split_right = Gtk.Button(label="▥")
        split_right.get_style_context().add_class("hud-button")
        split_right.get_style_context().add_class("split-button")
        split_right.set_tooltip_text("Split right")
        split_right.connect("clicked", lambda *_args: self.split_active(Gtk.Orientation.HORIZONTAL))
        split_down = Gtk.Button(label="⬒")
        split_down.get_style_context().add_class("hud-button")
        split_down.get_style_context().add_class("split-button")
        split_down.set_tooltip_text("Split down")
        split_down.connect("clicked", lambda *_args: self.split_active(Gtk.Orientation.VERTICAL))
        timeline_button = Gtk.Button.new_from_icon_name("document-open-recent-symbolic", Gtk.IconSize.BUTTON)
        timeline_button.get_style_context().add_class("hud-button")
        timeline_button.set_tooltip_text("Project timeline")
        timeline_button.connect("clicked", lambda *_args: self.show_timeline())
        handoff_button = Gtk.Button(label="⇄")
        handoff_button.get_style_context().add_class("hud-button")
        handoff_button.set_tooltip_text("Create agent handoff")
        handoff_button.connect("clicked", lambda *_args: self.show_handoff())
        race_button = Gtk.Button(label="A/B")
        race_button.get_style_context().add_class("hud-button")
        race_button.set_tooltip_text("Run Codex and Claude in parallel worktrees")
        race_button.connect("clicked", lambda *_args: self.show_agent_races())
        map_button = Gtk.Button.new_from_icon_name("network-workgroup-symbolic", Gtk.IconSize.BUTTON)
        map_button.get_style_context().add_class("hud-button")
        map_button.set_tooltip_text("Service dependency map")
        map_button.connect("clicked", lambda *_args: self.show_service_map())
        self.keyboard_mode_button = Gtk.Button(label="KEYS")
        self.keyboard_mode_button.get_style_context().add_class("hud-button")
        self.keyboard_mode_button.set_tooltip_text("Keyboard mode (Ctrl+Space)")
        self.keyboard_mode_button.connect("clicked", lambda *_args: self.toggle_keyboard_mode())
        settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON)
        settings_button.get_style_context().add_class("hud-button")
        settings_button.set_tooltip_text("Agent integrations")
        settings_button.connect("clicked", lambda *_args: self.show_integration_dialog())
        header.pack_start(project_button)
        header.pack_start(terminal_button)
        header.pack_start(split_right)
        header.pack_start(split_down)
        header.pack_end(settings_button)
        header.pack_end(timeline_button)
        header.pack_end(map_button)
        header.pack_end(race_button)
        header.pack_end(handoff_button)
        header.pack_end(self.keyboard_mode_button)
        self.set_titlebar(header)
        accelerator = Gtk.AccelGroup()
        self.add_accel_group(accelerator)
        terminal_button.add_accelerator("clicked", accelerator, Gdk.KEY_T, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK, Gtk.AccelFlags.VISIBLE)
        split_right.add_accelerator("clicked", accelerator, Gdk.KEY_Right, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK, Gtk.AccelFlags.VISIBLE)
        split_down.add_accelerator("clicked", accelerator, Gdk.KEY_Down, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK, Gtk.AccelFlags.VISIBLE)
        self.keyboard_mode_button.add_accelerator("clicked", accelerator, Gdk.KEY_space, Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags.VISIBLE)

    def _build_body(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.get_style_context().add_class("mujterm-root")
        self.add(outer)
        self.banner = Gtk.InfoBar()
        self.banner.get_style_context().add_class("mujterm-infobar")
        self.banner.set_message_type(Gtk.MessageType.INFO)
        self.banner_label = Gtk.Label(xalign=0)
        self.banner.get_content_area().add(self.banner_label)
        self.banner.add_button("Enable", Gtk.ResponseType.ACCEPT)
        self.banner.add_button("Not now", Gtk.ResponseType.CLOSE)
        self.banner.connect("response", self._banner_response)
        outer.pack_start(self.banner, False, False, 0)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer.pack_start(paned, True, True, 0)
        sidebar_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar_shell.set_size_request(326, -1)
        sidebar_shell.get_style_context().add_class("mujterm-sidebar")
        sidebar_hud = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar_hud.get_style_context().add_class("sidebar-hud")
        kicker = Gtk.Label(label="// WORKSPACE NETWORK", xalign=0)
        kicker.get_style_context().add_class("sidebar-kicker")
        heading = Gtk.Label(label="Terminals", xalign=0)
        heading.get_style_context().add_class("sidebar-title")
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.session_count_label = Gtk.Label(label="00 SESSIONS", xalign=0)
        self.agent_count_label = Gtk.Label(label="00 AGENTS", xalign=0)
        self.session_count_label.get_style_context().add_class("sidebar-stat")
        self.agent_count_label.get_style_context().add_class("sidebar-stat")
        stats.pack_start(self.session_count_label, False, False, 0)
        stats.pack_start(self.agent_count_label, False, False, 0)
        sidebar_hud.pack_start(kicker, False, False, 0)
        sidebar_hud.pack_start(heading, False, False, 0)
        sidebar_hud.pack_start(stats, False, False, 4)
        sidebar_shell.pack_start(sidebar_hud, False, False, 0)
        self.attention_revealer = Gtk.Revealer()
        self.attention_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        attention_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        attention_panel.get_style_context().add_class("attention-panel")
        attention_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.attention_title = Gtk.Label(label="! ATTENTION", xalign=0)
        self.attention_title.get_style_context().add_class("attention-title")
        self.attention_next = Gtk.Button(label="NEXT")
        self.attention_next.get_style_context().add_class("attention-item")
        self.attention_next.set_tooltip_text("Jump to next terminal needing action (Ctrl+Shift+A)")
        self.attention_next.connect("clicked", lambda *_args: self.select_next_attention())
        attention_header.pack_start(self.attention_title, True, True, 0)
        attention_header.pack_end(self.attention_next, False, False, 0)
        self.attention_items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        attention_panel.pack_start(attention_header, False, False, 0)
        attention_panel.pack_start(self.attention_items, False, False, 0)
        self.attention_revealer.add(attention_panel)
        sidebar_shell.pack_start(self.attention_revealer, False, False, 0)
        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sidebar = Gtk.ListBox()
        self.sidebar.set_selection_mode(Gtk.SelectionMode.NONE)
        sidebar_scroll.add(self.sidebar)
        sidebar_shell.pack_start(sidebar_scroll, True, True, 0)
        footer = Gtk.Label(label="●  LOCAL LINK / TMUX CORE", xalign=0)
        footer.get_style_context().add_class("sidebar-footer")
        sidebar_shell.pack_end(footer, False, False, 0)
        paned.pack1(sidebar_shell, resize=False, shrink=False)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.stack.get_style_context().add_class("mujterm-workspace")
        self.stack.add_named(self._welcome_widget(), "welcome")
        paned.pack2(self.stack, resize=True, shrink=False)
        paned.set_position(326)
        attention_accelerator = Gtk.AccelGroup()
        self.add_accel_group(attention_accelerator)
        self.attention_next.add_accelerator(
            "clicked",
            attention_accelerator,
            Gdk.KEY_A,
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK,
            Gtk.AccelFlags.VISIBLE,
        )

    def _welcome_widget(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        glyph = Gtk.Label(label=">_")
        glyph.get_style_context().add_class("welcome-glyph")
        status = Gtk.Label(label="● SYSTEM READY")
        status.get_style_context().add_class("welcome-status")
        title = Gtk.Label(label="Command the workspace")
        title.get_style_context().add_class("welcome-title")
        copy = Gtk.Label(label="Persistent terminals. Live branches. Agent telemetry.")
        copy.get_style_context().add_class("welcome-copy")
        button = Gtk.Button(label="INITIALIZE PROJECT")
        button.get_style_context().add_class("primary-action")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_args: self.open_project_dialog())
        box.pack_start(glyph, False, False, 0)
        box.pack_start(status, False, False, 0)
        box.pack_start(title, False, False, 0)
        box.pack_start(copy, False, False, 0)
        box.pack_start(button, False, False, 8)
        return box

    def _restore_sessions(self) -> None:
        existing_ids = {terminal.id for terminal in self.database.list_terminals()}
        panes = self.backend.list_panes()
        for terminal_id, pane in panes.items():
            if terminal_id not in existing_ids:
                self.database.create_terminal(
                    None,
                    "Recovered terminal",
                    pane.cwd or str(Path.home()),
                    terminal_id=terminal_id,
                    tmux_name=pane.tmux_name,
                )
        for terminal in self.database.list_terminals():
            if not self.backend.has_session(terminal.tmux_name):
                fallback = self._recovery_cwd(terminal)
                self.backend.create_session(terminal, fallback)
                if fallback != terminal.last_cwd:
                    self.database.update_terminal_cwd(terminal.id, fallback)

    def _recovery_cwd(self, terminal: TerminalSession) -> str:
        if Path(terminal.last_cwd).is_dir():
            return terminal.last_cwd
        if terminal.project_id:
            project = self.database.get_project(terminal.project_id)
            if project and Path(project.root_path).is_dir():
                return project.root_path
        return str(Path.home())

    def rebuild_sidebar(self) -> None:
        for child in self.sidebar.get_children():
            self.sidebar.remove(child)
        self.terminal_rows.clear()
        self.project_sections.clear()
        for project in self.database.list_projects():
            terminals = [
                terminal
                for terminal in self.database.list_terminals(project.id)
                if terminal.project_id == project.id
            ]
            section = ProjectSection(self, project, terminals)
            self.project_sections.append(section)
            self.sidebar.add(section)
        ungrouped = self.database.list_ungrouped_terminals()
        if ungrouped:
            section = ProjectSection(self, None, ungrouped)
            self.project_sections.append(section)
            self.sidebar.add(section)
        self.sidebar.show_all()
        self._update_sidebar_stats()
        self._apply_snapshots()

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

    def create_terminal(
        self,
        project_id: Optional[str],
        cwd: Optional[str] = None,
        activate: bool = True,
    ) -> Optional[TerminalSession]:
        if project_id is None and not self.database.list_projects():
            self.open_project_dialog()
            return None
        project = self.database.get_project(project_id) if project_id else None
        start_cwd = cwd or (project.root_path if project else str(Path.home()))
        if not Path(start_cwd).is_dir():
            start_cwd = project.root_path if project and Path(project.root_path).is_dir() else str(Path.home())
        count = len(
            self.database.list_ungrouped_terminals()
            if project_id is None
            else [t for t in self.database.list_terminals(project_id) if t.project_id == project_id]
        )
        terminal = self.database.create_terminal(project_id, f"Terminal {count + 1}", start_cwd)
        try:
            self.backend.create_session(terminal)
        except TmuxError as exc:
            self.database.delete_terminal(terminal.id)
            self._error("Could not create terminal", str(exc))
            return None
        self._record_event(terminal, "terminal", f"Created in {display_path(start_cwd)}")
        self.rebuild_sidebar()
        if activate:
            self.select_terminal(terminal.id)
        return terminal

    def create_terminal_from_active(self) -> None:
        if not self.active_terminal_id:
            self.create_terminal(self.active_project_id)
            return
        terminal = self.database.get_terminal(self.active_terminal_id)
        if not terminal:
            self.create_terminal(self.active_project_id)
            return
        self.create_terminal(terminal.project_id, self._terminal_cwd(terminal))

    def duplicate_terminal(self, terminal_id: str) -> None:
        terminal = self.database.get_terminal(terminal_id)
        if terminal:
            self.create_terminal(terminal.project_id, self._terminal_cwd(terminal))

    def _terminal_cwd(self, terminal: TerminalSession) -> str:
        snapshot = self.snapshots.get(terminal.id)
        if snapshot and snapshot.cwd and Path(snapshot.cwd).is_dir():
            return snapshot.cwd
        return self._recovery_cwd(terminal)

    def select_terminal(self, terminal_id: str) -> None:
        terminal = self.database.get_terminal(terminal_id)
        if not terminal:
            return
        self.active_terminal_id = terminal_id
        self.active_project_id = terminal.project_id
        view = self._ensure_terminal_view(terminal)
        host = self.pane_hosts.get(terminal_id)
        if not host:
            workspace_id = terminal.id
            workspace = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            host = self._new_pane_host(view)
            workspace.pack_start(host, True, True, 0)
            self.workspaces[workspace_id] = workspace
            self.terminal_workspaces[terminal_id] = workspace_id
            self.pane_hosts[terminal_id] = host
            self.stack.add_named(workspace, f"workspace:{workspace_id}")
        workspace_id = self.terminal_workspaces[terminal_id]
        self.active_workspace_id = workspace_id
        self.workspace_focus[workspace_id] = terminal_id
        self.workspaces[workspace_id].show_all()
        self.stack.set_visible_child_name(f"workspace:{workspace_id}")
        host.show_all()
        view.terminal.grab_focus()
        self._apply_snapshots()

    def split_active(self, orientation: Gtk.Orientation) -> None:
        if not self.active_terminal_id:
            self.create_terminal_from_active()
            return
        current = self.database.get_terminal(self.active_terminal_id)
        current_host = self.pane_hosts.get(self.active_terminal_id)
        if not current or not current_host:
            return
        terminal = self.create_terminal(
            current.project_id,
            self._terminal_cwd(current),
            activate=False,
        )
        if not terminal:
            return
        direction = "right" if orientation == Gtk.Orientation.HORIZONTAL else "down"
        self._record_event(terminal, "layout", f"Opened as split {direction}")
        self._split_with_terminal(terminal, orientation)

    def _split_with_terminal(
        self, terminal: TerminalSession, orientation: Gtk.Orientation
    ) -> None:
        if not self.active_terminal_id:
            return
        current = self.database.get_terminal(self.active_terminal_id)
        current_host = self.pane_hosts.get(self.active_terminal_id)
        if not current or not current_host or current.id == terminal.id:
            return
        current_view = self._ensure_terminal_view(current)
        new_view = self._ensure_terminal_view(terminal)
        parent = current_host.get_parent()
        if not parent:
            return
        paned = Gtk.Paned(orientation=orientation)
        self._replace_child(parent, current_host, paned)
        first = self._new_pane_host(current_view)
        second = self._new_pane_host(new_view)
        paned.pack1(first, resize=True, shrink=True)
        paned.pack2(second, resize=True, shrink=True)
        self.pane_hosts[current.id] = first
        self.pane_hosts[terminal.id] = second
        workspace_id = self.terminal_workspaces[current.id]
        self.terminal_workspaces[terminal.id] = workspace_id
        self.workspace_focus[workspace_id] = terminal.id
        self.active_workspace_id = workspace_id
        self.active_terminal_id = terminal.id
        self.active_project_id = terminal.project_id
        paned.show_all()
        GLib.idle_add(self._balance_paned, paned, orientation)
        new_view.terminal.grab_focus()
        self._apply_snapshots()

    def _ensure_terminal_view(self, terminal: TerminalSession) -> TerminalView:
        view = self.terminal_views.get(terminal.id)
        if not view:
            view = TerminalView(
                terminal,
                self.backend,
                self._terminal_input,
                self._terminal_child_exit,
                self._terminal_focused,
                self._keyboard_mode_key,
            )
            self.terminal_views[terminal.id] = view
        return view

    def _terminal_focused(self, terminal_id: str) -> None:
        terminal = self.database.get_terminal(terminal_id)
        if not terminal:
            return
        self.active_terminal_id = terminal_id
        self.active_project_id = terminal.project_id
        self.active_workspace_id = self.terminal_workspaces.get(terminal_id)
        if self.active_workspace_id:
            self.workspace_focus[self.active_workspace_id] = terminal_id
            self.stack.set_visible_child_name(f"workspace:{self.active_workspace_id}")
        self._apply_snapshots()

    @staticmethod
    def _new_pane_host(view: TerminalView) -> Gtk.Box:
        host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        host.set_size_request(0, 0)
        parent = view.get_parent()
        if parent:
            parent.remove(view)
        host.pack_start(view, True, True, 0)
        return host

    def _active_host(self) -> Optional[Gtk.Box]:
        if self.active_terminal_id:
            return self.pane_hosts.get(self.active_terminal_id)
        return None

    def toggle_keyboard_mode(self) -> None:
        self.keyboard_mode = not self.keyboard_mode
        context = self.keyboard_mode_button.get_style_context()
        if self.keyboard_mode:
            context.add_class("keyboard-mode-on")
            self.keyboard_mode_button.set_label("KEY MODE")
            self.keyboard_mode_button.set_tooltip_text(
                "h/j/k/l move · [/] workspace · n new · v split right · s split down · x close · a attention · q exit"
            )
        else:
            context.remove_class("keyboard-mode-on")
            self.keyboard_mode_button.set_label("KEYS")
            self.keyboard_mode_button.set_tooltip_text("Keyboard mode (Ctrl+Space)")

    def _keyboard_mode_key(self, event: Gdk.EventKey) -> bool:
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if control and event.keyval == Gdk.KEY_space:
            self.toggle_keyboard_mode()
            return True
        if not self.keyboard_mode:
            return False
        key = Gdk.keyval_to_lower(event.keyval)
        if key in (Gdk.KEY_Escape, Gdk.KEY_q):
            self.toggle_keyboard_mode()
        elif key == Gdk.KEY_h:
            self._focus_direction(-1, 0)
        elif key == Gdk.KEY_j:
            self._focus_direction(0, 1)
        elif key == Gdk.KEY_k:
            self._focus_direction(0, -1)
        elif key == Gdk.KEY_l:
            self._focus_direction(1, 0)
        elif key == Gdk.KEY_bracketleft:
            self._cycle_workspace(-1)
        elif key == Gdk.KEY_bracketright:
            self._cycle_workspace(1)
        elif key == Gdk.KEY_n:
            self.create_terminal_from_active()
        elif key == Gdk.KEY_v:
            self.split_active(Gtk.Orientation.HORIZONTAL)
        elif key == Gdk.KEY_s:
            self.split_active(Gtk.Orientation.VERTICAL)
        elif key == Gdk.KEY_x and self.active_terminal_id:
            self.close_terminal(self.active_terminal_id)
        elif key == Gdk.KEY_a:
            self.select_next_attention()
        else:
            return True
        return True

    def _focus_direction(self, horizontal: int, vertical: int) -> None:
        if not self.active_terminal_id or not self.active_workspace_id:
            return
        workspace = self.workspaces.get(self.active_workspace_id)
        active_host = self.pane_hosts.get(self.active_terminal_id)
        if not workspace or not active_host:
            return
        origin_x, origin_y = self._widget_center(active_host, workspace)
        best: Optional[tuple[float, str]] = None
        for terminal_id, workspace_id in self.terminal_workspaces.items():
            if workspace_id != self.active_workspace_id or terminal_id == self.active_terminal_id:
                continue
            host = self.pane_hosts.get(terminal_id)
            if not host:
                continue
            candidate_x, candidate_y = self._widget_center(host, workspace)
            delta_x = candidate_x - origin_x
            delta_y = candidate_y - origin_y
            if horizontal and delta_x * horizontal <= 0:
                continue
            if vertical and delta_y * vertical <= 0:
                continue
            primary = abs(delta_x) if horizontal else abs(delta_y)
            secondary = abs(delta_y) if horizontal else abs(delta_x)
            score = primary + secondary * 0.35
            if best is None or score < best[0]:
                best = (score, terminal_id)
        if best:
            self.select_terminal(best[1])

    @staticmethod
    def _widget_center(widget: Gtk.Widget, ancestor: Gtk.Widget) -> tuple[float, float]:
        x = widget.get_allocated_width() / 2
        y = widget.get_allocated_height() / 2
        current: Optional[Gtk.Widget] = widget
        while current and current is not ancestor:
            allocation = current.get_allocation()
            x += allocation.x
            y += allocation.y
            current = current.get_parent()
        return x, y

    def _cycle_workspace(self, direction: int) -> None:
        workspace_ids = list(self.workspaces)
        if not workspace_ids:
            return
        try:
            index = workspace_ids.index(self.active_workspace_id)
        except ValueError:
            index = 0
        workspace_id = workspace_ids[(index + direction) % len(workspace_ids)]
        terminal_id = self.workspace_focus.get(workspace_id)
        if not terminal_id:
            terminal_id = next(
                (
                    item
                    for item, candidate_workspace in self.terminal_workspaces.items()
                    if candidate_workspace == workspace_id
                ),
                None,
            )
        if terminal_id:
            self.select_terminal(terminal_id)

    def _terminal_id_for_host(self, host: Gtk.Box) -> Optional[str]:
        return next(
            (terminal_id for terminal_id, candidate in self.pane_hosts.items() if candidate is host),
            None,
        )

    def _replace_child(self, parent: Gtk.Container, old: Gtk.Widget, new: Gtk.Widget) -> None:
        if isinstance(parent, Gtk.Paned):
            first = parent.get_child1() is old
            parent.remove(old)
            if first:
                parent.pack1(new, resize=True, shrink=True)
            else:
                parent.pack2(new, resize=True, shrink=True)
        else:
            parent.remove(old)
            parent.add(new)

    @staticmethod
    def _balance_paned(paned: Gtk.Paned, orientation: Gtk.Orientation) -> bool:
        size = paned.get_allocated_width() if orientation == Gtk.Orientation.HORIZONTAL else paned.get_allocated_height()
        if size > 0:
            paned.set_position(size // 2)
        return False

    def close_terminal(self, terminal_id: str) -> None:
        terminal = self.database.get_terminal(terminal_id)
        if not terminal:
            return
        snapshot = self.snapshots.get(terminal_id)
        if snapshot and snapshot.status in (AgentStatus.WORKING, AgentStatus.NEEDS_ACTION, AgentStatus.UNKNOWN):
            if not self._confirm("Close running terminal?", "Its tmux session and running processes will be terminated."):
                return
        self.backend.kill_session(terminal.tmux_name)
        self._record_event(terminal, "terminal", "Closed terminal session")
        self.database.delete_terminal(terminal_id)
        remove_agent_state(terminal_id)
        self._remove_pane(terminal_id)
        view = self.terminal_views.pop(terminal_id, None)
        if view:
            view.destroy()
        self.snapshots.pop(terminal_id, None)
        self._status_overrides.pop(terminal_id, None)
        if self.active_terminal_id == terminal_id:
            self.active_terminal_id = None
            visible = next(
                (
                    item
                    for item, workspace_id in self.terminal_workspaces.items()
                    if workspace_id == self.active_workspace_id
                ),
                None,
            )
            visible = visible or next(iter(self.pane_hosts), None)
            remaining = self.database.list_terminals()
            if visible:
                self.select_terminal(visible)
            elif remaining:
                self.select_terminal(remaining[0].id)
            else:
                self.stack.set_visible_child_name("welcome")
        self.rebuild_sidebar()

    def _remove_pane(self, terminal_id: str) -> None:
        host = self.pane_hosts.pop(terminal_id, None)
        workspace_id = self.terminal_workspaces.pop(terminal_id, None)
        if workspace_id and self.workspace_focus.get(workspace_id) == terminal_id:
            replacement = next(
                (
                    item
                    for item, candidate_workspace in self.terminal_workspaces.items()
                    if candidate_workspace == workspace_id
                ),
                None,
            )
            if replacement:
                self.workspace_focus[workspace_id] = replacement
            else:
                self.workspace_focus.pop(workspace_id, None)
        if not host:
            return
        parent = host.get_parent()
        if not parent:
            return
        workspace = self.workspaces.get(workspace_id) if workspace_id else None
        if parent is workspace:
            workspace.remove(host)
            if workspace_id:
                self.stack.remove(workspace)
                self.workspaces.pop(workspace_id, None)
                if self.active_workspace_id == workspace_id:
                    self.active_workspace_id = None
            return
        if not isinstance(parent, Gtk.Paned):
            parent.remove(host)
            return
        sibling = parent.get_child2() if parent.get_child1() is host else parent.get_child1()
        grandparent = parent.get_parent()
        parent.remove(host)
        if sibling:
            parent.remove(sibling)
        if grandparent:
            self._replace_child(grandparent, parent, sibling or Gtk.Box())

    def move_terminal(self, terminal_id: str, project_id: Optional[str], before_terminal_id: Optional[str] = None) -> None:
        self.database.move_terminal(terminal_id, project_id, before_terminal_id)
        if terminal_id == self.active_terminal_id:
            self.active_project_id = project_id
        self.rebuild_sidebar()

    def move_project(self, project_id: str, before_project_id: str) -> None:
        ids = [project.id for project in self.database.list_projects() if project.id != project_id]
        try:
            index = ids.index(before_project_id)
        except ValueError:
            index = len(ids)
        ids.insert(index, project_id)
        self.database.reorder_projects(ids)
        self.rebuild_sidebar()

    def open_service(self, port: int) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(f"http://localhost:{port}", None)
        except GLib.Error as exc:
            self._error("Could not open service", str(exc))

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
            self._error("Could not stop service", str(exc))

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
        terminal = self.create_terminal(
            source.project_id, self._terminal_cwd(source), activate=True
        )
        if not terminal:
            return
        self.database.rename_terminal(terminal.id, f"{agent.value.title()} handoff")
        try:
            self.backend.send_command(terminal.tmux_name, [agent.value, card])
        except TmuxError as exc:
            self._error(f"Could not start {agent.value}", str(exc))
        self._record_event(terminal, "handoff", f"Started {agent.value} from {source.name}")
        self.rebuild_sidebar()

    def show_agent_races(self) -> None:
        project = self.database.get_project(self.active_project_id) if self.active_project_id else None
        if not project:
            self._error("No active project", "Select a project before starting an agent race.")
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
        except TmuxError as exc:
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
            self._error("Could not update agent configuration", str(exc))

    def _show_integration_banner_if_needed(self) -> None:
        if self.integrations.status().complete:
            self._set_banner_visible(False)
        else:
            self.banner_label.set_text("Enable Codex and Claude Code hooks for exact working and needs-input status.")
            self._set_banner_visible(True)

    def _banner_response(self, _banner: Gtk.InfoBar, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            try:
                self.integrations.install()
                self._set_banner_visible(False)
            except IntegrationError as exc:
                self._error("Could not update agent configuration", str(exc))
        else:
            self._set_banner_visible(False)

    def agent_event_received(self, payload: dict[str, Any]) -> None:
        GLib.idle_add(self._handle_agent_event, payload)

    def _handle_agent_event(self, payload: dict[str, Any]) -> bool:
        terminal_id = payload.get("terminal_id")
        if isinstance(terminal_id, str):
            self._status_overrides.pop(terminal_id, None)
        self._start_snapshot_refresh()
        return False

    def _terminal_input(self, terminal_id: str) -> None:
        snapshot = self.snapshots.get(terminal_id)
        if snapshot and snapshot.status == AgentStatus.NEEDS_ACTION:
            self._status_overrides[terminal_id] = AgentStatus.WORKING
            self.snapshots[terminal_id] = TerminalSnapshot(
                terminal_id=snapshot.terminal_id,
                cwd=snapshot.cwd,
                command=snapshot.command,
                branch=snapshot.branch,
                git_root=snapshot.git_root,
                agent=snapshot.agent,
                status=AgentStatus.WORKING,
                cpu_percent=snapshot.cpu_percent,
                memory_bytes=snapshot.memory_bytes,
                services=snapshot.services,
                connected_ports=snapshot.connected_ports,
                dead=snapshot.dead,
            )
            self._apply_snapshots()

    def _terminal_child_exit(self, terminal_id: str) -> None:
        if self._closing:
            return
        snapshot = self.snapshots.get(terminal_id)
        if snapshot:
            self.snapshots[terminal_id] = TerminalSnapshot(
                terminal_id=snapshot.terminal_id,
                cwd=snapshot.cwd,
                command=snapshot.command,
                branch=snapshot.branch,
                git_root=snapshot.git_root,
                agent=snapshot.agent,
                status=AgentStatus.ENDED,
                cpu_percent=snapshot.cpu_percent,
                memory_bytes=snapshot.memory_bytes,
                services=snapshot.services,
                connected_ports=snapshot.connected_ports,
                dead=True,
            )
            self._apply_snapshots()

    def _start_snapshot_refresh(self) -> bool:
        if self._closing:
            return False
        if self._snapshot_future is None or self._snapshot_future.done():
            self._snapshot_future = self._executor.submit(self._collect_snapshot)
            self._snapshot_future.add_done_callback(lambda future: GLib.idle_add(self._snapshot_done, future))
        return True

    def _snapshot_tick(self) -> bool:
        self._snapshot_timer_id = None
        if self._closing:
            return False
        self._start_snapshot_refresh()
        delay = 1000 if self.is_active() else 3000
        self._snapshot_timer_id = GLib.timeout_add(delay, self._snapshot_tick)
        return False

    def _collect_snapshot(self) -> dict[str, TerminalSnapshot]:
        panes = self.backend.list_panes()
        usage = self._usage_sampler.sample(
            pane.pane_pid for pane in panes.values() if pane.pane_pid
        )
        return collect_snapshots(
            panes,
            usage,
            detected_agents=self._usage_sampler.agents,
            git_cache=self._git_cache,
        )

    def _snapshot_done(self, future: concurrent.futures.Future[dict[str, TerminalSnapshot]]) -> bool:
        if self._closing:
            return False
        try:
            snapshots = future.result()
        except Exception:
            return False
        self._record_snapshot_events(self.snapshots, snapshots)
        self.snapshots = snapshots
        for terminal_id, snapshot in snapshots.items():
            terminal = self.database.get_terminal(terminal_id)
            if terminal and snapshot.cwd and snapshot.cwd != terminal.last_cwd:
                self.database.update_terminal_cwd(terminal_id, snapshot.cwd)
        self._apply_snapshots()
        return False

    def _apply_snapshots(self) -> None:
        for terminal_id, row in self.terminal_rows.items():
            snapshot = self.snapshots.get(terminal_id)
            override = self._status_overrides.get(terminal_id)
            if snapshot and override:
                snapshot = TerminalSnapshot(
                    terminal_id=snapshot.terminal_id,
                    cwd=snapshot.cwd,
                    command=snapshot.command,
                    branch=snapshot.branch,
                    git_root=snapshot.git_root,
                    agent=snapshot.agent,
                    status=override,
                    cpu_percent=snapshot.cpu_percent,
                    memory_bytes=snapshot.memory_bytes,
                    services=snapshot.services,
                    connected_ports=snapshot.connected_ports,
                    dead=snapshot.dead,
                )
            row.update(snapshot, terminal_id == self.active_terminal_id)
            view = self.terminal_views.get(terminal_id)
            terminal = self.database.get_terminal(terminal_id)
            if view:
                view.update_snapshot(snapshot, terminal.name if terminal else None)
        for section in self.project_sections:
            section.update_summary(self.snapshots)
        self._update_sidebar_stats()
        self._update_attention_queue()

    def _record_event(self, terminal: TerminalSession, kind: str, summary: str) -> None:
        self.database.append_timeline_event(
            terminal.project_id,
            terminal.id,
            terminal.name,
            kind,
            summary,
            time.time(),
        )

    def _record_snapshot_events(
        self,
        previous: dict[str, TerminalSnapshot],
        current: dict[str, TerminalSnapshot],
    ) -> None:
        status_messages = {
            AgentStatus.WORKING: "Agent started working",
            AgentStatus.NEEDS_ACTION: "Agent needs your action",
            AgentStatus.READY: "Agent finished and is ready",
            AgentStatus.ERROR: "Agent stopped with an error",
            AgentStatus.ENDED: "Terminal process ended",
        }
        for terminal_id, snapshot in current.items():
            before = previous.get(terminal_id)
            terminal = self.database.get_terminal(terminal_id)
            if not before or not terminal:
                continue
            if snapshot.branch and snapshot.branch != before.branch:
                old_branch = before.branch or "no branch"
                self._record_event(
                    terminal,
                    "git",
                    f"Branch changed from {old_branch} to {snapshot.branch}",
                )
            if snapshot.status != before.status and snapshot.status in status_messages:
                agent = snapshot.agent.value.title() if snapshot.agent else "Process"
                self._record_event(
                    terminal,
                    "agent",
                    f"{agent}: {status_messages[snapshot.status]}",
                )
            old_ports = {service.port for service in before.services}
            new_ports = {service.port for service in snapshot.services}
            for port in sorted(new_ports - old_ports):
                self._record_event(terminal, "service", f"Started listening on localhost:{port}")
            for port in sorted(old_ports - new_ports):
                self._record_event(terminal, "service", f"Stopped listening on localhost:{port}")

    def _update_attention_queue(self) -> None:
        terminals = {terminal.id: terminal for terminal in self.database.list_terminals()}
        self._attention_ids = [
            terminal_id
            for terminal_id in terminals
            if self.snapshots.get(terminal_id)
            and self._status_overrides.get(
                terminal_id, self.snapshots[terminal_id].status
            ) == AgentStatus.NEEDS_ACTION
        ]
        for child in self.attention_items.get_children():
            self.attention_items.remove(child)
        self.attention_title.set_text(f"! ATTENTION  {len(self._attention_ids)}")
        for terminal_id in self._attention_ids[:5]:
            terminal = terminals[terminal_id]
            project = self.database.get_project(terminal.project_id) if terminal.project_id else None
            prefix = f"{project.name} · " if project else ""
            button = Gtk.Button(label=f"!  {prefix}{terminal.name}")
            button.set_halign(Gtk.Align.FILL)
            button.get_style_context().add_class("attention-item")
            button.connect(
                "clicked",
                lambda _button, item=terminal_id: self.select_terminal(item),
            )
            self.attention_items.pack_start(button, False, False, 0)
        self.attention_items.show_all()
        self.attention_revealer.set_reveal_child(bool(self._attention_ids))

    def select_next_attention(self) -> None:
        if not getattr(self, "_attention_ids", None):
            return
        try:
            index = self._attention_ids.index(self.active_terminal_id)
        except ValueError:
            index = -1
        self.select_terminal(self._attention_ids[(index + 1) % len(self._attention_ids)])

    def _update_sidebar_stats(self) -> None:
        sessions = len(self.database.list_terminals())
        agents = sum(1 for snapshot in self.snapshots.values() if snapshot.agent)
        self.session_count_label.set_text(f"{sessions:02d} SESSIONS")
        self.agent_count_label.set_text(f"{agents:02d} AGENTS")

    def _set_banner_visible(self, visible: bool) -> None:
        self.banner.set_no_show_all(not visible)
        if visible:
            self.banner.show_all()
        else:
            self.banner.hide()

    def _delete_event(self, *_args: Any) -> bool:
        self.shutdown()
        return False

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

    @staticmethod
    def _install_css() -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
