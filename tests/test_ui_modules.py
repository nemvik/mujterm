from __future__ import annotations

import unittest

from mujterm.agent_tools import AgentToolsMixin
from mujterm.dialogs import WindowDialogsMixin
from mujterm.search import SearchMixin
from mujterm.terminal_view import TerminalView as ExtractedTerminalView
from mujterm.ui import MainWindow, TerminalView


class UiModuleBoundaryTests(unittest.TestCase):
    def test_main_window_composes_focused_feature_mixins(self) -> None:
        self.assertTrue(issubclass(MainWindow, AgentToolsMixin))
        self.assertTrue(issubclass(MainWindow, SearchMixin))
        self.assertTrue(issubclass(MainWindow, WindowDialogsMixin))

    def test_legacy_terminal_view_import_is_preserved(self) -> None:
        self.assertIs(TerminalView, ExtractedTerminalView)


if __name__ == "__main__":
    unittest.main()
