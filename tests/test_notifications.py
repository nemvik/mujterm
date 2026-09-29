from __future__ import annotations

import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import ANY, Mock

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

from mujterm.app import MujTermApplication  # noqa: E402
from mujterm.models import (  # noqa: E402
    AgentKind,
    AgentStatus,
    Project,
    TerminalSession,
    TerminalSnapshot,
)
from mujterm.ui import MainWindow, attention_notification_text  # noqa: E402


def _terminal(terminal_id: str, project_id: str | None = "project-1") -> TerminalSession:
    return TerminalSession(
        id=terminal_id,
        project_id=project_id,
        name=f"Terminal {terminal_id}",
        tmux_name=f"mujterm-{terminal_id}",
        initial_cwd="/tmp",
        last_cwd="/tmp",
        position=0,
    )


def _waiting_snapshot(terminal_id: str) -> TerminalSnapshot:
    return TerminalSnapshot(
        terminal_id=terminal_id,
        cwd="/tmp",
        command="claude",
        branch=None,
        git_root=None,
        agent=AgentKind.CLAUDE,
        status=AgentStatus.NEEDS_ACTION,
    )


class AttentionNotificationTests(unittest.TestCase):
    def _window(self, *, active: bool, baseline: set[str] | None) -> SimpleNamespace:
        application = SimpleNamespace(
            send_notification=Mock(), withdraw_notification=Mock()
        )
        project = Project(
            id="project-1", name="webapp", root_path="/tmp", position=0
        )
        window = SimpleNamespace(
            _attention_notification_baseline=baseline,
            _notified_attention=set(),
            is_active=Mock(return_value=active),
            get_application=Mock(return_value=application),
            snapshots={"t1": _waiting_snapshot("t1")},
            database=SimpleNamespace(get_project=Mock(return_value=project)),
        )
        window._withdraw_attention_notification = MethodType(
            MainWindow._withdraw_attention_notification, window
        )
        return window

    def test_agent_that_starts_waiting_in_background_is_notified(self) -> None:
        window = self._window(active=False, baseline=set())

        MainWindow._sync_attention_notifications(window, ["t1"], {"t1": _terminal("t1")})

        application = window.get_application()
        application.send_notification.assert_called_once_with("attention-t1", ANY)
        self.assertIsInstance(
            application.send_notification.call_args.args[1], Gio.Notification
        )
        self.assertEqual(window._notified_attention, {"t1"})

    def test_no_notification_while_the_window_is_focused(self) -> None:
        window = self._window(active=True, baseline=set())

        MainWindow._sync_attention_notifications(window, ["t1"], {"t1": _terminal("t1")})

        window.get_application().send_notification.assert_not_called()

    def test_agents_already_waiting_at_startup_are_not_announced(self) -> None:
        window = self._window(active=False, baseline=None)

        MainWindow._sync_attention_notifications(window, ["t1"], {"t1": _terminal("t1")})

        window.get_application().send_notification.assert_not_called()
        self.assertEqual(window._attention_notification_baseline, {"t1"})

    def test_baseline_waits_for_the_first_real_snapshot(self) -> None:
        window = self._window(active=False, baseline=None)
        window.snapshots = {}

        MainWindow._sync_attention_notifications(window, [], {})

        self.assertIsNone(window._attention_notification_baseline)

    def test_agent_that_keeps_waiting_is_not_announced_twice(self) -> None:
        window = self._window(active=False, baseline={"t1"})

        MainWindow._sync_attention_notifications(window, ["t1"], {"t1": _terminal("t1")})

        window.get_application().send_notification.assert_not_called()

    def test_notification_is_withdrawn_once_the_agent_stops_waiting(self) -> None:
        window = self._window(active=False, baseline={"t1"})
        window._notified_attention = {"t1"}

        MainWindow._sync_attention_notifications(window, [], {"t1": _terminal("t1")})

        window.get_application().withdraw_notification.assert_called_once_with(
            "attention-t1"
        )
        self.assertEqual(window._notified_attention, set())

    def test_notification_text_names_agent_project_and_terminal(self) -> None:
        self.assertEqual(
            attention_notification_text(AgentKind.CLAUDE, "Terminal 1", "webapp"),
            ("Claude is waiting for input", "webapp · Terminal 1"),
        )
        self.assertEqual(
            attention_notification_text(None, "Terminal 2", None),
            ("An agent is waiting for input", "Terminal 2"),
        )

    def test_notification_click_shows_the_waiting_terminal(self) -> None:
        window = Mock()
        application = SimpleNamespace(window=window)

        MujTermApplication._show_terminal(
            application, None, SimpleNamespace(get_string=lambda: "t1")
        )

        window.present.assert_called_once_with()
        window.select_terminal.assert_called_once_with("t1")


if __name__ == "__main__":
    unittest.main()
