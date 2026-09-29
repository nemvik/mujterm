from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from mujterm.models import AgentKind, AgentStatus, TerminalSnapshot
from mujterm.status_display import CpuHistory
from mujterm.ui import MainWindow


def _snapshot(
    terminal_id: str,
    status: AgentStatus,
    cpu_percent: float = 0.0,
) -> TerminalSnapshot:
    return TerminalSnapshot(
        terminal_id=terminal_id,
        cwd="/tmp",
        command="claude",
        branch=None,
        git_root=None,
        agent=AgentKind.CLAUDE,
        status=status,
        cpu_percent=cpu_percent,
    )


class HeaderStateButtonTests(unittest.TestCase):
    def _window(
        self,
        snapshots: dict[str, TerminalSnapshot],
        overrides: dict[str, AgentStatus] | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            snapshots=snapshots,
            _status_overrides=overrides or {},
            header_state_buttons={
                key: Mock() for key in ("action", "working", "ready", "failed", "ended")
            },
            _state_ids={},
        )

    def test_buttons_show_counts_and_hide_empty_groups(self) -> None:
        window = self._window(
            {
                "t1": _snapshot("t1", AgentStatus.WORKING),
                "t2": _snapshot("t2", AgentStatus.NEEDS_ACTION),
                "t3": _snapshot("t3", AgentStatus.WORKING),
            }
        )

        MainWindow._update_state_buttons(window, ["t1", "t2", "t3"])

        buttons = window.header_state_buttons
        buttons["action"].set_label.assert_called_once_with("1 needs input")
        buttons["action"].set_visible.assert_called_once_with(True)
        buttons["working"].set_label.assert_called_once_with("◌ 2 working")
        buttons["ready"].set_visible.assert_called_once_with(False)
        buttons["failed"].set_visible.assert_called_once_with(False)
        buttons["ended"].set_visible.assert_called_once_with(False)
        self.assertEqual(window._state_ids["working"], ("t1", "t3"))

    def test_status_overrides_are_counted_instead_of_stale_snapshots(self) -> None:
        window = self._window(
            {"t1": _snapshot("t1", AgentStatus.WORKING)},
            overrides={"t1": AgentStatus.NEEDS_ACTION},
        )

        MainWindow._update_state_buttons(window, ["t1"])

        self.assertEqual(window._state_ids["action"], ("t1",))
        window.header_state_buttons["working"].set_visible.assert_called_once_with(
            False
        )

    def test_closed_terminals_are_not_counted(self) -> None:
        window = self._window({"gone": _snapshot("gone", AgentStatus.READY)})

        MainWindow._update_state_buttons(window, [])

        self.assertEqual(window._state_ids["ready"], ())

    def test_state_button_cycles_through_terminals_in_that_state(self) -> None:
        window = SimpleNamespace(
            _state_ids={"working": ("a", "b"), "ready": ()},
            active_terminal_id="a",
            select_terminal=Mock(),
        )

        MainWindow.select_next_in_state(window, "working")
        window.select_terminal.assert_called_once_with("b")

        window.select_terminal.reset_mock()
        window.active_terminal_id = "b"
        MainWindow.select_next_in_state(window, "working")
        window.select_terminal.assert_called_once_with("a")

        window.select_terminal.reset_mock()
        window.active_terminal_id = "elsewhere"
        MainWindow.select_next_in_state(window, "working")
        window.select_terminal.assert_called_once_with("a")

        window.select_terminal.reset_mock()
        MainWindow.select_next_in_state(window, "ready")
        window.select_terminal.assert_not_called()


class CpuHistoryWiringTests(unittest.TestCase):
    def test_snapshot_cpu_feeds_row_sparklines_and_forgets_closed_terminals(
        self,
    ) -> None:
        history = CpuHistory()
        history.add("gone", 0.0, 50.0)
        row = Mock()
        window = SimpleNamespace(_cpu_history=history, terminal_rows={"t1": row})

        MainWindow._record_cpu_history(
            window, {"t1": _snapshot("t1", AgentStatus.WORKING, 12.0)}, 5.0
        )

        row.update_cpu_history.assert_called_once_with(((5.0, 12.0),), 5.0)
        self.assertEqual(history.samples("gone"), ())


if __name__ == "__main__":
    unittest.main()
