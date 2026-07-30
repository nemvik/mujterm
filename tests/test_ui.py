from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk

from mujterm.models import SshConnection, TerminalSession, ToolboxCommand
from mujterm.ui import MainWindow, TerminalView


class TerminalViewTests(unittest.TestCase):
    def test_left_mouse_gesture_forces_native_vte_selection(self) -> None:
        events = []

        press = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
        press.button.button = 1
        events.append(press)

        motion = Gdk.Event.new(Gdk.EventType.MOTION_NOTIFY)
        motion.state = Gdk.ModifierType.BUTTON1_MASK
        events.append(motion)

        release = Gdk.Event.new(Gdk.EventType.BUTTON_RELEASE)
        release.button.button = 1
        events.append(release)

        for event in events:
            handled = TerminalView._on_pointer_event(None, None, event)
            self.assertFalse(handled)
            self.assertTrue(event.get_state()[1] & Gdk.ModifierType.SHIFT_MASK)

    def test_other_mouse_buttons_are_left_untouched(self) -> None:
        event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
        event.button.button = 3

        handled = TerminalView._on_pointer_event(None, None, event)

        self.assertFalse(handled)
        self.assertFalse(event.get_state()[1] & Gdk.ModifierType.SHIFT_MASK)

    def test_toolbox_command_is_inserted_into_active_terminal(self) -> None:
        terminal = TerminalSession(
            id="terminal-1",
            project_id=None,
            name="Terminal 1",
            tmux_name="mujterm-terminal-1",
            initial_cwd="/tmp",
            last_cwd="/tmp",
            position=0,
        )
        item = ToolboxCommand(
            id="command-1",
            name="Dev server",
            command="pnpm dev",
            position=0,
        )
        backend = SimpleNamespace(send_text=Mock())
        popover = SimpleNamespace(popdown=Mock())
        terminal_widget = SimpleNamespace(grab_focus=Mock())
        window = SimpleNamespace(
            active_terminal_id=terminal.id,
            database=SimpleNamespace(get_terminal=Mock(return_value=terminal)),
            backend=backend,
            toolbox_popover=popover,
            terminal_views={
                terminal.id: SimpleNamespace(terminal=terminal_widget),
            },
            _terminal_input=Mock(),
            _error=Mock(),
        )

        MainWindow._insert_toolbox_command(window, item)

        backend.send_text.assert_called_once_with(terminal.tmux_name, item.command)
        popover.popdown.assert_called_once_with()
        window._terminal_input.assert_called_once_with(terminal.id)
        terminal_widget.grab_focus.assert_called_once_with()
        window._error.assert_not_called()

    def test_ssh_connection_builds_safe_arguments_and_starts_in_terminal(self) -> None:
        connection = SshConnection(
            project_id="project-1", target="root@example.com", port=2222
        )
        terminal = TerminalSession(
            id="terminal-1",
            project_id=connection.project_id,
            name="Terminal 1",
            tmux_name="mujterm-terminal-1",
            initial_cwd="/tmp",
            last_cwd="/tmp",
            position=0,
        )
        database = SimpleNamespace(
            get_ssh_connection=Mock(return_value=connection)
        )
        backend = SimpleNamespace(send_command=Mock())
        window = SimpleNamespace(
            database=database,
            backend=backend,
            _ssh_arguments=MainWindow._ssh_arguments,
        )

        result = MainWindow._start_project_connection(window, terminal)

        self.assertEqual(result, connection)
        backend.send_command.assert_called_once_with(
            terminal.tmux_name,
            ["ssh", "-p", "2222", "root@example.com"],
        )

    def test_ssh_port_parser_accepts_default_and_rejects_text(self) -> None:
        self.assertIsNone(MainWindow._parse_ssh_port("  "))
        self.assertEqual(MainWindow._parse_ssh_port("2222"), 2222)
        with self.assertRaisesRegex(ValueError, "must be a number"):
            MainWindow._parse_ssh_port("http")


if __name__ == "__main__":
    unittest.main()
