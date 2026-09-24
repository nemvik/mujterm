from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from .agent_tools import AgentToolsMixin
from .database import Database
from .dialogs import WindowDialogsMixin
from .integrations import IntegrationError, IntegrationManager
from .logging_config import record_runtime_error
from .metadata import (
    GitInfoCache,
    ProcessUsageSampler,
    collect_snapshots,
    remove_agent_state,
)
from .models import (
    AgentStatus,
    SshConnection,
    TerminalSession,
    TerminalSnapshot,
    ToolboxCommand,
)
from .search import SearchMixin, literal_search_regex, output_match_summary
from .sidebar import PROJECT_TARGET, TERMINAL_TARGET, ProjectSection, TerminalRow
from .tmux_backend import TmuxBackend, TmuxError
from .terminal_view import (
    CommandStartContext,
    PCRE2_CASELESS,
    PCRE2_MULTILINE,
    PCRE2_UCP,
    PCRE2_UTF,
    RADAR_CLASSES,
    SHELL_LIKE_COMMANDS,
    SemanticCommandBlock,
    TerminalView,
    URL_PATTERN,
    display_path,
    extract_prompt_command,
    ghost_diff,
    normalized_url,
    quiet_radar_state,
    resource_text,
    selection_autoscroll_lines,
    selection_autoscroll_y,
    shell_like_command,
    terminal_output_delta,
    without_trailing_prompt,
)


__all__ = [
    "CommandStartContext",
    "MainWindow",
    "PCRE2_CASELESS",
    "PCRE2_MULTILINE",
    "PCRE2_UCP",
    "PCRE2_UTF",
    "PROJECT_TARGET",
    "ProjectSection",
    "RADAR_CLASSES",
    "SHELL_LIKE_COMMANDS",
    "SemanticCommandBlock",
    "TERMINAL_TARGET",
    "TerminalRow",
    "TerminalView",
    "URL_PATTERN",
    "display_path",
    "extract_prompt_command",
    "ghost_diff",
    "literal_search_regex",
    "normalized_url",
    "output_match_summary",
    "quiet_radar_state",
    "resource_text",
    "selection_autoscroll_lines",
    "selection_autoscroll_y",
    "shell_like_command",
    "terminal_output_delta",
    "without_trailing_prompt",
]


