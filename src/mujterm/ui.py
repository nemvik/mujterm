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
.hud-button.attention-button-active {
  color: #fff7d6;
  background: rgba(253, 230, 138, 0.16);
  border-color: rgba(253, 230, 138, 0.58);
  font-weight: bold;
}
.overflow-button { font-family: Monospace; font-size: 1.15em; font-weight: bold; }
.split-button {
  min-width: 38px;
  font-family: Monospace;
  font-size: 1.1em;
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
.project-ssh {
  color: #bbf7d0;
  background: rgba(134, 239, 172, 0.10);
  border: 1px solid rgba(134, 239, 172, 0.38);
  border-radius: 8px;
  padding: 1px 5px;
  font-family: Monospace;
  font-size: 0.68em;
  font-weight: bold;
}
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
.toolbox-popover { padding: 12px; }
.toolbox-title { color: #f1f5f9; font-family: Monospace; font-weight: bold; }
.toolbox-target { color: #7dd3fc; font-family: Monospace; font-size: 0.72em; }
.toolbox-row {
  padding: 3px;
  background: rgba(20, 34, 52, 0.76);
  border: 1px solid rgba(84, 126, 157, 0.28);
  border-radius: 7px;
}
.toolbox-insert {
  padding: 5px 7px;
  color: #e5edf6;
  background: transparent;
  border: 0;
  box-shadow: none;
}
.toolbox-insert:hover { background: rgba(61, 211, 235, 0.11); }
.toolbox-add {
  padding: 7px 10px;
  color: #061923;
  background: #67e8f9;
  border-color: #67e8f9;
  font-family: Monospace;
  font-weight: bold;
}
.toolbox-add:hover { color: #020617; background: #a5f3fc; border-color: #a5f3fc; }
.toolbox-name { color: #f8fafc; font-weight: bold; }
.toolbox-command { color: #8ca8bf; font-family: Monospace; font-size: 0.76em; }
.toolbox-empty { padding: 18px 8px; color: #7891a8; }
.toolbox-error { color: #fda4af; font-size: 0.82em; }
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
.terminal-search {
  padding: 6px 9px;
  color: #e5e7eb;
  background: #111827;
  border-bottom: 1px solid #374151;
}
.terminal-search-entry { min-width: 120px; }
.terminal-search-status {
  color: #a6a7c5;
  font-family: Monospace;
  font-size: 0.72em;
}
.terminal-search-status.no-match { color: #fda4af; }
.terminal-search-button {
  min-width: 28px;
  min-height: 26px;
  padding: 0 6px;
  color: #d8d5ff;
  background: rgba(196, 181, 253, 0.10);
  border: 1px solid rgba(196, 181, 253, 0.30);
  box-shadow: none;
}
.terminal-search-button:hover { color: #ffffff; border-color: #c4b5fd; }
.project-search-snippet { color: #b6c2d9; font-family: Monospace; font-size: 0.78em; }
.terminal-shell {
  padding: 8px 10px 10px 10px;
  background: #050810;
  border: 1px solid rgba(100, 116, 139, 0.12);
  border-radius: 5px;
}
.terminal-shell.radar-working { border-color: rgba(103, 232, 249, 0.50); box-shadow: inset 0 0 12px rgba(34, 211, 238, 0.08); }
.terminal-shell.radar-attention { border-color: rgba(253, 230, 138, 0.62); box-shadow: inset 0 0 13px rgba(250, 204, 21, 0.09); }
.terminal-shell.radar-ready { border-color: rgba(134, 239, 172, 0.52); box-shadow: inset 0 0 12px rgba(74, 222, 128, 0.08); }
.terminal-shell.radar-error { border-color: rgba(253, 164, 175, 0.68); box-shadow: inset 0 0 13px rgba(251, 113, 133, 0.10); }
.terminal-shell.radar-hot { border-color: rgba(240, 171, 252, 0.68); box-shadow: inset 0 0 14px rgba(232, 121, 249, 0.10); }
.terminal-shell.radar-service { border-color: rgba(147, 197, 253, 0.40); }
.radar-indicator { color: #4b5563; font-family: Monospace; font-size: 0.72em; }
.radar-indicator.radar-working { color: #67e8f9; }
.radar-indicator.radar-attention { color: #fde68a; }
.radar-indicator.radar-ready { color: #86efac; }
.radar-indicator.radar-error { color: #fda4af; }
.radar-indicator.radar-hot { color: #f0abfc; }
.radar-indicator.radar-service { color: #93c5fd; }
.command-block-toggle {
  min-height: 24px;
  padding: 1px 7px;
  color: #c4b5fd;
  background: rgba(196, 181, 253, 0.09);
  border: 1px solid rgba(196, 181, 253, 0.28);
  border-radius: 8px;
  font-family: Monospace;
  font-size: 0.70em;
}
.command-blocks {
  padding: 7px 9px 9px 9px;
  color: #e5e7eb;
  background: #0d1220;
  border-top: 1px solid #353b5a;
}
.command-blocks-title { color: #a5f3fc; font-family: Monospace; font-size: 0.72em; font-weight: bold; }
.command-blocks-note { color: #818aa3; font-size: 0.70em; }
.command-block-row {
  margin: 2px 0;
  padding: 0;
  background: #151a2b;
  border: 1px solid #353b5a;
  border-radius: 7px;
}
.command-block-header { padding: 5px 8px; background: transparent; border: 0; box-shadow: none; }
.command-block-command { color: #f1f5f9; font-family: Monospace; font-size: 0.78em; }
.command-block-meta { color: #8b93aa; font-family: Monospace; font-size: 0.68em; }
.command-block-diff { color: #f0abfc; font-family: Monospace; font-size: 0.70em; font-weight: bold; }
.command-block-same { color: #86efac; font-family: Monospace; font-size: 0.70em; }
.command-block-error { color: #fda4af; font-family: Monospace; font-size: 0.70em; font-weight: bold; }
.command-block-output { padding: 7px 9px; color: #cbd5e1; background: #080b14; font-family: Monospace; font-size: 0.74em; }
.command-block-clear { min-height: 22px; padding: 0 6px; color: #a6a7c5; background: transparent; border: 0; box-shadow: none; }
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
.mujterm-runtime-error { background: #352516; color: #ffe7bd; border-bottom: 1px solid #805b2a; }

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
.project-ssh { color: #bbf7d0; border-color: rgba(134, 239, 172, 0.48); }
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
.toolbox-title, .toolbox-name { color: #ffffff; }
.toolbox-target { color: #a5f3fc; }
.toolbox-row { background: #171c2e; border-color: #414966; }
.toolbox-command { color: #b6c2d9; }
.toolbox-insert:hover { background: rgba(165, 243, 252, 0.12); }
.toolbox-add { color: #111827; background: #a5f3fc; border-color: #a5f3fc; }
.toolbox-add:hover { color: #020617; background: #cffafe; border-color: #cffafe; }
.toolbox-empty { color: #a6a7c5; }
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
.mujterm-runtime-error { background: #3a2830; color: #ffe4e8; border-bottom-color: #936071; }
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
        for view in self.terminal_views.values():
            view._stop_command_poll()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._search_executor.shutdown(wait=False, cancel_futures=True)
        self._capture_executor.shutdown(wait=False, cancel_futures=True)

    def _build_header(self) -> None:
        header = Gtk.HeaderBar(show_close_button=True)
        header.get_style_context().add_class("mujterm-header")
        accelerator = Gtk.AccelGroup()
        self.add_accel_group(accelerator)

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
        self.toolbox_button = Gtk.MenuButton(label="CMD")
        self.toolbox_button.get_style_context().add_class("hud-button")
        self.toolbox_button.set_tooltip_text("Command toolbox")
        self._build_toolbox_popover()

        self.header_attention_button = Gtk.Button(label="ATTN")
        self.header_attention_button.get_style_context().add_class("hud-button")
        self.header_attention_button.set_sensitive(False)
        self.header_attention_button.set_tooltip_text("No agent currently needs attention")
        self.header_attention_button.connect(
            "clicked", lambda *_args: self.select_next_attention()
        )

        overflow_button = Gtk.MenuButton(label="⋯")
        overflow_button.get_style_context().add_class("hud-button")
        overflow_button.get_style_context().add_class("overflow-button")
        overflow_button.set_tooltip_text("More actions")
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
        add_item("Find in Project Output…", self.show_project_search)
        overflow.append(Gtk.SeparatorMenuItem())
        add_item("Project Timeline…", self.show_timeline)
        add_item("Create Agent Handoff…", self.show_handoff)
        add_item("Codex ↔ Claude Races…", self.show_agent_races)
        add_item("Service Dependency Map…", self.show_service_map)
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
        header.pack_start(split_right)
        header.pack_start(split_down)
        header.pack_start(self.toolbox_button)
        header.pack_end(overflow_button)
        header.pack_end(self.header_attention_button)
        self.set_titlebar(header)
        terminal_button.add_accelerator("clicked", accelerator, Gdk.KEY_T, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK, Gtk.AccelFlags.VISIBLE)
        split_right.add_accelerator("clicked", accelerator, Gdk.KEY_Right, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK, Gtk.AccelFlags.VISIBLE)
        split_down.add_accelerator("clicked", accelerator, Gdk.KEY_Down, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK, Gtk.AccelFlags.VISIBLE)
        search_item.add_accelerator("activate", accelerator, Gdk.KEY_F, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK, Gtk.AccelFlags.VISIBLE)

    def _build_toolbox_popover(self) -> None:
        self.toolbox_popover = Gtk.Popover.new(self.toolbox_button)
        self.toolbox_popover.set_position(Gtk.PositionType.BOTTOM)
        self.toolbox_popover.set_size_request(390, -1)
        self.toolbox_popover.connect("show", self._toolbox_popover_shown)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_border_width(12)
        content.get_style_context().add_class("toolbox-popover")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="// COMMAND TOOLBOX", xalign=0)
        title.get_style_context().add_class("toolbox-title")
        header.pack_start(title, True, True, 0)

        self.toolbox_target = Gtk.Label(xalign=0)
        self.toolbox_target.get_style_context().add_class("toolbox-target")

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(72)
        scrolled.set_max_content_height(320)
        scrolled.set_propagate_natural_height(True)
        self.toolbox_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        scrolled.add(self.toolbox_list)

        self.toolbox_add_button = Gtk.Button(label="+ ADD COMMAND")
        self.toolbox_add_button.get_style_context().add_class("toolbox-add")
        self.toolbox_add_button.set_tooltip_text("Save a command in the toolbox")
        self.toolbox_add_button.connect(
            "clicked", lambda *_args: self._show_toolbox_editor()
        )

        content.pack_start(header, False, False, 0)
        content.pack_start(self.toolbox_target, False, False, 0)
        content.pack_start(scrolled, True, True, 0)
        content.pack_start(self.toolbox_add_button, False, False, 0)
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
            self.toolbox_target.set_text(f"TARGET // {terminal.name}")
            self.toolbox_target.set_tooltip_text(terminal.last_cwd)
        else:
            self.toolbox_target.set_text("TARGET // NO ACTIVE TERMINAL")
            self.toolbox_target.set_tooltip_text("Select a terminal to insert commands")

        commands = self.database.list_toolbox_commands()
        if not commands:
            empty = Gtk.Label(label="No saved commands yet.", xalign=0)
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
            edit.get_style_context().add_class("terminal-row-action")
            edit.set_tooltip_text(f"Edit {item.name}")
            edit.connect(
                "clicked",
                lambda _button, command=item: self._show_toolbox_editor(command),
            )
            delete = Gtk.Button.new_from_icon_name(
                "edit-delete-symbolic", Gtk.IconSize.MENU
            )
            delete.get_style_context().add_class("terminal-row-action")
            delete.get_style_context().add_class("terminal-row-close")
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
        self.banner.get_style_context().add_class("mujterm-infobar")
        self.banner.set_message_type(Gtk.MessageType.INFO)
        self.banner_label = Gtk.Label(xalign=0)
        self.banner.get_content_area().add(self.banner_label)
        self.banner.add_button("Enable", Gtk.ResponseType.ACCEPT)
        self.banner.add_button("Not now", Gtk.ResponseType.CLOSE)
        self.banner.connect("response", self._banner_response)
        outer.pack_start(self.banner, False, False, 0)
        self.runtime_banner = Gtk.InfoBar()
        self.runtime_banner.get_style_context().add_class("mujterm-runtime-error")
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
                try:
                    self._start_project_connection(terminal)
                except TmuxError as exc:
                    record_runtime_error("SSH session restoration failed", exc)
                    self._record_event(
                        terminal, "terminal", f"SSH reconnect failed: {exc}"
                    )

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
        connection: Optional[SshConnection] = None
        try:
            self.backend.create_session(terminal)
            connection = self._start_project_connection(terminal)
        except TmuxError as exc:
            record_runtime_error("Terminal creation failed", exc)
            self.backend.kill_session(terminal.tmux_name)
            self.database.delete_terminal(terminal.id)
            self._error("Could not create terminal", str(exc))
            return None
        summary = (
            f"SSH terminal opened for {self._ssh_connection_label(connection)}"
            if connection
            else f"Created in {display_path(start_cwd)}"
        )
        self._record_event(terminal, "terminal", summary)
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
                connected_ports=snapshot.connected_ports,
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
        self._record_snapshot_events(previous, snapshots, terminals)
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
                connected_ports=snapshot.connected_ports,
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
        terminals: Optional[dict[str, TerminalSession]] = None,
    ) -> None:
        status_messages = {
            AgentStatus.WORKING: "Agent started working",
            AgentStatus.NEEDS_ACTION: "Agent needs your action",
            AgentStatus.READY: "Agent finished and is ready",
            AgentStatus.ERROR: "Agent stopped with an error",
            AgentStatus.ENDED: "Terminal process ended",
        }
        if terminals is None:
            terminals = {
                terminal.id: terminal for terminal in self.database.list_terminals()
            }
        for terminal_id, snapshot in current.items():
            before = previous.get(terminal_id)
            terminal = terminals.get(terminal_id)
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
        count = len(self._attention_ids)
        context = self.header_attention_button.get_style_context()
        self.header_attention_button.set_sensitive(bool(count))
        if count:
            context.add_class("attention-button-active")
            self.header_attention_button.set_label(f"! {count}")
            self.header_attention_button.set_tooltip_text(
                f"Jump to the next of {count} terminals needing attention "
                "(Ctrl+Shift+A)"
            )
        else:
            context.remove_class("attention-button-active")
            self.header_attention_button.set_label("ATTN")
            self.header_attention_button.set_tooltip_text(
                "No agent currently needs attention"
            )

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

    @staticmethod
    def _install_css() -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
