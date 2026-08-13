from __future__ import annotations

import concurrent.futures
import re
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import GLib, Gtk, Pango, Vte  # noqa: E402

from .models import TerminalSession


PCRE2_CASELESS = 0x00000008
PCRE2_UCP = 0x00020000
PCRE2_UTF = 0x00080000


def literal_search_regex(query: str, case_sensitive: bool = False) -> Vte.Regex | None:
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


class SearchMixin:
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


