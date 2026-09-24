from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from mujterm.models import (  # noqa: E402
    AgentKind,
    AgentStatus,
    ListeningService,
    TerminalSession,
    TerminalSnapshot,
)
from mujterm.ui import MainWindow, TerminalView  # noqa: E402


# Widgets must never demand more width than a split pane or a small laptop
# screen offers, otherwise GTK refuses to shrink the whole window.
MAX_TERMINAL_VIEW_MIN_WIDTH = 360


@unittest.skipIf(Gdk.Display.get_default() is None, "needs a display")
class LayoutTests(unittest.TestCase):
    def _terminal_view(self) -> TerminalView:
        session = TerminalSession(
            id="terminal-1",
            project_id=None,
            name="A terminal with a rather long descriptive name",
            tmux_name="mujterm-terminal-1",
            initial_cwd="/tmp",
            last_cwd="/tmp",
            position=0,
        )
        with patch.object(TerminalView, "_spawn"):
            view = TerminalView(
                session,
                Mock(),
                Mock(),
                Mock(),
                Mock(),
                Mock(return_value=False),
                Mock(),
                Mock(),
            )
        window = Gtk.OffscreenWindow()
        window.add(view)
        view.show_all()
        self.addCleanup(window.destroy)
        return view

    def test_busy_terminal_header_does_not_force_a_wide_window(self) -> None:
        view = self._terminal_view()
        snapshot = TerminalSnapshot(
            terminal_id="terminal-1",
            cwd="/home/user/projects/some/deeply/nested/repository/path",
            command="claude",
            branch="feature/a-really-long-branch-name-that-keeps-going",
            git_root="/home/user/projects/some/deeply/nested/repository/path",
            agent=AgentKind.CLAUDE,
            status=AgentStatus.NEEDS_ACTION,
            cpu_percent=12.5,
            memory_bytes=512 * 1024 * 1024,
            services=tuple(
                ListeningService(port=port, pid=1)
                for port in (3000, 5173, 8080, 9229)
            ),
        )

        view.update_snapshot(snapshot, "A terminal with a rather long descriptive name")

        minimum, _natural = view.get_preferred_width()
        self.assertLessEqual(minimum, MAX_TERMINAL_VIEW_MIN_WIDTH)

    def test_toolbox_popover_content_is_visible(self) -> None:
        window = SimpleNamespace(
            toolbox_button=Gtk.MenuButton(),
            _toolbox_popover_shown=Mock(),
            _show_toolbox_editor=Mock(),
        )

        MainWindow._build_toolbox_popover(window)

        content = window.toolbox_popover.get_child()
        self.assertIsNotNone(content)
        self.assertTrue(content.get_visible())
        self.assertTrue(window.toolbox_add_button.get_visible())


if __name__ == "__main__":
    unittest.main()
