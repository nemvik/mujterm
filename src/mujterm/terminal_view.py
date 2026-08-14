from __future__ import annotations

import concurrent.futures
import difflib
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # noqa: E402

from .impact import (
    GitImpact,
    GitImpactSnapshot,
    capture_git_impact,
    compare_git_impact,
)
from .logging_config import record_runtime_error
from .models import AgentStatus, TerminalSession, TerminalSnapshot
from .search import literal_search_regex
from .tmux_backend import TmuxBackend


# VTE's regex constructor accepts PCRE2 compile flags, which are not exported by
# PyGObject. Keep the subset used by literal, Unicode-aware searches here.
PCRE2_CASELESS = 0x00000008
PCRE2_MULTILINE = 0x00000400
PCRE2_UCP = 0x00020000
PCRE2_UTF = 0x00080000
URL_PATTERN = r"(?:https?://|www\.)[^\s<>\[\]{}\"']+"
VTE_SCROLLBACK_LINES = 10_000
HIDDEN_TERMINAL_SUSPEND_DELAY_MS = 1_500


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
class CommandStartContext:
    baseline_output: str
    started_at: float
    cwd: str
    git_before: Optional[GitImpactSnapshot]
    ports_before: tuple[int, ...]
    branch_before: Optional[str]
    cpu_before: float
    memory_before: int


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
    exact: bool = False
    shell: Optional[str] = None
    cwd: str = ""
    end_cwd: str = ""
    exit_code: Optional[int] = None
    git_before: Optional[GitImpactSnapshot] = None
    git_after: Optional[GitImpactSnapshot] = None
    git_impact: GitImpact = field(default_factory=GitImpact)
    ports_before: tuple[int, ...] = ()
    ports_seen: set[int] = field(default_factory=set)
    opened_ports: tuple[int, ...] = ()
    closed_ports: tuple[int, ...] = ()
    branch_before: Optional[str] = None
    branch_after: Optional[str] = None
    cpu_before: float = 0.0
    peak_cpu: float = 0.0
    memory_before: int = 0
    peak_memory: int = 0
    impact_ready: bool = False

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def duration(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started_at


@dataclass
class _CommandBlockWidgets:
    command: Gtk.Label
    meta: Gtk.Label
    badge: Gtk.Label
    detail: Gtk.Label
    details: Gtk.Revealer
    badge_class: str = ""
    markup: str = ""


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
        on_runtime_error: Optional[Callable[[str, BaseException | str], None]] = None,
        capture_executor: Optional[concurrent.futures.Executor] = None,
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
        self.on_runtime_error = on_runtime_error
        self._selection_drag_active = False
        self._selection_drag_happened = False
        self._selection_scroll_lines = 0
        self._selection_autoscroll_timer_id: Optional[int] = None
        self._selection_clipboard_timer_id: Optional[int] = None
        self.command_blocks: list[SemanticCommandBlock] = []
        self._next_command_block_id = 1
        self._pending_command_block: Optional[SemanticCommandBlock] = None
        self._armed_command_context: Optional[CommandStartContext] = None
        self._command_capture_last = ""
        self._command_capture_changed_at = 0.0
        self._command_poll_timer_id: Optional[int] = None
        self._command_poll_generation = 0
        self._command_capture_future: Optional[
            concurrent.futures.Future[str]
        ] = None
        self._command_finish_futures: set[
            concurrent.futures.Future[
                tuple[str, Optional[GitImpactSnapshot]]
            ]
        ] = set()
        self._owns_capture_executor = capture_executor is None
        self._capture_executor = capture_executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="command-capture",
        )
        self._expanded_command_blocks: set[int] = set()
        self._command_block_widgets: dict[int, _CommandBlockWidgets] = {}
        self._rendered_command_block_ids: Optional[tuple[int, ...]] = None
        self._last_snapshot: Optional[TerminalSnapshot] = None
        self._radar_state = ""
        self._radar_tooltip = ""
        self._hud_agent_state: Optional[tuple[AgentStatus, Optional[str]]] = None
        self._radar_completion_active = False
        self._radar_completion_error = False
        self._radar_completion_timer_id: Optional[int] = None
        self._spawn_cancellable: Optional[Gio.Cancellable] = None
        self._spawn_in_progress = False
        self._child_pid: Optional[int] = None
        self._should_be_attached = True
        self._suspend_requested = False
        self._suspended = False
        self._suspend_timer_id: Optional[int] = None
        self._destroyed = False
        self.connect("map", self._visibility_mapped)
        self.connect("unmap", self._visibility_unmapped)
        self.connect("destroy", self._selection_destroyed)
        self._build_hud()
        self._build_search()
        self.terminal_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_shell.get_style_context().add_class("terminal-shell")
        self.terminal = Vte.Terminal()
        # tmux remains the authoritative 50k-line history. Keeping a smaller
        # VTE copy avoids retaining the same scrollback twice for every view.
        self.terminal.set_scrollback_lines(VTE_SCROLLBACK_LINES)
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
        self.terminal.connect("child-exited", self._child_exited)
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
        note = Gtk.Label(
            label="OSC 133 exact · memory only · never written to disk", xalign=0
        )
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
        visible = button.get_active()
        self.command_blocks_revealer.set_reveal_child(visible)
        if visible:
            self._render_command_blocks()
            block = self._pending_command_block
            if block is not None and block.exact:
                self._start_command_poll()
        else:
            block = self._pending_command_block
            if block is not None and block.exact:
                self._stop_command_poll()

    def _clear_command_blocks(self, *_args: Any) -> None:
        self._stop_command_poll()
        for future in tuple(self._command_finish_futures):
            future.cancel()
        self._command_finish_futures.clear()
        self._pending_command_block = None
        self._armed_command_context = None
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
        count_label = f"BLOCKS {len(self.command_blocks)}"
        if self.command_blocks_button.get_label() != count_label:
            self.command_blocks_button.set_label(count_label)
        if not self.command_blocks_button.get_active():
            return

        block_ids = tuple(block.id for block in reversed(self.command_blocks))
        if block_ids != self._rendered_command_block_ids:
            self._rebuild_command_block_rows(block_ids)

        for block in reversed(self.command_blocks):
            self._update_command_block_row(block)

    def _rebuild_command_block_rows(self, block_ids: tuple[int, ...]) -> None:
        for child in self.command_blocks_list.get_children():
            self.command_blocks_list.remove(child)
        self._command_block_widgets.clear()
        self._rendered_command_block_ids = block_ids
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
            command = Gtk.Label(xalign=0)
            command.set_ellipsize(Pango.EllipsizeMode.END)
            command.get_style_context().add_class("command-block-command")
            meta = Gtk.Label()
            meta.get_style_context().add_class("command-block-meta")
            diff = Gtk.Label()
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
            detail.get_style_context().add_class("command-block-output")
            details.add(detail)
            row.pack_start(details, False, False, 0)
            self.command_blocks_list.add(row)
            self._command_block_widgets[block.id] = _CommandBlockWidgets(
                command=command,
                meta=meta,
                badge=diff,
                detail=detail,
                details=details,
            )
        self.command_blocks_list.show_all()

    def _update_command_block_row(self, block: SemanticCommandBlock) -> None:
        widgets = self._command_block_widgets.get(block.id)
        if widgets is None:
            return
        glyph = "●" if block.running else ("×" if block.exit_code else "✓")
        command = f"{glyph}  {block.command}"
        if widgets.command.get_text() != command:
            widgets.command.set_text(command)
        meta = self._command_block_meta(block)
        if widgets.meta.get_text() != meta:
            widgets.meta.set_text(meta)
        badge = self._command_block_badge(block)
        if widgets.badge.get_text() != badge:
            widgets.badge.set_text(badge)
        badge_class = "command-block-diff"
        if block.exit_code not in (None, 0):
            badge_class = "command-block-error"
        elif (
            block.previous_output is not None
            and not block.added_lines
            and not block.removed_lines
        ):
            badge_class = "command-block-same"
        if widgets.badge_class != badge_class:
            context = widgets.badge.get_style_context()
            for candidate in (
                "command-block-diff",
                "command-block-error",
                "command-block-same",
            ):
                context.remove_class(candidate)
            context.add_class(badge_class)
            widgets.badge_class = badge_class
        markup = self._command_block_markup(block)
        if widgets.markup != markup:
            widgets.detail.set_markup(markup)
            widgets.markup = markup
        widgets.details.set_reveal_child(
            block.id in self._expanded_command_blocks
        )

    @staticmethod
    def _command_block_meta(block: SemanticCommandBlock) -> str:
        duration = block.duration
        source = "OSC" if block.exact else "EST"
        if block.running:
            return f"{source} · RUNNING {duration:.1f}s"
        if block.exit_code is not None:
            return f"{source} · EXIT {block.exit_code} · {duration:.1f}s"
        return f"{source} · {duration:.1f}s"

    @staticmethod
    def _command_block_badge(block: SemanticCommandBlock) -> str:
        if block.running:
            return "LIVE"
        impact_count = len(block.git_impact.changed_files)
        impact = f" · IMPACT {impact_count}" if impact_count else ""
        if block.previous_output is None:
            return f"FIRST RUN{impact}"
        if not block.added_lines and not block.removed_lines:
            return f"NO CHANGE{impact}"
        return f"Δ +{block.added_lines} −{block.removed_lines}{impact}"

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
        rendered = [TerminalView._command_impact_markup(block), ""]
        rendered.append(f'<span foreground="#a5f3fc"><b>{heading}</b></span>')
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

    @staticmethod
    def _command_impact_markup(block: SemanticCommandBlock) -> str:
        rendered = [
            '<span foreground="#f0abfc"><b>IMPACT LENS</b></span>'
        ]
        protocol = f"exact OSC 133 · {block.shell or 'shell'}" if block.exact else "estimated"
        facts = [protocol]
        if block.exit_code is not None:
            facts.append(f"exit {block.exit_code}")
        location = block.end_cwd or block.cwd
        if location:
            facts.append(location)
        rendered.append(
            f'<span foreground="#9ca3b8">{GLib.markup_escape_text(" · ".join(facts))}</span>'
        )
        if block.running or not block.impact_ready:
            rendered.append(
                '<span foreground="#818aa3">measuring Git, services and resources…</span>'
            )
            return "\n".join(rendered)

        impact = block.git_impact
        observations = 0
        if impact.repository_changed:
            before = impact.repository_before or "no repository"
            after = impact.repository_after or "no repository"
            rendered.append(
                '<span foreground="#93c5fd">repository  '
                f'{GLib.markup_escape_text(before)} → {GLib.markup_escape_text(after)}</span>'
            )
            observations += 1
        if impact.branch_changed:
            before = impact.branch_before or "detached/none"
            after = impact.branch_after or "detached/none"
            rendered.append(
                '<span foreground="#93c5fd">branch      '
                f'{GLib.markup_escape_text(before)} → {GLib.markup_escape_text(after)}</span>'
            )
            observations += 1
        if impact.commit_changed:
            rendered.append(
                '<span foreground="#c4b5fd">commit      '
                f'{impact.head_before[:8]} → {impact.head_after[:8]}</span>'
            )
            observations += 1

        file_groups = (
            ("+", "#86efac", impact.created),
            ("~", "#fde68a", impact.modified),
            ("−", "#fda4af", impact.deleted),
            ("✓", "#93c5fd", impact.resolved),
        )
        shown = 0
        for glyph, color, paths in file_groups:
            for path in paths:
                if shown >= 18:
                    break
                rendered.append(
                    f'<span foreground="{color}">{glyph} {GLib.markup_escape_text(path)}</span>'
                )
                shown += 1
            observations += len(paths)
        omitted = len(impact.changed_files) - shown
        if omitted > 0 or impact.truncated:
            suffix = f"{omitted} more" if omitted > 0 else "additional files"
            rendered.append(
                f'<span foreground="#818aa3">… {suffix} omitted …</span>'
            )

        if block.opened_ports:
            ports = " ".join(f":{port}" for port in block.opened_ports)
            rendered.append(
                f'<span foreground="#86efac">ports open  {ports}</span>'
            )
            observations += len(block.opened_ports)
        if block.closed_ports:
            ports = " ".join(f":{port}" for port in block.closed_ports)
            rendered.append(
                f'<span foreground="#fda4af">ports close {ports}</span>'
            )
            observations += len(block.closed_ports)

        resource_parts: list[str] = []
        if block.peak_cpu >= 1.0:
            resource_parts.append(f"peak CPU {block.peak_cpu:.0f}%")
        memory_delta = max(0, block.peak_memory - block.memory_before)
        if memory_delta >= 1024 * 1024:
            resource_parts.append(f"RAM +{memory_delta / (1024 ** 2):.0f} MiB")
        if resource_parts:
            rendered.append(
                f'<span foreground="#f0abfc">resources   {" · ".join(resource_parts)}</span>'
            )
            observations += 1
        if not observations:
            rendered.append(
                '<span foreground="#818aa3">no observable Git, service or resource change</span>'
            )
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
        context = self._capture_command_start_context()
        self._armed_command_context = context
        cursor_context = self.backend.capture_cursor_context(self.session.tmux_name)
        command = extract_prompt_command(cursor_context)
        if not command:
            return
        self._create_command_block(command, context)

    def _capture_command_start_context(self) -> CommandStartContext:
        snapshot = self._last_snapshot
        cwd = snapshot.cwd if snapshot else self.session.last_cwd
        known_root = snapshot.git_root if snapshot else None
        git_before = capture_git_impact(cwd, known_root=known_root)
        ports = (
            tuple(sorted(service.port for service in snapshot.services))
            if snapshot
            else ()
        )
        return CommandStartContext(
            baseline_output=self.backend.capture_recent_output(
                self.session.tmux_name
            ),
            started_at=time.monotonic(),
            cwd=cwd,
            git_before=git_before,
            ports_before=ports,
            branch_before=snapshot.branch if snapshot else None,
            cpu_before=snapshot.cpu_percent if snapshot else 0.0,
            memory_before=snapshot.memory_bytes if snapshot else 0,
        )

    def _create_command_block(
        self,
        command: str,
        context: CommandStartContext,
        *,
        exact: bool = False,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> SemanticCommandBlock:
        block = SemanticCommandBlock(
            id=self._next_command_block_id,
            command=command[:500],
            baseline_output=context.baseline_output,
            started_at=context.started_at,
            exact=exact,
            shell=shell,
            cwd=cwd or context.cwd,
            git_before=context.git_before,
            ports_before=context.ports_before,
            ports_seen=set(context.ports_before),
            branch_before=context.branch_before,
            cpu_before=context.cpu_before,
            peak_cpu=context.cpu_before,
            memory_before=context.memory_before,
            peak_memory=context.memory_before,
        )
        self._next_command_block_id += 1
        self.command_blocks.append(block)
        if len(self.command_blocks) > 20:
            removed = self.command_blocks.pop(0)
            self._expanded_command_blocks.discard(removed.id)
        self._pending_command_block = block
        self._command_capture_last = context.baseline_output
        self._command_capture_changed_at = context.started_at
        if self._should_poll_command(block):
            self._start_command_poll()
        self._render_command_blocks()
        self._apply_quiet_radar()
        return block

    def handle_shell_event(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        if event == "command_start":
            command = payload.get("command")
            if not isinstance(command, str) or not command.strip():
                return
            shell = payload.get("shell")
            cwd = payload.get("cwd")
            self._exact_command_started(
                command.strip(),
                shell if isinstance(shell, str) else None,
                cwd if isinstance(cwd, str) else None,
            )
        elif event == "command_end":
            status = payload.get("status")
            cwd = payload.get("cwd")
            if isinstance(status, int) and 0 <= status <= 255:
                self._exact_command_ended(
                    status, cwd if isinstance(cwd, str) else None
                )

    def _exact_command_started(
        self, command: str, shell: Optional[str], cwd: Optional[str]
    ) -> None:
        block = self._pending_command_block
        if block is not None and block.exact:
            if " ".join(block.command.split()) == " ".join(command.split()):
                return
            self._finish_command_capture()
            block = None
        context = self._armed_command_context
        if context and time.monotonic() - context.started_at > 3.0:
            context = None
        if block is None:
            context = context or self._capture_command_start_context()
            block = self._create_command_block(
                command,
                context,
                exact=True,
                shell=shell,
                cwd=cwd,
            )
        else:
            block.command = command[:500]
            block.exact = True
            block.shell = shell
            block.cwd = cwd or block.cwd
            if self._should_poll_command(block):
                self._start_command_poll()
            else:
                self._stop_command_poll()
            self._render_command_blocks()
        self._armed_command_context = None

    def _exact_command_ended(self, status: int, cwd: Optional[str]) -> None:
        block = self._pending_command_block
        if block is None or not block.exact:
            return
        block.exit_code = status
        block.end_cwd = cwd or block.cwd
        self._finish_command_capture()

    def _command_blocks_visible(self) -> bool:
        button = getattr(self, "command_blocks_button", None)
        return bool(button is not None and button.get_active())

    def _should_poll_command(self, block: SemanticCommandBlock) -> bool:
        return not block.exact or self._command_blocks_visible()

    def _start_command_poll(self) -> None:
        block = self._pending_command_block
        if block is None or not self._should_poll_command(block):
            return
        if self._command_poll_timer_id is None:
            self._command_poll_timer_id = GLib.timeout_add(
                400, self._command_poll_tick
            )

    def _stop_command_poll(self) -> None:
        if self._command_poll_timer_id is not None:
            GLib.source_remove(self._command_poll_timer_id)
            self._command_poll_timer_id = None
        self._command_poll_generation += 1
        future = self._command_capture_future
        self._command_capture_future = None
        if future is not None:
            future.cancel()

    def _command_poll_tick(self) -> bool:
        block = self._pending_command_block
        if block is None or not self._should_poll_command(block):
            self._command_poll_timer_id = None
            return False
        if self._command_capture_future is not None:
            return True
        generation = self._command_poll_generation
        try:
            future = self._capture_executor.submit(
                self.backend.capture_recent_output,
                self.session.tmux_name,
            )
        except RuntimeError as exc:
            self._command_poll_timer_id = None
            self._report_capture_error(exc)
            return False
        self._command_capture_future = future
        future.add_done_callback(
            lambda completed, block_id=block.id, token=generation: GLib.idle_add(
                self._command_poll_done,
                block_id,
                token,
                completed,
            )
        )
        return True

    def _command_poll_done(
        self,
        block_id: int,
        generation: int,
        future: concurrent.futures.Future[str],
    ) -> bool:
        if self._destroyed or generation != self._command_poll_generation:
            return False
        if self._command_capture_future is future:
            self._command_capture_future = None
        try:
            capture = future.result()
        except concurrent.futures.CancelledError:
            return False
        except Exception as exc:
            self._stop_command_poll()
            self._report_capture_error(exc)
            return False
        block = self._pending_command_block
        if block is None or block.id != block_id:
            return False
        now = time.monotonic()
        if capture != self._command_capture_last:
            self._command_capture_last = capture
            self._command_capture_changed_at = now
            output, truncated = terminal_output_delta(block.baseline_output, capture)
            block.output = output
            block.truncated = block.truncated or truncated
            self._render_command_blocks()

        if block.exact:
            return False

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
            self._finish_command_capture(capture)
        return False

    def _report_capture_error(self, error: BaseException) -> None:
        if self.on_runtime_error is not None:
            self.on_runtime_error("Command output capture failed", error)
        else:
            record_runtime_error("Command output capture failed", error)

    def _finish_command_capture(
        self, capture: Optional[str] = None, stop_timer: bool = True
    ) -> None:
        block = self._pending_command_block
        if block is None:
            return
        if stop_timer:
            self._stop_command_poll()
        self._pending_command_block = None
        self._armed_command_context = None
        end_cwd = block.end_cwd or (
            self._last_snapshot.cwd if self._last_snapshot else block.cwd
        )
        block.end_cwd = end_cwd
        try:
            future = self._capture_executor.submit(
                self._collect_command_completion,
                capture,
                end_cwd,
            )
        except RuntimeError as exc:
            self._report_capture_error(exc)
            self._complete_command_capture(block, capture or "", None)
            return
        self._command_finish_futures.add(future)
        future.add_done_callback(
            lambda completed, item=block: GLib.idle_add(
                self._command_finish_done,
                item,
                completed,
            )
        )
        self._apply_quiet_radar()

    def _collect_command_completion(
        self,
        capture: Optional[str],
        end_cwd: str,
    ) -> tuple[str, Optional[GitImpactSnapshot]]:
        if capture is None:
            capture = self.backend.capture_recent_output(self.session.tmux_name)
        return capture, capture_git_impact(end_cwd)

    def _command_finish_done(
        self,
        block: SemanticCommandBlock,
        future: concurrent.futures.Future[
            tuple[str, Optional[GitImpactSnapshot]]
        ],
    ) -> bool:
        self._command_finish_futures.discard(future)
        if self._destroyed:
            return False
        try:
            capture, git_after = future.result()
        except concurrent.futures.CancelledError:
            return False
        except Exception as exc:
            self._report_capture_error(exc)
            capture, git_after = self._command_capture_last, None
        self._complete_command_capture(block, capture, git_after)
        return False

    def _complete_command_capture(
        self,
        block: SemanticCommandBlock,
        capture: str,
        git_after: Optional[GitImpactSnapshot],
    ) -> None:
        output, truncated = terminal_output_delta(block.baseline_output, capture)
        block.output = without_trailing_prompt(output)
        block.truncated = block.truncated or truncated
        block.finished_at = time.monotonic()
        self._finish_command_impact(block, git_after)
        key = " ".join(block.command.split())
        try:
            block_index = self.command_blocks.index(block)
        except ValueError:
            return
        previous = next(
            (
                candidate for candidate in reversed(self.command_blocks[:block_index])
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
        self._render_command_blocks()
        self._show_command_completion_radar(block.exit_code not in (None, 0))

    def _finish_command_impact(
        self,
        block: SemanticCommandBlock,
        git_after: Optional[GitImpactSnapshot] = None,
    ) -> None:
        snapshot = self._last_snapshot
        end_cwd = block.end_cwd or (snapshot.cwd if snapshot else block.cwd)
        block.end_cwd = end_cwd
        block.git_after = git_after
        block.git_impact = compare_git_impact(block.git_before, block.git_after)
        block.branch_after = (
            block.git_after.branch
            if block.git_after
            else (snapshot.branch if snapshot else None)
        )
        current_ports = {
            service.port for service in snapshot.services
        } if snapshot else set()
        block.ports_seen.update(current_ports)
        block.opened_ports = tuple(
            sorted(block.ports_seen - set(block.ports_before))
        )
        block.closed_ports = tuple(
            sorted(set(block.ports_before) - current_ports)
        )
        if snapshot:
            block.peak_cpu = max(block.peak_cpu, snapshot.cpu_percent)
            block.peak_memory = max(block.peak_memory, snapshot.memory_bytes)
        block.impact_ready = True

    def _show_command_completion_radar(self, failed: bool = False) -> None:
        self._radar_completion_active = True
        self._radar_completion_error = failed
        if self._radar_completion_timer_id is not None:
            GLib.source_remove(self._radar_completion_timer_id)
        self._radar_completion_timer_id = GLib.timeout_add(
            1800, self._end_command_completion_radar
        )
        self._apply_quiet_radar()

    def _end_command_completion_radar(self) -> bool:
        self._radar_completion_timer_id = None
        self._radar_completion_active = False
        self._radar_completion_error = False
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
            "Exact OSC 133 command blocks, Ghost Diff and Impact Lens"
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
            state = "error" if self._radar_completion_error else "ready"
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
        tooltip = f"Quiet radar: {detail}"
        if state != self._radar_state:
            self._radar_state = state
            class_name = f"radar-{state}"
            for widget in (self.terminal_shell, self.radar_indicator):
                context = widget.get_style_context()
                for candidate in RADAR_CLASSES:
                    context.remove_class(candidate)
                context.add_class(class_name)
        if tooltip != self._radar_tooltip:
            self._radar_tooltip = tooltip
            self.radar_indicator.set_tooltip_text(tooltip)

    def _observe_command_impact(self, snapshot: TerminalSnapshot) -> None:
        block = self._pending_command_block
        if block is None and self.command_blocks:
            candidate = self.command_blocks[-1]
            if (
                candidate.finished_at is not None
                and time.monotonic() - candidate.finished_at <= 2.0
            ):
                block = candidate
        if block is None:
            return
        before = (
            block.peak_cpu,
            block.peak_memory,
            block.opened_ports,
            block.closed_ports,
        )
        block.peak_cpu = max(block.peak_cpu, snapshot.cpu_percent)
        block.peak_memory = max(block.peak_memory, snapshot.memory_bytes)
        current_ports = {service.port for service in snapshot.services}
        block.ports_seen.update(current_ports)
        if block.finished_at is not None:
            block.opened_ports = tuple(
                sorted(block.ports_seen - set(block.ports_before))
            )
            block.closed_ports = tuple(
                sorted(set(block.ports_before) - current_ports)
            )
            after = (
                block.peak_cpu,
                block.peak_memory,
                block.opened_ports,
                block.closed_ports,
            )
            if after != before:
                self._render_command_blocks()

    def update_snapshot(
        self, snapshot: Optional[TerminalSnapshot], title: Optional[str] = None
    ) -> None:
        previous = self._last_snapshot
        self._last_snapshot = snapshot
        if snapshot and snapshot != previous:
            self._observe_command_impact(snapshot)
        self._apply_quiet_radar()
        if title and self.hud_title.get_text() != title:
            self.hud_title.set_text(title)
        if not snapshot:
            self._update_hud_agent(None)
            return
        path = display_path(snapshot.cwd)
        if self.hud_path.get_text() != path:
            self.hud_path.set_text(path)
        if self.hud_path.get_tooltip_text() != snapshot.cwd:
            self.hud_path.set_tooltip_text(snapshot.cwd)
        branch = f"GIT // {snapshot.branch}" if snapshot.branch else "NO REPOSITORY"
        if self.hud_branch.get_text() != branch:
            self.hud_branch.set_text(branch)
        resources = resource_text(snapshot.cpu_percent, snapshot.memory_bytes)
        if self.hud_resources.get_text() != resources:
            self.hud_resources.set_text(resources)
        ports = " ".join(f":{service.port}" for service in snapshot.services)
        services = f"PORTS // {ports}" if ports else "NO SERVICES"
        if self.hud_services.get_text() != services:
            self.hud_services.set_text(services)
        self._update_hud_agent(snapshot)

    def _update_hud_agent(
        self, snapshot: Optional[TerminalSnapshot]
    ) -> None:
        state = (snapshot.status, snapshot.agent.value if snapshot.agent else None) if snapshot else None
        if state == self._hud_agent_state:
            return
        self._hud_agent_state = state
        if snapshot is None or (
            snapshot.status == AgentStatus.SHELL and not snapshot.agent
        ):
            self.hud_agent.set_no_show_all(True)
            self.hud_agent.hide()
            return
        context = self.hud_agent.get_style_context()
        for class_name in ("status-working", "status-action", "status-ready", "status-error", "status-shell"):
            context.remove_class(class_name)
        agent = snapshot.agent.value.upper() if snapshot.agent else "SHELL"
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
        if (
            self._destroyed
            or not self._should_be_attached
            or self._spawn_in_progress
            or self._child_pid is not None
        ):
            return
        environment = [f"{key}={value}" for key, value in os.environ.items()]
        environment.append("COLORTERM=truecolor")
        argv = self.backend.attach_command(self.session.tmux_name)
        self._spawn_cancellable = Gio.Cancellable()
        self._spawn_in_progress = True
        try:
            self.terminal.spawn_async(
                Vte.PtyFlags.DEFAULT,
                self.session.last_cwd,
                argv,
                environment,
                GLib.SpawnFlags.SEARCH_PATH,
                None,
                None,
                -1,
                self._spawn_cancellable,
                self._spawn_finished,
                None,
            )
        except GLib.Error as error:
            self._spawn_cancellable = None
            self._spawn_in_progress = False
            self._suspended = True
            self._show_spawn_error(error)

    def _spawn_finished(
        self,
        _terminal: Vte.Terminal,
        _child_pid: int,
        error: Optional[GLib.Error],
        _user_data: Any = None,
    ) -> None:
        self._spawn_cancellable = None
        self._spawn_in_progress = False
        if self._destroyed:
            return
        if error is not None:
            cancelled = error.matches(
                Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
            )
            expected_cancel = cancelled and (
                self._suspend_requested or not self._should_be_attached
            )
            self._suspend_requested = False
            self._suspended = True
            if expected_cancel:
                if self._should_be_attached:
                    self._spawn_after_suspend()
                else:
                    self.terminal.reset(True, True)
                return
            self._show_spawn_error(error)
            return
        self._child_pid = _child_pid if _child_pid > 0 else None
        self._suspended = False
        if not self._should_be_attached:
            self._suspend_requested = True
            self._signal_child_for_suspend()
        else:
            # A spawn cancellation can lose a race with a quick remap. In
            # that case the successful child is already the desired client.
            self._suspend_requested = False

    def suspend(self) -> None:
        """Detach the VTE tmux client while leaving its tmux session alive."""
        if self._destroyed:
            return
        self._cancel_suspend_timer()
        self._should_be_attached = False
        if self._child_pid is not None:
            if not self._suspend_requested:
                self._suspend_requested = True
                self._signal_child_for_suspend()
            return
        if self._spawn_in_progress and self._spawn_cancellable is not None:
            self._suspend_requested = True
            self._spawn_cancellable.cancel()
            return
        self._suspended = True

    def resume(self) -> None:
        """Attach VTE again after a hidden terminal becomes visible."""
        if self._destroyed:
            return
        self._cancel_suspend_timer()
        self._should_be_attached = True
        if self._suspend_requested:
            # SIGTERM/cancellation is already in flight. The completion
            # handler will immediately reattach without reporting an exit.
            return
        if self._child_pid is not None or self._spawn_in_progress:
            self._suspended = False
            return
        self._spawn_after_suspend()

    def _spawn_after_suspend(self) -> None:
        if self._destroyed or not self._should_be_attached:
            return
        # tmux redraws the complete current screen after attach, so discard
        # VTE's stale duplicate before starting the fresh client.
        self.terminal.reset(True, True)
        self._suspended = False
        self._spawn()

    def _signal_child_for_suspend(self) -> None:
        if self._child_pid is None:
            self._suspend_requested = False
            self._suspended = True
            if self._should_be_attached:
                self._spawn_after_suspend()
            return
        try:
            os.kill(self._child_pid, signal.SIGTERM)
        except ProcessLookupError:
            # VTE already has the corresponding child-exited notification
            # queued; keep the request set so that notification is ignored.
            return
        except OSError as error:
            self._suspend_requested = False
            self._should_be_attached = True
            if self.on_runtime_error is not None:
                self.on_runtime_error("Terminal suspend failed", error)
            else:
                record_runtime_error("Terminal suspend failed", error)

    def _child_exited(
        self, _terminal: Vte.Terminal, _status: int
    ) -> None:
        expected = self._suspend_requested or not self._should_be_attached
        self._child_pid = None
        if self._destroyed:
            return
        if expected:
            self._suspend_requested = False
            self._suspended = True
            if self._should_be_attached:
                self._spawn_after_suspend()
            else:
                self.terminal.reset(True, True)
            return
        self._suspended = True
        self.on_exit(self.session.id)

    def _visibility_mapped(self, *_args: Any) -> None:
        self.resume()

    def _visibility_unmapped(self, *_args: Any) -> None:
        if self._destroyed or self._suspend_timer_id is not None:
            return
        self._suspend_timer_id = GLib.timeout_add(
            HIDDEN_TERMINAL_SUSPEND_DELAY_MS,
            self._suspend_if_still_hidden,
        )

    def _suspend_if_still_hidden(self) -> bool:
        self._suspend_timer_id = None
        if not self._destroyed and not self.get_mapped():
            self.suspend()
        return False

    def _cancel_suspend_timer(self) -> None:
        if self._suspend_timer_id is not None:
            GLib.source_remove(self._suspend_timer_id)
            self._suspend_timer_id = None

    def _show_spawn_error(self, error: BaseException) -> None:
        if self.on_runtime_error is not None:
            self.on_runtime_error("Terminal attach failed", error)
        else:
            record_runtime_error("Terminal attach failed", error)
        if not self._destroyed:
            self.terminal.feed(
                f"\r\nMujTerm could not attach to tmux: {error}\r\n".encode()
            )

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
        self._destroyed = True
        self._should_be_attached = False
        self._cancel_suspend_timer()
        if self._spawn_cancellable is not None:
            self._spawn_cancellable.cancel()
            self._spawn_cancellable = None
        self._stop_selection_autoscroll()
        if self._selection_clipboard_timer_id is not None:
            GLib.source_remove(self._selection_clipboard_timer_id)
            self._selection_clipboard_timer_id = None
        self._stop_command_poll()
        finish_futures = getattr(self, "_command_finish_futures", set())
        for future in tuple(finish_futures):
            future.cancel()
        finish_futures.clear()
        if getattr(self, "_owns_capture_executor", False):
            self._capture_executor.shutdown(wait=False, cancel_futures=True)
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