CSS = b"""
/* One quiet palette shared by the chrome and the terminal itself: the status
   colours are the terminal's own ANSI colours, surfaces are flat graphite. */
@define-color mt_sidebar #17191e;
@define-color mt_surface #1c1f25;
@define-color mt_raised #262a31;
@define-color mt_hover #21242a;
@define-color mt_line #2b2f37;
@define-color mt_text #dde1e7;
@define-color mt_muted #8b919b;
@define-color mt_faint #5d636d;
@define-color mt_blue #61afef;
@define-color mt_yellow #e5c07b;
@define-color mt_green #98c379;
@define-color mt_red #e06c75;
@define-color mt_cyan #56b6c2;
@define-color mt_magenta #c678dd;

.mujterm-root { background-color: @mt_surface; }

/* Sidebar */
.mujterm-sidebar {
  background-color: @mt_sidebar;
  border-right: 1px solid @mt_line;
}
.mujterm-sidebar list,
.mujterm-sidebar row,
.mujterm-sidebar row:selected,
.mujterm-sidebar row:hover {
  background-color: transparent;
  background-image: none;
  box-shadow: none;
}
.mujterm-project-header { padding: 14px 10px 4px 14px; }
.mujterm-project-title { color: @mt_muted; font-weight: bold; font-size: 0.86em; }
.project-chevron { color: @mt_faint; font-size: 0.8em; }
.project-count { color: @mt_faint; font-size: 0.82em; }
.project-alert { color: @mt_yellow; font-weight: bold; font-size: 0.82em; }
.project-ssh {
  color: @mt_muted;
  border: 1px solid @mt_line;
  border-radius: 4px;
  padding: 0 4px;
  font-size: 0.72em;
}
.sidebar-action {
  min-width: 22px;
  min-height: 22px;
  padding: 0;
  color: @mt_faint;
  background: none;
  border: 0;
  box-shadow: none;
}
.sidebar-action:hover { color: @mt_text; background-color: @mt_raised; }
.mujterm-terminal-row {
  color: @mt_text;
  border-radius: 6px;
  padding: 6px 6px 6px 10px;
  margin: 1px 6px;
}
.mujterm-sidebar row.mujterm-terminal-row:hover { background-color: @mt_hover; }
.mujterm-sidebar row.mujterm-terminal-row.active {
  background-color: @mt_raised;
  box-shadow: inset 2px 0 @mt_blue;
}
.terminal-row-title { color: @mt_text; }
.mujterm-path { color: @mt_muted; font-size: 0.84em; }
.terminal-row-action {
  min-width: 22px;
  min-height: 22px;
  padding: 0;
  color: @mt_faint;
  background: none;
  border: 0;
  box-shadow: none;
  opacity: 0;
}
.mujterm-terminal-row:hover .terminal-row-action,
.mujterm-terminal-row.active .terminal-row-action { opacity: 1; }
.terminal-row-action:hover { color: @mt_red; background-color: alpha(@mt_red, 0.12); }
.port-chip {
  min-height: 0;
  padding: 0 5px;
  color: @mt_cyan;
  background: none;
  border: 1px solid alpha(@mt_cyan, 0.35);
  border-radius: 4px;
  box-shadow: none;
  font-size: 0.78em;
}
.port-chip:hover { background-color: alpha(@mt_cyan, 0.12); }
.attention-panel {
  padding: 8px 8px 10px 14px;
  border-bottom: 1px solid @mt_line;
}
.attention-title { color: @mt_yellow; font-weight: bold; font-size: 0.86em; }
.attention-next, .attention-item {
  min-height: 0;
  padding: 3px 6px;
  color: @mt_text;
  background: none;
  border: 0;
  box-shadow: none;
  font-size: 0.88em;
}
.attention-next { color: @mt_yellow; }
.attention-next:hover, .attention-item:hover { background-color: @mt_raised; }

/* Status text uses colour plus a distinct glyph, never colour alone. */
.status-working { color: @mt_blue; }
.status-action { color: @mt_yellow; font-weight: bold; }
.status-ready { color: @mt_green; }
.status-error { color: @mt_red; }
.status-shell { color: @mt_muted; }
.status-working, .status-action, .status-ready, .status-error, .status-shell { font-size: 0.86em; }

/* Header bar: the theme draws it; only the attention button is tinted. */
.attention-button-active { color: @mt_yellow; font-weight: bold; }

/* Command toolbox */
.toolbox-title { font-weight: bold; }
.toolbox-target { color: @mt_muted; font-size: 0.86em; }
.toolbox-row { border-radius: 6px; }
.toolbox-row:hover { background-color: alpha(currentColor, 0.06); }
.toolbox-insert { padding: 4px 6px; background: none; border: 0; box-shadow: none; }
.toolbox-name { font-weight: bold; }
.toolbox-command { font-family: Monospace; font-size: 0.84em; opacity: 0.7; }
.toolbox-empty { padding: 14px 6px; opacity: 0.7; }
.toolbox-error { color: @mt_red; font-size: 0.9em; }

/* Terminal pane */
.terminal-view, .terminal-shell, .mujterm-workspace { background-color: @mt_surface; }
.terminal-hud {
  min-height: 30px;
  padding: 3px 8px 3px 12px;
  color: @mt_muted;
  background-color: @mt_surface;
  border-bottom: 1px solid @mt_line;
}
.terminal-hud-title { color: @mt_muted; font-weight: bold; }
.terminal-view.focused .terminal-hud-title { color: @mt_text; }
.terminal-hud-path { color: @mt_faint; }
.terminal-hud-branch { color: @mt_magenta; }
.terminal-hud-services { color: @mt_cyan; }
.terminal-hud-resources { color: @mt_faint; font-feature-settings: "tnum"; }
.terminal-hud-path, .terminal-hud-branch, .terminal-hud-services, .terminal-hud-resources { font-size: 0.86em; }
.terminal-shell { padding: 6px 4px 4px 10px; }
.radar-indicator { color: @mt_faint; font-size: 0.8em; }
.radar-indicator.radar-working { color: @mt_blue; }
.radar-indicator.radar-attention { color: @mt_yellow; }
.radar-indicator.radar-ready { color: @mt_green; }
.radar-indicator.radar-error { color: @mt_red; }
.radar-indicator.radar-hot { color: @mt_magenta; }
.radar-indicator.radar-service { color: @mt_cyan; }
.terminal-hud-button {
  min-height: 22px;
  padding: 0 8px;
  color: @mt_muted;
  background: none;
  border: 0;
  border-radius: 4px;
  box-shadow: none;
  font-size: 0.86em;
}
.terminal-hud-button:hover { color: @mt_text; background-color: @mt_raised; }
.terminal-hud-button:checked { color: @mt_text; background-color: @mt_raised; }

.terminal-search {
  padding: 4px 8px;
  background-color: @mt_surface;
  border-bottom: 1px solid @mt_line;
}
.terminal-search-status { color: @mt_muted; font-size: 0.86em; }
.terminal-search-status.no-match { color: @mt_red; }
.terminal-search-button {
  min-width: 26px;
  min-height: 24px;
  padding: 0 6px;
  color: @mt_muted;
  background: none;
  border: 0;
  border-radius: 4px;
  box-shadow: none;
}
.terminal-search-button:hover, .terminal-search-button:checked { color: @mt_text; background-color: @mt_raised; }
.project-search-snippet { font-family: Monospace; font-size: 0.84em; opacity: 0.8; }

/* Command blocks */
.command-blocks {
  padding: 6px 8px 8px 10px;
  color: @mt_text;
  background-color: @mt_sidebar;
  border-top: 1px solid @mt_line;
}
.command-blocks list { background-color: transparent; }
.command-blocks-title { font-weight: bold; font-size: 0.9em; }
.command-blocks-note { color: @mt_faint; font-size: 0.84em; }
.command-block-row { margin: 1px 0; border-radius: 6px; }
.command-block-header { padding: 4px 6px; background: none; border: 0; box-shadow: none; }
.command-block-header:hover { background-color: @mt_hover; }
.command-block-command { font-family: Monospace; font-size: 0.9em; }
.command-block-meta { color: @mt_faint; font-size: 0.82em; font-feature-settings: "tnum"; }
.command-block-diff { color: @mt_magenta; font-size: 0.82em; }
.command-block-same { color: @mt_green; font-size: 0.82em; }
.command-block-error { color: @mt_red; font-size: 0.82em; font-weight: bold; }
.command-block-output {
  padding: 6px 8px;
  color: @mt_text;
  background-color: @mt_surface;
  border-radius: 4px;
  font-family: Monospace;
  font-size: 0.84em;
}
.command-block-clear { min-height: 0; padding: 1px 6px; color: @mt_muted; background: none; border: 0; box-shadow: none; }
.command-block-clear:hover { color: @mt_text; background-color: @mt_raised; }

/* Empty workspace */
.welcome-title { color: @mt_text; font-size: 1.3em; font-weight: bold; }
.welcome-copy { color: @mt_muted; }
"""


