from __future__ import annotations

import concurrent.futures
import difflib
import os
import re
import signal
import time
from dataclasses import dataclass
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
    SshConnection,
    TerminalSession,
    TerminalSnapshot,
    ToolboxCommand,
)
from .tmux_backend import TmuxBackend, TmuxError
from .worktrees import WorktreeError, compare_worktree, create_race_worktrees


PROJECT_TARGET = Gtk.TargetEntry.new("application/x-mujterm-project", Gtk.TargetFlags.SAME_APP, 1)
TERMINAL_TARGET = Gtk.TargetEntry.new("application/x-mujterm-terminal", Gtk.TargetFlags.SAME_APP, 2)

# VTE's regex constructor accepts PCRE2 compile flags, which are not exported by
# PyGObject.  Keep the small subset used by literal, Unicode-aware searches here.
PCRE2_CASELESS = 0x00000008
PCRE2_MULTILINE = 0x00000400
PCRE2_UCP = 0x00020000
PCRE2_UTF = 0x00080000
URL_PATTERN = r"(?:https?://|www\.)[^\s<>\[\]{}\"']+"


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


def literal_search_regex(query: str, case_sensitive: bool = False) -> Optional[Vte.Regex]:
    if not query:
        return None
    pattern = re.escape(query)
    flags = PCRE2_UTF | PCRE2_UCP
    if not case_sensitive:
        flags |= PCRE2_CASELESS
    return Vte.Regex.new_for_search(pattern, len(pattern.encode("utf-8")), flags)


def output_match_summary(
    output: str,
    query: str,
    preview_limit: int = 2,
    case_sensitive: bool = False,
) -> tuple[int, tuple[str, ...]]:
    """Return a literal match count and short matching lines."""
    if not query:
        return 0, ()
    needle = query if case_sensitive else query.casefold()
    count = 0
    previews: list[str] = []
    for line in output.splitlines():
        haystack = line if case_sensitive else line.casefold()
        line_count = haystack.count(needle)
        if not line_count:
            continue
        count += line_count
        if len(previews) < preview_limit:
            compact = " ".join(line.strip().split())
            previews.append(compact[:180] or "(blank line)")
    return count, tuple(previews)


def normalized_url(value: str) -> str:
    uri = value.strip().rstrip(".,;:!?)]}")
    if uri.startswith("www."):
        return f"https://{uri}"
    return uri


def selection_autoscroll_y(
    pointer_y: float, terminal_height: int, edge_size: float
) -> float:
    """Move an edge drag just outside VTE so its native autoscroll engages."""
    if terminal_height <= 0 or edge_size <= 0:
        return pointer_y
    edge_size = min(edge_size, terminal_height / 4)
    if pointer_y < edge_size:
        return -1.0
    if pointer_y >= terminal_height - edge_size:
        return float(terminal_height)
    return pointer_y


def selection_autoscroll_lines(
    pointer_y: float,
    terminal_height: int,
    edge_size: float,
    max_lines: int = 6,
) -> int:
    """Return signed tmux copy-mode lines for a drag near a viewport edge."""
    if terminal_height <= 0 or edge_size <= 0 or max_lines <= 0:
        return 0
    edge_size = min(edge_size, terminal_height / 4)
    if pointer_y < edge_size:
        distance = edge_size - pointer_y
        speed = min(max_lines, 1 + int((distance / edge_size) * 2))
        return -speed
    if pointer_y >= terminal_height - edge_size:
        distance = pointer_y - (terminal_height - edge_size)
        speed = min(max_lines, 1 + int((distance / edge_size) * 2))
        return speed
    return 0