class MainWindow(
    AgentToolsMixin,
    SearchMixin,
    WindowDialogsMixin,
    Gtk.ApplicationWindow,
):
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
        self._git_cache = GitInfoCache(ttl=8.0)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="metadata")
        self._search_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="output-search"
        )
        self._capture_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="command-capture"
        )
        self._snapshot_timer_id: Optional[int] = None
        self._closing = False
        self._last_runtime_warning_key: Optional[str] = None
        self._last_runtime_warning_at = 0.0
        self._status_overrides: dict[str, AgentStatus] = {}
        self._attention_ids: list[str] = []
        self._attention_signature: Optional[tuple[str, ...]] = None
        self._sidebar_stats: Optional[tuple[int, int]] = None
        self.get_style_context().add_class("mujterm-window")
        self.set_default_size(1100, 700)
        self.set_size_request(560, 360)
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
        for view in self.terminal_views.values():
            view._stop_command_poll()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._search_executor.shutdown(wait=False, cancel_futures=True)
        self._capture_executor.shutdown(wait=False, cancel_futures=True)

    def _build_header(self) -> None:
        header = Gtk.HeaderBar(show_close_button=True, title="MujTerm")
        self.header_bar = header
        accelerator = Gtk.AccelGroup()
        self.add_accel_group(accelerator)
        shortcut_mods = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK

        def shortcut(key: int, callback: Callable[[], None]) -> None:
            def activate(*_args: Any) -> bool:
                callback()
                return True

            accelerator.connect(key, shortcut_mods, Gtk.AccelFlags.VISIBLE, activate)

        def icon_button(
            icon: str, tooltip: str, widget: type[Gtk.Button] = Gtk.Button
        ) -> Gtk.Button:
            button = widget()
            button.add(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            button.set_tooltip_text(tooltip)
            return button

        terminal_button = icon_button(
            "list-add-symbolic", "New terminal (Ctrl+Shift+T)"
        )
        terminal_button.connect("clicked", lambda *_args: self.create_terminal_from_active())

        split_button = icon_button("view-dual-symbolic", "Split", Gtk.MenuButton)
        split_menu = Gtk.Menu()
        for label, key, orientation in (
            ("Split Right", Gdk.KEY_Right, Gtk.Orientation.HORIZONTAL),
            ("Split Down", Gdk.KEY_Down, Gtk.Orientation.VERTICAL),
        ):
            item = Gtk.MenuItem(label=label)
            item.get_child().set_accel(key, shortcut_mods)
            item.connect(
                "activate",
                lambda _item, value=orientation: self.split_active(value),
            )
            split_menu.append(item)
        split_menu.show_all()
        split_button.set_popup(split_menu)

        self.toolbox_button = Gtk.MenuButton(label="Commands")
        self._build_toolbox_popover()

        self.header_attention_button = Gtk.Button()
        self.header_attention_button.set_no_show_all(True)
        self.header_attention_button.connect(
            "clicked", lambda *_args: self.select_next_attention()
        )

        overflow_button = icon_button("open-menu-symbolic", "Menu", Gtk.MenuButton)
        overflow = Gtk.Menu()

        def add_item(label: str, callback: Callable[[], None]) -> Gtk.MenuItem:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda *_args: callback())
            overflow.append(item)
            return item

        add_item("Open Project…", self.open_project_dialog)
        add_item("New SSH Project…", self.open_ssh_project_dialog)
        overflow.append(Gtk.SeparatorMenuItem())
        search_item = add_item("Find in Terminal Output", self.show_terminal_search)
        search_item.get_child().set_accel(Gdk.KEY_F, shortcut_mods)
        add_item("Find in Project Output…", self.show_project_search)
        overflow.append(Gtk.SeparatorMenuItem())
        add_item("Codex ↔ Claude Races…", self.show_agent_races)
        overflow.append(Gtk.SeparatorMenuItem())
        self.keyboard_mode_item = Gtk.CheckMenuItem(label="Keyboard Navigation Mode")
        self.keyboard_mode_item.set_tooltip_text("Toggle with Ctrl+Space")
        self.keyboard_mode_item.connect(
            "toggled", lambda item: self._set_keyboard_mode(item.get_active())
        )
        overflow.append(self.keyboard_mode_item)
        add_item("Agent Integrations…", self.show_integration_dialog)
        overflow.append(Gtk.SeparatorMenuItem())
        add_item("About / Diagnostics…", self.show_diagnostics)
        overflow.show_all()
        overflow_button.set_popup(overflow)

        header.pack_start(terminal_button)
        header.pack_start(split_button)
        header.pack_start(self.toolbox_button)
        header.pack_end(overflow_button)
        header.pack_end(self.header_attention_button)
        self.set_titlebar(header)
        shortcut(Gdk.KEY_T, self.create_terminal_from_active)
        shortcut(Gdk.KEY_Right, lambda: self.split_active(Gtk.Orientation.HORIZONTAL))
        shortcut(Gdk.KEY_Down, lambda: self.split_active(Gtk.Orientation.VERTICAL))
        shortcut(Gdk.KEY_F, self.show_terminal_search)
        shortcut(Gdk.KEY_A, self.select_next_attention)

    def _build_toolbox_popover(self) -> None:
        self.toolbox_popover = Gtk.Popover.new(self.toolbox_button)
        self.toolbox_popover.set_position(Gtk.PositionType.BOTTOM)
        self.toolbox_popover.set_size_request(360, -1)
        self.toolbox_popover.connect("show", self._toolbox_popover_shown)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_border_width(10)

        title = Gtk.Label(label="Saved commands", xalign=0)
        title.get_style_context().add_class("toolbox-title")

        self.toolbox_target = Gtk.Label(xalign=0)
        self.toolbox_target.set_ellipsize(Pango.EllipsizeMode.END)
        self.toolbox_target.get_style_context().add_class("toolbox-target")

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(72)
        scrolled.set_max_content_height(320)
        scrolled.set_propagate_natural_height(True)
        self.toolbox_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        scrolled.add(self.toolbox_list)

        self.toolbox_add_button = Gtk.Button(label="Add Command…")
        self.toolbox_add_button.connect(
            "clicked", lambda *_args: self._show_toolbox_editor()
        )

        content.pack_start(title, False, False, 0)
        content.pack_start(self.toolbox_target, False, False, 0)
        content.pack_start(scrolled, True, True, 0)
        content.pack_start(self.toolbox_add_button, False, False, 0)
        # Window.show_all() never reaches popovers, so their content has to be
        # shown explicitly or the popover opens as an empty bubble.
        content.show_all()
        self.toolbox_popover.add(content)
        self.toolbox_button.set_popover(self.toolbox_popover)

    def _toolbox_popover_shown(self, *_args: Any) -> None:
        self._rebuild_toolbox()

    def _rebuild_toolbox(self) -> None:
        for child in self.toolbox_list.get_children():
            self.toolbox_list.remove(child)

        terminal = (
            self.database.get_terminal(self.active_terminal_id)
            if self.active_terminal_id
            else None
        )
        if terminal:
            self.toolbox_target.set_text(
                f"Click a command to type it into {terminal.name}"
            )
            self.toolbox_target.set_tooltip_text(terminal.last_cwd)
        else:
            self.toolbox_target.set_text("Select a terminal to insert commands")
            self.toolbox_target.set_tooltip_text(None)

        commands = self.database.list_toolbox_commands()
        if not commands:
            empty = Gtk.Label(
                label="No saved commands yet. Commands are typed at the prompt "
                "without pressing Enter.",
                xalign=0,
            )
            empty.set_line_wrap(True)
            empty.set_max_width_chars(40)
            empty.get_style_context().add_class("toolbox-empty")
            self.toolbox_list.pack_start(empty, False, False, 0)

        for item in commands:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
            row.get_style_context().add_class("toolbox-row")

            insert = Gtk.Button()
            insert.get_style_context().add_class("toolbox-insert")
            insert.set_relief(Gtk.ReliefStyle.NONE)
            insert.set_sensitive(terminal is not None)
            insert.set_tooltip_text(item.command)
            insert.connect(
                "clicked",
                lambda _button, command=item: self._insert_toolbox_command(command),
            )
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            name = Gtk.Label(label=item.name, xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.get_style_context().add_class("toolbox-name")
            preview = Gtk.Label(label=item.command, xalign=0)
            preview.set_ellipsize(Pango.EllipsizeMode.END)
            preview.set_max_width_chars(42)
            preview.get_style_context().add_class("toolbox-command")
            labels.pack_start(name, False, False, 0)
            labels.pack_start(preview, False, False, 0)
            insert.add(labels)

            edit = Gtk.Button.new_from_icon_name(
                "document-edit-symbolic", Gtk.IconSize.MENU
            )
            edit.set_relief(Gtk.ReliefStyle.NONE)
            edit.set_valign(Gtk.Align.CENTER)
            edit.set_tooltip_text(f"Edit {item.name}")
            edit.connect(
                "clicked",
                lambda _button, command=item: self._show_toolbox_editor(command),
            )
            delete = Gtk.Button.new_from_icon_name(
                "edit-delete-symbolic", Gtk.IconSize.MENU
            )
            delete.set_relief(Gtk.ReliefStyle.NONE)
            delete.set_valign(Gtk.Align.CENTER)
            delete.set_tooltip_text(f"Delete {item.name}")
            delete.connect(
                "clicked",
                lambda _button, command=item: self._delete_toolbox_command(command),
            )

            row.pack_start(insert, True, True, 0)
            row.pack_end(delete, False, False, 0)
            row.pack_end(edit, False, False, 0)
            self.toolbox_list.pack_start(row, False, False, 0)

        self.toolbox_list.show_all()

    def _insert_toolbox_command(self, item: ToolboxCommand) -> None:
        terminal = (
            self.database.get_terminal(self.active_terminal_id)
            if self.active_terminal_id
            else None
        )
        if not terminal:
            self.toolbox_popover.popdown()
            self._error("No active terminal", "Select a terminal before inserting a command.")
            return
        try:
            self.backend.send_text(terminal.tmux_name, item.command)
        except TmuxError as exc:
            record_runtime_error("Command insertion failed", exc)
            self.toolbox_popover.popdown()
            self._error("Could not insert command", str(exc))
            return
        self.toolbox_popover.popdown()
        self._terminal_input(terminal.id)
        view = self.terminal_views.get(terminal.id)
        if view:
            view.terminal.grab_focus()

    def _build_body(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.get_style_context().add_class("mujterm-root")
        self.add(outer)
        self.banner = Gtk.InfoBar()
        self.banner.set_message_type(Gtk.MessageType.INFO)
        self.banner_label = Gtk.Label(xalign=0)
        self.banner_label.set_line_wrap(True)
        self.banner.get_content_area().add(self.banner_label)
        self.banner.add_button("Enable", Gtk.ResponseType.ACCEPT)
        self.banner.add_button("Not now", Gtk.ResponseType.CLOSE)
        self.banner.connect("response", self._banner_response)
        outer.pack_start(self.banner, False, False, 0)
        self.runtime_banner = Gtk.InfoBar()
        self.runtime_banner.set_message_type(Gtk.MessageType.WARNING)
        self.runtime_banner_label = Gtk.Label(xalign=0)
        self.runtime_banner_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.runtime_banner.get_content_area().add(self.runtime_banner_label)
        self.runtime_banner.add_button("Diagnostics", Gtk.ResponseType.APPLY)
        self.runtime_banner.add_button("Dismiss", Gtk.ResponseType.CLOSE)
        self.runtime_banner.connect("response", self._runtime_banner_response)
        self.runtime_banner.set_no_show_all(True)
        self.runtime_banner.hide()
        outer.pack_start(self.runtime_banner, False, False, 0)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer.pack_start(paned, True, True, 0)
        sidebar_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar_shell.set_size_request(220, -1)
        sidebar_shell.get_style_context().add_class("mujterm-sidebar")
        self.attention_revealer = Gtk.Revealer()
        self.attention_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        attention_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        attention_panel.get_style_context().add_class("attention-panel")
        attention_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.attention_title = Gtk.Label(label="Waiting for input", xalign=0)
        self.attention_title.get_style_context().add_class("attention-title")
        self.attention_next = Gtk.Button(label="Next")
        self.attention_next.get_style_context().add_class("attention-next")
        self.attention_next.set_tooltip_text("Jump to the next waiting terminal (Ctrl+Shift+A)")
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
        paned.pack1(sidebar_shell, resize=False, shrink=False)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.stack.get_style_context().add_class("mujterm-workspace")
        self.stack.add_named(self._welcome_widget(), "welcome")
        paned.pack2(self.stack, resize=True, shrink=False)
        paned.set_position(270)

    def _welcome_widget(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        title = Gtk.Label(label="No terminals open")
        title.get_style_context().add_class("welcome-title")
        copy = Gtk.Label(label="Open a project folder to start a terminal in it.")
        copy.set_line_wrap(True)
        copy.set_justify(Gtk.Justification.CENTER)
        copy.get_style_context().add_class("welcome-copy")
        button = Gtk.Button(label="Open Project…")
        button.get_style_context().add_class("suggested-action")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_args: self.open_project_dialog())
        box.pack_start(title, False, False, 0)
        box.pack_start(copy, False, False, 0)
        box.pack_start(button, False, False, 10)
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
                try:
                    self._start_project_connection(terminal)
                except TmuxError as exc:
                    record_runtime_error("SSH session restoration failed", exc)

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
        self._attention_signature = None
        self._sidebar_stats = None
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

    def _start_project_connection(
        self, terminal: TerminalSession
    ) -> Optional[SshConnection]:
        if not terminal.project_id:
            return None
        connection = self.database.get_ssh_connection(terminal.project_id)
        if connection:
            self.backend.send_command(
                terminal.tmux_name, self._ssh_arguments(connection)
            )
        return connection

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
            self._start_project_connection(terminal)
        except TmuxError as exc:
            record_runtime_error("Terminal creation failed", exc)
            self.backend.kill_session(terminal.tmux_name)
            self.database.delete_terminal(terminal.id)
            self._error("Could not create terminal", str(exc))
            return None
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
                self.open_uri,
                self.show_project_search,
                self.report_runtime_error,
                self._capture_executor,
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
        self._set_keyboard_mode(not self.keyboard_mode)

    def _set_keyboard_mode(self, enabled: bool) -> None:
        self.keyboard_mode = enabled
        if self.keyboard_mode_item.get_active() != enabled:
            self.keyboard_mode_item.set_active(enabled)
        self.keyboard_mode_item.set_tooltip_text(
            "h/j/k/l move · [/] workspace · n new · v split right · "
            "s split down · x close · a attention · q exit"
            if enabled
            else "Toggle with Ctrl+Space"
        )

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
                record_runtime_error("Agent integration setup failed", exc)
                self._error("Could not update agent configuration", str(exc))
        else:
            self._set_banner_visible(False)

    def report_runtime_error(
        self, context: str, error: BaseException | str
    ) -> None:
        message = str(error).strip() or type(error).__name__
        key = f"{context}\0{type(error).__name__}\0{message}"
        now = time.monotonic()
        if key != self._last_runtime_warning_key or now - self._last_runtime_warning_at >= 30:
            record_runtime_error(context, error)
            self._last_runtime_warning_key = key
            self._last_runtime_warning_at = now
        self.runtime_banner_label.set_text(
            f"{context}: {message} — details are available in Diagnostics."
        )
        self.runtime_banner.set_no_show_all(False)
        self.runtime_banner.show_all()

    def _runtime_banner_response(
        self, _banner: Gtk.InfoBar, response: int
    ) -> None:
        self.runtime_banner.set_no_show_all(True)
        self.runtime_banner.hide()
        if response == Gtk.ResponseType.APPLY:
            self.show_diagnostics()

    def agent_event_received(self, payload: dict[str, Any]) -> None:
        GLib.idle_add(self._handle_agent_event, payload)

    def _handle_agent_event(self, payload: dict[str, Any]) -> bool:
        terminal_id = payload.get("terminal_id")
        if payload.get("kind") == "shell":
            if isinstance(terminal_id, str):
                view = self.terminal_views.get(terminal_id)
                if view:
                    view.handle_shell_event(payload)
            self._start_snapshot_refresh()
            return False
        if isinstance(terminal_id, str):
            override = self._status_overrides.pop(terminal_id, None)
            if override is not None:
                self._apply_snapshots(changed_ids={terminal_id})
        self._start_snapshot_refresh()
        return False

    def _terminal_input(self, terminal_id: str) -> None:
        snapshot = self.snapshots.get(terminal_id)
        if snapshot and snapshot.status == AgentStatus.NEEDS_ACTION:
            self._status_overrides[terminal_id] = AgentStatus.WORKING
            self._apply_snapshots(changed_ids={terminal_id})

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
                dead=True,
            )
            self._apply_snapshots(changed_ids={terminal_id})

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
        except Exception as exc:
            self.report_runtime_error("Background monitoring failed", exc)
            return False
        previous = self.snapshots
        terminals = {
            terminal.id: terminal for terminal in self.database.list_terminals()
        }
        self.snapshots = snapshots
        cwd_updates = {
            terminal_id: snapshot.cwd
            for terminal_id, snapshot in snapshots.items()
            if terminal_id in terminals
            and snapshot.cwd
            and snapshot.cwd != terminals[terminal_id].last_cwd
        }
        self.database.update_terminal_cwds(cwd_updates)
        self._apply_snapshots(
            changed_ids={
                terminal_id
                for terminal_id in set(previous) | set(snapshots)
                if previous.get(terminal_id) != snapshots.get(terminal_id)
            },
            terminals=terminals,
        )
        return False

    def _effective_snapshot(
        self, terminal_id: str
    ) -> Optional[TerminalSnapshot]:
        snapshot = self.snapshots.get(terminal_id)
        override = self._status_overrides.get(terminal_id)
        if snapshot and override and snapshot.status != override:
            return TerminalSnapshot(
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
                dead=snapshot.dead,
            )
        return snapshot

    def _apply_snapshots(
        self,
        changed_ids: Optional[set[str]] = None,
        terminals: Optional[dict[str, TerminalSession]] = None,
    ) -> None:
        if terminals is None:
            terminals = {
                terminal.id: terminal for terminal in self.database.list_terminals()
            }
        target_ids = set(self.terminal_rows) | set(self.terminal_views)
        if changed_ids is not None:
            target_ids &= changed_ids
        for terminal_id in target_ids:
            snapshot = self._effective_snapshot(terminal_id)
            row = self.terminal_rows.get(terminal_id)
            if row:
                row.update(snapshot, terminal_id == self.active_terminal_id)
            view = self.terminal_views.get(terminal_id)
            terminal = terminals.get(terminal_id)
            if view:
                view.update_snapshot(snapshot, terminal.name if terminal else None)
        if changed_ids is None or changed_ids:
            for section in self.project_sections:
                section.update_summary(self.snapshots)
            self._update_sidebar_stats(len(terminals))
            self._update_attention_queue(terminals)

    def _update_attention_queue(
        self,
        terminals: Optional[dict[str, TerminalSession]] = None,
    ) -> None:
        if terminals is None:
            terminals = {
                terminal.id: terminal for terminal in self.database.list_terminals()
            }
        attention_ids = [
            terminal_id
            for terminal_id in terminals
            if self.snapshots.get(terminal_id)
            and self._status_overrides.get(
                terminal_id, self.snapshots[terminal_id].status
            ) == AgentStatus.NEEDS_ACTION
        ]
        signature = tuple(attention_ids)
        if signature == self._attention_signature:
            self._attention_ids = attention_ids
            return
        self._attention_signature = signature
        self._attention_ids = attention_ids
        for child in self.attention_items.get_children():
            self.attention_items.remove(child)
        self.attention_title.set_text(f"Waiting for input ({len(self._attention_ids)})")
        for terminal_id in self._attention_ids[:5]:
            terminal = terminals[terminal_id]
            project = self.database.get_project(terminal.project_id) if terminal.project_id else None
            prefix = f"{project.name} · " if project else ""
            label = Gtk.Label(label=f"{prefix}{terminal.name}", xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            button = Gtk.Button()
            button.add(label)
            button.set_halign(Gtk.Align.FILL)
            button.get_style_context().add_class("attention-item")
            button.connect(
                "clicked",
                lambda _button, item=terminal_id: self.select_terminal(item),
            )
            self.attention_items.pack_start(button, False, False, 0)
        self.attention_items.show_all()
        self.attention_revealer.set_reveal_child(bool(self._attention_ids))
        count = len(self._attention_ids)
        self.header_attention_button.get_style_context().add_class(
            "attention-button-active"
        )
        if count:
            self.header_attention_button.set_label(f"{count} waiting")
            self.header_attention_button.set_tooltip_text(
                f"Jump to the next of {count} terminals waiting for input "
                "(Ctrl+Shift+A)"
            )
            self.header_attention_button.show()
        else:
            self.header_attention_button.hide()

    def select_next_attention(self) -> None:
        if not getattr(self, "_attention_ids", None):
            return
        try:
            index = self._attention_ids.index(self.active_terminal_id)
        except ValueError:
            index = -1
        self.select_terminal(self._attention_ids[(index + 1) % len(self._attention_ids)])

    def _update_sidebar_stats(self, sessions: Optional[int] = None) -> None:
        if sessions is None:
            sessions = len(self.database.list_terminals())
        agents = sum(1 for snapshot in self.snapshots.values() if snapshot.agent)
        stats = (sessions, agents)
        if stats == self._sidebar_stats:
            return
        self._sidebar_stats = stats
        summary = f"{sessions} terminal{'s' if sessions != 1 else ''}"
        if agents:
            summary += f" · {agents} agent{'s' if agents != 1 else ''}"
        self.header_bar.set_subtitle(summary if sessions else None)

    def _set_banner_visible(self, visible: bool) -> None:
        self.banner.set_no_show_all(not visible)
        if visible:
            self.banner.show_all()
        else:
            self.banner.hide()

    def _delete_event(self, *_args: Any) -> bool:
        self.shutdown()
        return False

    @staticmethod
    def _install_css() -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