SHELL_LIKE_COMMANDS = frozenset(
    {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh", "nu", "xonsh", "ssh"}
)
RADAR_CLASSES = (
    "radar-idle",
    "radar-working",
    "radar-attention",
    "radar-ready",
    "radar-error",
    "radar-hot",
    "radar-service",
)


@dataclass
class SemanticCommandBlock:
    id: int
    command: str
    baseline_output: str
    started_at: float
    output: str = ""
    finished_at: Optional[float] = None
    previous_output: Optional[str] = None
    diff_text: str = ""
    added_lines: int = 0
    removed_lines: int = 0
    truncated: bool = False
    saw_process: bool = False

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def duration(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started_at


def shell_like_command(command: str) -> bool:
    return Path(command).name.lower() in SHELL_LIKE_COMMANDS


def extract_prompt_command(cursor_context: str) -> str:
    """Best-effort extraction of a command from the cursor's logical line."""
    lines = cursor_context.replace("\r", "").splitlines()
    if not lines:
        return ""
    line = lines[-1].strip()
    if not line:
        return ""
    match = re.match(r"^.*?[$#%>❯➜]\s+(\S.*)$", line)
    if match:
        return match.group(1).strip()
    if re.search(r"[$#%>❯➜]\s*$", line):
        return ""
    return line


def terminal_output_delta(before: str, after: str) -> tuple[str, bool]:
    """Return content appended after a bounded terminal capture and truncation state."""
    def capture_lines(value: str) -> list[str]:
        lines = [line.rstrip() for line in value.replace("\r", "").splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        return lines

    before_lines = capture_lines(before)
    after_lines = capture_lines(after)
    if not before_lines:
        return "\n".join(after_lines), bool(after_lines)
    for start in range(len(before_lines)):
        suffix = before_lines[start:]
        if len(suffix) <= len(after_lines) and suffix == after_lines[: len(suffix)]:
            return "\n".join(after_lines[len(suffix) :]), start > 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    candidates = [
        block
        for block in matcher.get_matching_blocks()
        if block.size and block.a + block.size == len(before_lines)
    ]
    if candidates:
        overlap = max(candidates, key=lambda block: block.size)
        return "\n".join(after_lines[overlap.b + overlap.size :]), True
    return "\n".join(after_lines), True


def without_trailing_prompt(output: str) -> str:
    lines = [line.rstrip() for line in output.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if lines and re.search(r"[$#%>❯➜]\s*$", lines[-1]):
        lines.pop()
    return "\n".join(lines).strip("\n")


def ghost_diff(previous: str, current: str) -> tuple[str, int, int]:
    previous_lines = previous.splitlines()
    current_lines = current.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            previous_lines,
            current_lines,
            fromfile="previous run",
            tofile="current run",
            n=2,
            lineterm="",
        )
    )
    added = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )
    return ("\n".join(diff_lines) if diff_lines else "No output changes."), added, removed


def quiet_radar_state(
    snapshot: Optional[TerminalSnapshot], command_running: bool = False
) -> str:
    if snapshot is None:
        return "idle"
    if snapshot.status in (AgentStatus.ERROR, AgentStatus.ENDED):
        return "error"
    if snapshot.status == AgentStatus.NEEDS_ACTION:
        return "attention"
    if snapshot.cpu_percent >= 85 or snapshot.memory_bytes >= 1536 * 1024 * 1024:
        return "hot"
    if command_running or snapshot.status in (AgentStatus.WORKING, AgentStatus.UNKNOWN):
        return "working"
    if snapshot.status == AgentStatus.READY:
        return "ready"
    if snapshot.services:
        return "service"
    return "idle"


class TerminalView(Gtk.Box):
    def __init__(
        self,
        session: TerminalSession,
        backend: TmuxBackend,
        on_input: Callable[[str], None],
        on_exit: Callable[[str], None],
        on_focus: Callable[[str], None],
        on_key: Callable[[Gdk.EventKey], bool],
        on_open_uri: Callable[[str], None],
        on_project_search: Callable[[str, bool], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_style_context().add_class("terminal-view")
        self.session = session
        self.backend = backend
        self.on_input = on_input
        self.on_exit = on_exit
        self.on_focus = on_focus
        self.on_key = on_key
        self.on_open_uri = on_open_uri
        self.on_project_search = on_project_search
        self._selection_drag_active = False
        self._selection_drag_happened = False
        self._selection_scroll_lines = 0
        self._selection_autoscroll_timer_id: Optional[int] = None
        self._selection_clipboard_timer_id: Optional[int] = None
        self.command_blocks: list[SemanticCommandBlock] = []
        self._next_command_block_id = 1
        self._pending_command_block: Optional[SemanticCommandBlock] = None
        self._command_capture_last = ""
        self._command_capture_changed_at = 0.0
        self._command_poll_timer_id: Optional[int] = None
        self._expanded_command_blocks: set[int] = set()
        self._last_snapshot: Optional[TerminalSnapshot] = None
        self._radar_state = "idle"
        self._radar_completion_active = False
        self._radar_completion_timer_id: Optional[int] = None
        self.connect("destroy", self._selection_destroyed)
        self._build_hud()
        self._build_search()
        self.terminal_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_shell.get_style_context().add_class("terminal-shell")
        self.terminal = Vte.Terminal()
        self.terminal.set_scrollback_lines(50_000)
        self.terminal.set_scroll_on_output(False)
        self.terminal.set_scroll_on_keystroke(True)
        self.terminal.set_mouse_autohide(True)
        self.terminal.set_allow_hyperlink(True)
        self.terminal.set_font(Pango.FontDescription("Monospace 11"))
        self._apply_terminal_palette()
        self._configure_url_matching()
        self.terminal.connect("event", self._on_pointer_event)
        self.terminal.connect("button-press-event", self._on_button_press)
        self.terminal.connect("key-press-event", self._on_key_press)
        self.terminal.connect("button-press-event", self._on_pointer_input)
        self.terminal.connect("focus-in-event", self._on_focus_in)
        self.terminal.connect("child-exited", lambda *_args: self.on_exit(self.session.id))
        self.terminal_shell.pack_start(self.terminal, True, True, 0)
        self.pack_start(self.terminal_shell, True, True, 0)
        self._build_command_blocks()
        self._spawn()

    def _build_search(self) -> None:
        self.search_revealer = Gtk.Revealer()
        self.search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.search_revealer.set_transition_duration(100)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.get_style_context().add_class("terminal-search")
        bar.connect("size-allocate", self._search_size_allocate)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Find in terminal output")
        self.search_entry.get_style_context().add_class("terminal-search-entry")
        self.search_entry.connect("changed", self._search_changed)
        self.search_entry.connect("key-press-event", self._search_key_press)

        self.search_case = Gtk.ToggleButton(label="Aa")
        self.search_case.get_style_context().add_class("terminal-search-button")
        self.search_case.set_tooltip_text("Match case")
        self.search_case.connect("toggled", self._search_changed)

        previous = Gtk.Button(label="↑")
        previous.get_style_context().add_class("terminal-search-button")
        previous.set_tooltip_text("Previous match (Shift+Enter)")
        previous.connect("clicked", lambda *_args: self._find_search_match(False))
        next_button = Gtk.Button(label="↓")
        next_button.get_style_context().add_class("terminal-search-button")
        next_button.set_tooltip_text("Next match (Enter)")
        next_button.connect("clicked", lambda *_args: self._find_search_match(True))

        self.search_project = Gtk.Button(label="PROJECT")
        self.search_project.get_style_context().add_class("terminal-search-button")
        self.search_project.set_tooltip_text(
            "Search output from every session in this project"
        )
        self.search_project.connect(
            "clicked",
            lambda *_args: self.on_project_search(
                self.search_entry.get_text(), self.search_case.get_active()
            ),
        )

        self.search_status = Gtk.Label(label="", xalign=0)
        self.search_status.get_style_context().add_class("terminal-search-status")

        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.get_style_context().add_class("terminal-search-button")
        close.set_tooltip_text("Close search (Esc)")
        close.connect("clicked", lambda *_args: self.close_search())

        bar.pack_start(self.search_entry, True, True, 0)
        bar.pack_start(self.search_case, False, False, 0)
        bar.pack_start(previous, False, False, 0)
        bar.pack_start(next_button, False, False, 0)
        bar.pack_start(self.search_project, False, False, 0)
        bar.pack_start(self.search_status, False, False, 4)
        bar.pack_end(close, False, False, 0)
        self.search_revealer.add(bar)
        self.pack_start(self.search_revealer, False, False, 0)

    def _search_size_allocate(
        self, _bar: Gtk.Widget, allocation: Gdk.Rectangle
    ) -> None:
        self._set_responsive_visibility(self.search_status, allocation.width >= 620)
        self._set_responsive_visibility(self.search_project, allocation.width >= 480)
        self._set_responsive_visibility(self.search_case, allocation.width >= 360)

    def open_search(self, query: Optional[str] = None) -> None:
        self.search_revealer.set_reveal_child(True)
        self.search_revealer.show_all()
        if query is not None and query != self.search_entry.get_text():
            self.search_entry.set_text(query)
        else:
            self._search_changed()
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def close_search(self) -> None:
        self.terminal.search_set_regex(None, 0)
        self.search_revealer.set_reveal_child(False)
        self.terminal.grab_focus()

    def _search_changed(self, *_args: Any) -> None:
        query = self.search_entry.get_text()
        regex = literal_search_regex(query, self.search_case.get_active())
        self.terminal.search_set_regex(regex, 0)
        self.terminal.search_set_wrap_around(True)
        if not regex:
            self._set_search_status("")
            return
        self._set_search_status("" if self.terminal.search_find_next() else "NO MATCH")

    def _find_search_match(self, forward: bool) -> bool:
        if not self.search_entry.get_text():
            self.search_entry.grab_focus()
            return False
        found = (
            self.terminal.search_find_next()
            if forward
            else self.terminal.search_find_previous()
        )
        self._set_search_status("" if found else "NO MATCH")
        return found

    def _set_search_status(self, text: str) -> None:
        context = self.search_status.get_style_context()
        if text:
            context.add_class("no-match")
        else:
            context.remove_class("no-match")
        self.search_status.set_text(text)

    def _search_key_press(self, _entry: Gtk.Entry, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.close_search()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            backwards = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
            self._find_search_match(not backwards)
            return True
        return False

    def _build_command_blocks(self) -> None:
        self.command_blocks_revealer = Gtk.Revealer()
        self.command_blocks_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_UP
        )
        self.command_blocks_revealer.set_transition_duration(120)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        panel.get_style_context().add_class("command-blocks")
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        title = Gtk.Label(label="COMMAND BLOCKS // EPHEMERAL", xalign=0)
        title.get_style_context().add_class("command-blocks-title")
        note = Gtk.Label(label="memory only · never written to disk", xalign=0)
        note.get_style_context().add_class("command-blocks-note")
        clear = Gtk.Button(label="CLEAR")
        clear.get_style_context().add_class("command-block-clear")
        clear.connect("clicked", self._clear_command_blocks)
        heading.pack_start(title, False, False, 0)
        heading.pack_start(note, True, True, 0)
        heading.pack_end(clear, False, False, 0)
        panel.pack_start(heading, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(105)
        scrolled.set_max_content_height(250)
        scrolled.set_propagate_natural_height(True)
        self.command_blocks_list = Gtk.ListBox()
        self.command_blocks_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.add(self.command_blocks_list)
        panel.pack_start(scrolled, True, True, 0)
        self.command_blocks_revealer.add(panel)
        self.pack_start(self.command_blocks_revealer, False, False, 0)
        self._render_command_blocks()

    def _toggle_command_blocks(self, button: Gtk.ToggleButton) -> None:
        self.command_blocks_revealer.set_reveal_child(button.get_active())
        if button.get_active():
            self._render_command_blocks()

    def _clear_command_blocks(self, *_args: Any) -> None:
        self._stop_command_poll()
        self._pending_command_block = None
        self.command_blocks.clear()
        self._expanded_command_blocks.clear()
        self._render_command_blocks()
        self._apply_quiet_radar()

    def _toggle_command_block(self, block_id: int) -> None:
        if block_id in self._expanded_command_blocks:
            self._expanded_command_blocks.remove(block_id)
        else:
            self._expanded_command_blocks.add(block_id)
        self._render_command_blocks()

    def _render_command_blocks(self) -> None:
        self.command_blocks_button.set_label(f"BLOCKS {len(self.command_blocks)}")
        for child in self.command_blocks_list.get_children():
            self.command_blocks_list.remove(child)
        if not self.command_blocks:
            empty = Gtk.Label(
                label="Run a shell command to create the first semantic block.",
                xalign=0,
            )
            empty.get_style_context().add_class("command-blocks-note")
            empty.set_margin_top(8)
            empty.set_margin_bottom(8)
            self.command_blocks_list.add(empty)
            self.command_blocks_list.show_all()
            return

        for block in reversed(self.command_blocks):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            row.get_style_context().add_class("command-block-row")
            header = Gtk.Button()
            header.get_style_context().add_class("command-block-header")
            header.set_relief(Gtk.ReliefStyle.NONE)
            header.connect(
                "clicked", lambda _button, block_id=block.id: self._toggle_command_block(block_id)
            )
            summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            command = Gtk.Label(
                label=f"{'●' if block.running else '✓'}  {block.command}", xalign=0
            )
            command.set_ellipsize(Pango.EllipsizeMode.END)
            command.get_style_context().add_class("command-block-command")
            meta = Gtk.Label(label=self._command_block_meta(block))
            meta.get_style_context().add_class("command-block-meta")
            diff = Gtk.Label(label=self._command_block_badge(block))
            diff.get_style_context().add_class(
                "command-block-same"
                if block.previous_output is not None
                and not block.added_lines
                and not block.removed_lines
                else "command-block-diff"
            )
            summary.pack_start(command, True, True, 0)
            summary.pack_end(diff, False, False, 0)
            summary.pack_end(meta, False, False, 0)
            header.add(summary)
            row.pack_start(header, False, False, 0)

            details = Gtk.Revealer()
            details.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
            details.set_transition_duration(90)
            detail = Gtk.Label(xalign=0, yalign=0)
            detail.set_selectable(True)
            detail.set_line_wrap(True)
            detail.set_line_wrap_mode(Pango.WrapMode.CHAR)
            detail.set_max_width_chars(160)
            detail.set_markup(self._command_block_markup(block))
            detail.get_style_context().add_class("command-block-output")
            details.add(detail)
            details.set_reveal_child(block.id in self._expanded_command_blocks)
            row.pack_start(details, False, False, 0)
            self.command_blocks_list.add(row)
        self.command_blocks_list.show_all()

    @staticmethod
    def _command_block_meta(block: SemanticCommandBlock) -> str:
        duration = block.duration
        if block.running:
            return f"RUNNING {duration:.1f}s"
        return f"{duration:.1f}s"

    @staticmethod
    def _command_block_badge(block: SemanticCommandBlock) -> str:
        if block.running:
            return "LIVE"
        if block.previous_output is None:
            return "FIRST RUN"
        if not block.added_lines and not block.removed_lines:
            return "NO CHANGE"
        return f"Δ +{block.added_lines} −{block.removed_lines}"

    @staticmethod
    def _command_block_markup(block: SemanticCommandBlock) -> str:
        if block.running:
            heading = "LIVE OUTPUT"
            content = block.output or "(waiting for output)"
        elif block.previous_output is not None:
            heading = "GHOST DIFF // PREVIOUS → CURRENT"
            content = block.diff_text
        else:
            heading = "OUTPUT"
            content = block.output or "(no output)"
        lines = content.splitlines()
        clipped = len(lines) > 160
        if clipped:
            lines = lines[-160:]
        rendered = [f'<span foreground="#a5f3fc"><b>{heading}</b></span>']
        if block.truncated or clipped:
            rendered.append(
                '<span foreground="#818aa3">… earlier output omitted …</span>'
            )
        for line in lines:
            escaped = GLib.markup_escape_text(line)
            if line.startswith(("---", "+++", "@@")):
                rendered.append(f'<span foreground="#93c5fd">{escaped}</span>')
            elif line.startswith("+"):
                rendered.append(f'<span foreground="#86efac">{escaped}</span>')
            elif line.startswith("-"):
                rendered.append(f'<span foreground="#fda4af">{escaped}</span>')
            else:
                rendered.append(escaped or " ")
        return "\n".join(rendered)

    def _can_capture_command(self) -> bool:
        snapshot = self._last_snapshot
        return bool(
            snapshot
            and not snapshot.dead
            and snapshot.agent is None
            and shell_like_command(snapshot.command)
        )

    def _begin_command_capture(self) -> None:
        if not self._can_capture_command():
            return
        if self._pending_command_block is not None:
            self._finish_command_capture(
                self.backend.capture_recent_output(self.session.tmux_name)
            )
        cursor_context = self.backend.capture_cursor_context(self.session.tmux_name)
        command = extract_prompt_command(cursor_context)
        if not command:
            return
        baseline = self.backend.capture_recent_output(self.session.tmux_name)
        now = time.monotonic()
        block = SemanticCommandBlock(
            id=self._next_command_block_id,
            command=command[:500],
            baseline_output=baseline,
            started_at=now,
        )
        self._next_command_block_id += 1
        self.command_blocks.append(block)
        if len(self.command_blocks) > 20:
            removed = self.command_blocks.pop(0)
            self._expanded_command_blocks.discard(removed.id)
        self._pending_command_block = block
        self._command_capture_last = baseline
        self._command_capture_changed_at = now
        self._start_command_poll()
        self._render_command_blocks()
        self._apply_quiet_radar()

    def _start_command_poll(self) -> None:
        if self._command_poll_timer_id is None:
            self._command_poll_timer_id = GLib.timeout_add(
                400, self._command_poll_tick
            )

    def _stop_command_poll(self) -> None:
        if self._command_poll_timer_id is not None:
            GLib.source_remove(self._command_poll_timer_id)
            self._command_poll_timer_id = None

    def _command_poll_tick(self) -> bool:
        block = self._pending_command_block
        if block is None:
            self._command_poll_timer_id = None
            return False
        now = time.monotonic()
        capture = self.backend.capture_recent_output(self.session.tmux_name)
        if capture != self._command_capture_last:
            self._command_capture_last = capture
            self._command_capture_changed_at = now
            output, truncated = terminal_output_delta(block.baseline_output, capture)
            block.output = output
            block.truncated = block.truncated or truncated
            self._render_command_blocks()

        snapshot = self._last_snapshot
        shell_idle = bool(
            snapshot
            and snapshot.agent is None
            and shell_like_command(snapshot.command)
        )
        if not shell_idle:
            block.saw_process = True
        stable_for = now - self._command_capture_changed_at
        elapsed = now - block.started_at
        if shell_idle and (
            (block.saw_process and stable_for >= 0.2)
            or (elapsed >= 1.2 and stable_for >= 0.55)
        ):
            self._command_poll_timer_id = None
            self._finish_command_capture(capture, stop_timer=False)
            return False
        return True

    def _finish_command_capture(
        self, capture: Optional[str] = None, stop_timer: bool = True
    ) -> None:
        block = self._pending_command_block
        if block is None:
            return
        if stop_timer:
            self._stop_command_poll()
        capture = (
            capture
            if capture is not None
            else self.backend.capture_recent_output(self.session.tmux_name)
        )
        output, truncated = terminal_output_delta(block.baseline_output, capture)
        block.output = without_trailing_prompt(output)
        block.truncated = block.truncated or truncated
        block.finished_at = time.monotonic()
        key = " ".join(block.command.split())
        previous = next(
            (
                candidate
                for candidate in reversed(self.command_blocks[:-1])
                if candidate.finished_at is not None
                and " ".join(candidate.command.split()) == key
            ),
            None,
        )
        if previous is not None:
            block.previous_output = previous.output
            (
                block.diff_text,
                block.added_lines,
                block.removed_lines,
            ) = ghost_diff(previous.output, block.output)
        self._pending_command_block = None
        self._render_command_blocks()
        self._show_command_completion_radar()

    def _show_command_completion_radar(self) -> None:
        self._radar_completion_active = True
        if self._radar_completion_timer_id is not None:
            GLib.source_remove(self._radar_completion_timer_id)
        self._radar_completion_timer_id = GLib.timeout_add(
            1800, self._end_command_completion_radar
        )
        self._apply_quiet_radar()

    def _end_command_completion_radar(self) -> bool:
        self._radar_completion_timer_id = None
        self._radar_completion_active = False
        self._apply_quiet_radar()
        return False

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
        self.command_blocks_button = Gtk.ToggleButton(label="BLOCKS 0")
        self.command_blocks_button.get_style_context().add_class(
            "command-block-toggle"
        )
        self.command_blocks_button.set_tooltip_text(
            "Ephemeral command blocks and changes from the previous run"
        )
        self.command_blocks_button.connect("toggled", self._toggle_command_blocks)
        self.radar_indicator = Gtk.Label(label="●")
        self.radar_indicator.get_style_context().add_class("radar-indicator")
        self.radar_indicator.set_tooltip_text("Quiet radar: idle")
        hud.pack_start(identity, True, True, 0)
        hud.pack_end(self.radar_indicator, False, False, 0)
        hud.pack_end(self.command_blocks_button, False, False, 0)
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

    def _apply_quiet_radar(self) -> None:
        state = quiet_radar_state(
            self._last_snapshot, self._pending_command_block is not None
        )
        if self._radar_completion_active and state in ("idle", "service"):
            state = "ready"
        self._radar_state = state
        class_name = f"radar-{state}"
        for widget in (self.terminal_shell, self.radar_indicator):
            context = widget.get_style_context()
            for candidate in RADAR_CLASSES:
                context.remove_class(candidate)
            context.add_class(class_name)
        descriptions = {
            "idle": "idle",
            "working": "command or agent running",
            "attention": "agent needs input",
            "ready": "command or agent completed",
            "error": "agent or terminal ended with an error",
            "hot": "high CPU or memory pressure",
            "service": "local service is listening",
        }
        detail = descriptions[state]
        snapshot = self._last_snapshot
        if state == "hot" and snapshot:
            detail += (
                f" · CPU {snapshot.cpu_percent:.0f}%"
                f" · RAM {snapshot.memory_bytes / (1024 ** 2):.0f} MiB"
            )
        elif state == "service" and snapshot:
            ports = ", ".join(str(service.port) for service in snapshot.services)
            detail += f" · {ports}"
        self.radar_indicator.set_tooltip_text(f"Quiet radar: {detail}")

    def update_snapshot(self, snapshot: Optional[TerminalSnapshot], title: Optional[str] = None) -> None:
        self._last_snapshot = snapshot
        self._apply_quiet_radar()
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

    def _configure_url_matching(self) -> None:
        flags = PCRE2_UTF | PCRE2_UCP | PCRE2_CASELESS | PCRE2_MULTILINE
        self._url_regex = Vte.Regex.new_for_match(
            URL_PATTERN, len(URL_PATTERN.encode("utf-8")), flags
        )
        self._url_match_tag = self.terminal.match_add_regex(self._url_regex, 0)
        self.terminal.match_set_cursor_name(self._url_match_tag, "pointer")

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
        if control and shift and event.keyval in (Gdk.KEY_F, Gdk.KEY_f):
            self.open_search()
            return True
        if control and shift and event.keyval in (Gdk.KEY_C, Gdk.KEY_c):
            self.copy_selection()
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
        if (
            event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
            and not control
            and not bool(event.state & Gdk.ModifierType.MOD1_MASK)
        ):
            self._begin_command_capture()
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
        """Route plain drags through tmux history and Shift-drags through VTE."""
        has_button, button = event.get_button()
        has_state, state = event.get_state()
        is_left_button = has_button and button == 1
        is_left_drag = has_state and bool(state & Gdk.ModifierType.BUTTON1_MASK)
        native_selection = has_state and bool(state & Gdk.ModifierType.SHIFT_MASK)

        if is_left_button and event.type == Gdk.EventType.BUTTON_PRESS:
            self._stop_selection_autoscroll()
            self._selection_drag_active = not native_selection
            self._selection_drag_happened = False
        elif is_left_button and event.type == Gdk.EventType.BUTTON_RELEASE:
            should_copy = self._selection_drag_active and self._selection_drag_happened
            self._selection_drag_active = False
            self._selection_drag_happened = False
            self._stop_selection_autoscroll()
            if should_copy:
                self._schedule_tmux_clipboard_sync()

        if is_left_drag and event.type == Gdk.EventType.MOTION_NOTIFY:
            has_coords, _pointer_x, pointer_y = event.get_coords()
            if has_coords and _widget is not None:
                terminal_height = _widget.get_allocated_height()
                edge_size = max(6.0, min(12.0, _widget.get_char_height() / 2))
                if native_selection:
                    event.motion.y = selection_autoscroll_y(
                        pointer_y, terminal_height, edge_size
                    )
                elif self._selection_drag_active:
                    self._selection_drag_happened = True
                    self._selection_scroll_lines = selection_autoscroll_lines(
                        pointer_y, terminal_height, edge_size
                    )
                    if self._selection_scroll_lines:
                        self._start_selection_autoscroll()
                    else:
                        self._stop_selection_autoscroll()
        return False

    def _start_selection_autoscroll(self) -> None:
        if self._selection_autoscroll_timer_id is None:
            self._selection_autoscroll_timer_id = GLib.timeout_add(
                80, self._selection_autoscroll_tick
            )

    def _stop_selection_autoscroll(self) -> None:
        if self._selection_autoscroll_timer_id is not None:
            GLib.source_remove(self._selection_autoscroll_timer_id)
            self._selection_autoscroll_timer_id = None
        self._selection_scroll_lines = 0

    def _selection_autoscroll_tick(self) -> bool:
        if not self._selection_drag_active or not self._selection_scroll_lines:
            self._selection_autoscroll_timer_id = None
            return False
        self.backend.scroll_selection(
            self.session.tmux_name, self._selection_scroll_lines
        )
        return True

    def _schedule_tmux_clipboard_sync(self) -> None:
        if self._selection_clipboard_timer_id is not None:
            GLib.source_remove(self._selection_clipboard_timer_id)
        self._selection_clipboard_timer_id = GLib.timeout_add(
            80, self._sync_tmux_selection_clipboard
        )

    def _sync_tmux_selection_clipboard(self) -> bool:
        self._selection_clipboard_timer_id = None
        value = self.backend.capture_buffer()
        if value is not None:
            self._copy_text(value)
        return False

    def _selection_destroyed(self, *_args: Any) -> None:
        self._stop_selection_autoscroll()
        if self._selection_clipboard_timer_id is not None:
            GLib.source_remove(self._selection_clipboard_timer_id)
            self._selection_clipboard_timer_id = None
        self._stop_command_poll()
        if self._radar_completion_timer_id is not None:
            GLib.source_remove(self._radar_completion_timer_id)
            self._radar_completion_timer_id = None

    def copy_selection(self) -> None:
        if self.terminal.get_has_selection():
            self.terminal.copy_clipboard_format(Vte.Format.TEXT)
            return
        value = self.backend.capture_buffer()
        if value is not None:
            self._copy_text(value)

    def _on_button_press(
        self, _widget: Gtk.Widget, event: Gdk.EventButton
    ) -> bool:
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        uri = self._uri_at_event(event)
        if uri:
            open_link = Gtk.MenuItem(label="Open Link")
            open_link.connect("activate", lambda *_args: self.on_open_uri(uri))
            copy_link = Gtk.MenuItem(label="Copy Link")
            copy_link.connect("activate", lambda *_args: self._copy_text(uri))
            menu.append(open_link)
            menu.append(copy_link)
            menu.append(Gtk.SeparatorMenuItem())
        copy_item = Gtk.MenuItem(label="Copy")
        copy_item.connect("activate", lambda *_args: self.copy_selection())
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

    def _uri_at_event(self, event: Gdk.EventButton) -> Optional[str]:
        uri = self.terminal.hyperlink_check_event(event)
        if not uri:
            matched = self.terminal.match_check_event(event)
            if isinstance(matched, tuple):
                value, tag = matched
                if tag == self._url_match_tag:
                    uri = value
            elif isinstance(matched, str):
                uri = matched
        return normalized_url(uri) if uri else None

    @staticmethod
    def _copy_text(value: str) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(value, -1)
        clipboard.store()

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
        if ssh_badge:
            header_box.pack_start(ssh_badge, False, False, 0)
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
        self._search_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="output-search"
        )
        self._snapshot_timer_id: Optional[int] = None
        self._closing = False
        self._status_overrides: dict[str, AgentStatus] = {}
        self._attention_ids: list[str] = []
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
        self._search_executor.shutdown(wait=False, cancel_futures=True)

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

    def show_terminal_search(self) -> None:
        if not self.active_terminal_id:
            self._error("No active terminal", "Select a terminal before searching output.")
            return
        terminal = self.database.get_terminal(self.active_terminal_id)
        if terminal:
            self._ensure_terminal_view(terminal).open_search()

    def show_project_search(
        self, initial_query: str = "", initial_case_sensitive: bool = False
    ) -> None:
        active = (
            self.database.get_terminal(self.active_terminal_id)
            if self.active_terminal_id
            else None
        )
        if not active:
            self._error("No active terminal", "Select a terminal before searching output.")
            return
        project = (
            self.database.get_project(active.project_id) if active.project_id else None
        )
        terminals = (
            [
                terminal
                for terminal in self.database.list_terminals(project.id)
                if terminal.project_id == project.id
            ]
            if project
            else self.database.list_ungrouped_terminals()
        )
        scope_name = project.name if project else "Ungrouped"
        dialog = Gtk.Dialog(
            title=f"Search output — {scope_name}", transient_for=self, modal=True
        )
        dialog.set_default_size(680, 500)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        content = dialog.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)

        search_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search = Gtk.SearchEntry()
        search.set_placeholder_text("Search every terminal session in this project")
        match_case = Gtk.ToggleButton(label="Aa")
        match_case.get_style_context().add_class("terminal-search-button")
        match_case.set_tooltip_text("Match case")
        search_controls.pack_start(search, True, True, 0)
        search_controls.pack_start(match_case, False, False, 0)
        content.pack_start(search_controls, False, False, 0)

        progress = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        spinner = Gtk.Spinner()
        status = Gtk.Label(label="Type to search captured tmux history.", xalign=0)
        status.get_style_context().add_class("terminal-search-status")
        progress.pack_start(spinner, False, False, 0)
        progress.pack_start(status, True, True, 0)
        content.pack_start(progress, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        results = Gtk.ListBox()
        results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        results.set_activate_on_single_click(True)
        scrolled.add(results)
        content.pack_start(scrolled, True, True, 0)

        state: dict[str, Any] = {
            "closed": False,
            "generation": 0,
            "timeout": None,
        }
        row_terminals: dict[Gtk.ListBoxRow, str] = {}
        selected: dict[str, Any] = {}

        def clear_results() -> None:
            row_terminals.clear()
            for child in results.get_children():
                results.remove(child)

        def apply_results(
            generation: int,
            query: str,
            future: concurrent.futures.Future[
                list[tuple[TerminalSession, int, tuple[str, ...]]]
            ],
        ) -> bool:
            if state["closed"] or generation != state["generation"]:
                return False
            spinner.stop()
            spinner.hide()
            clear_results()
            try:
                matches = future.result()
            except Exception as exc:
                status.set_text(f"Search failed: {exc}")
                return False
            total = sum(count for _terminal, count, _previews in matches)
            status.set_text(
                f"{total} matches in {len(matches)} of {len(terminals)} sessions"
                if total
                else f'No matches for "{query}" in {len(terminals)} sessions'
            )
            for terminal, count, previews in matches:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                box.set_border_width(8)
                title = Gtk.Label(
                    label=f"{terminal.name}  ·  {count} matches", xalign=0
                )
                title.set_ellipsize(Pango.EllipsizeMode.END)
                box.pack_start(title, False, False, 0)
                for preview in previews:
                    snippet = Gtk.Label(label=preview, xalign=0)
                    snippet.set_ellipsize(Pango.EllipsizeMode.END)
                    snippet.get_style_context().add_class("project-search-snippet")
                    box.pack_start(snippet, False, False, 0)
                row.add(box)
                row_terminals[row] = terminal.id
                results.add(row)
            results.show_all()
            return False

        def run_search(
            generation: int, query: str, case_sensitive: bool
        ) -> bool:
            state["timeout"] = None
            if state["closed"] or generation != state["generation"]:
                return False
            spinner.show()
            spinner.start()
            status.set_text(f"Searching {len(terminals)} sessions…")
            future = self._search_executor.submit(
                self._collect_project_search, terminals, query, case_sensitive
            )
            future.add_done_callback(
                lambda completed: GLib.idle_add(
                    apply_results, generation, query, completed
                )
            )
            return False

        def schedule_search(*_args: Any) -> None:
            state["generation"] += 1
            timeout_id = state["timeout"]
            if timeout_id is not None:
                GLib.source_remove(timeout_id)
                state["timeout"] = None
            query = search.get_text()
            if not query:
                spinner.stop()
                spinner.hide()
                clear_results()
                status.set_text("Type to search captured tmux history.")
                return
            state["timeout"] = GLib.timeout_add(
                180,
                run_search,
                state["generation"],
                query,
                match_case.get_active(),
            )

        def result_activated(_list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
            terminal_id = row_terminals.get(row)
            if terminal_id:
                selected["terminal_id"] = terminal_id
                selected["query"] = search.get_text()
                selected["case_sensitive"] = match_case.get_active()
                dialog.response(Gtk.ResponseType.OK)

        search.connect("changed", schedule_search)
        match_case.connect("toggled", schedule_search)
        results.connect("row-activated", result_activated)
        dialog.show_all()
        spinner.hide()
        match_case.set_active(initial_case_sensitive)
        if initial_query:
            search.set_text(initial_query)
        search.grab_focus()
        search.select_region(0, -1)
        dialog.run()
        state["closed"] = True
        timeout_id = state["timeout"]
        if timeout_id is not None:
            GLib.source_remove(timeout_id)
        dialog.destroy()

        terminal_id = selected.get("terminal_id")
        if terminal_id:
            self.select_terminal(terminal_id)
            terminal = self.database.get_terminal(terminal_id)
            if terminal:
                view = self._ensure_terminal_view(terminal)
                view.search_case.set_active(selected["case_sensitive"])
                view.open_search(selected["query"])

    def _collect_project_search(
        self,
        terminals: list[TerminalSession],
        query: str,
        case_sensitive: bool = False,
    ) -> list[tuple[TerminalSession, int, tuple[str, ...]]]:
        matches: list[tuple[TerminalSession, int, tuple[str, ...]]] = []
        for terminal in terminals:
            output = self.backend.capture_output(terminal.tmux_name)
            count, previews = output_match_summary(
                output, query, case_sensitive=case_sensitive
            )
            if count:
                matches.append((terminal, count, previews))
        return matches

    def open_uri(self, uri: str) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
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
            self._start_project_connection(terminal)
        except TmuxError as exc:
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
