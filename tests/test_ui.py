from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk

from mujterm.models import SshConnection, TerminalSession, ToolboxCommand
from mujterm.ui import (
    MainWindow,
    TerminalView,
    literal_search_regex,
    normalized_url,
    output_match_summary,
    selection_autoscroll_lines,
    selection_autoscroll_y,
)


class TerminalViewTests(unittest.TestCase):
    def test_literal_search_regex_handles_metacharacters_and_unicode(self) -> None:
        self.assertIsNotNone(literal_search_regex("error [42] + příliš"))
        self.assertIsNone(literal_search_regex(""))

    def test_project_output_summary_is_literal_case_insensitive_and_limited(self) -> None:
        count, previews = output_match_summary(
            "Error [42]\nignored\nERROR [42] twice: error [42]\nerror [42] last",
            "error [42]",
        )

        self.assertEqual(count, 4)
        self.assertEqual(
            previews,
            ("Error [42]", "ERROR [42] twice: error [42]"),
        )
        self.assertEqual(
            output_match_summary(
                "Error [42]\nerror [42]", "error [42]", case_sensitive=True
            )[0],
            1,
        )

    def test_plain_web_addresses_are_normalized_for_opening(self) -> None:
        self.assertEqual(
            normalized_url("www.example.com/docs)."),
            "https://www.example.com/docs",
        )
        self.assertEqual(
            normalized_url("https://example.com/a?b=1"),
            "https://example.com/a?b=1",
        )

    def test_search_change_installs_regex_and_moves_to_a_match(self) -> None:
        terminal = SimpleNamespace(
            search_set_regex=Mock(),
            search_set_wrap_around=Mock(),
            search_find_next=Mock(return_value=True),
        )
        view = SimpleNamespace(
            search_entry=SimpleNamespace(get_text=Mock(return_value="error [42]")),
            search_case=SimpleNamespace(get_active=Mock(return_value=False)),
            terminal=terminal,
            _set_search_status=Mock(),
        )

        TerminalView._search_changed(view)

        regex = terminal.search_set_regex.call_args.args[0]
        self.assertIsNotNone(regex)
        terminal.search_set_wrap_around.assert_called_once_with(True)
        terminal.search_find_next.assert_called_once_with()
        view._set_search_status.assert_called_once_with("")

    def test_url_at_pointer_uses_plain_url_match_when_no_hyperlink_exists(self) -> None:
        event = object()
        view = SimpleNamespace(
            terminal=SimpleNamespace(
                hyperlink_check_event=Mock(return_value=None),
                match_check_event=Mock(return_value=("www.example.com/docs).", 7)),
            ),
            _url_match_tag=7,
        )

        uri = TerminalView._uri_at_event(view, event)

        self.assertEqual(uri, "https://www.example.com/docs")

    def test_plain_left_mouse_gesture_is_forwarded_to_tmux_copy_mode(self) -> None:
        press = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
        press.button.button = 1
        motion = Gdk.Event.new(Gdk.EventType.MOTION_NOTIFY)
        motion.motion.y = 100
        motion.state = Gdk.ModifierType.BUTTON1_MASK
        release = Gdk.Event.new(Gdk.EventType.BUTTON_RELEASE)
        release.button.button = 1
        terminal = SimpleNamespace(
            get_allocated_height=Mock(return_value=200),
            get_char_height=Mock(return_value=20),
        )
        view = SimpleNamespace(
            _selection_drag_active=False,
            _selection_drag_happened=False,
            _selection_scroll_lines=0,
            _stop_selection_autoscroll=Mock(),
            _start_selection_autoscroll=Mock(),
            _schedule_tmux_clipboard_sync=Mock(),
        )

        for event in (press, motion, release):
            self.assertFalse(TerminalView._on_pointer_event(view, terminal, event))
            self.assertFalse(event.get_state()[1] & Gdk.ModifierType.SHIFT_MASK)

        view._schedule_tmux_clipboard_sync.assert_called_once_with()

    def test_other_mouse_buttons_are_left_untouched(self) -> None:
        event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
        event.button.button = 3

        view = SimpleNamespace(
            _selection_drag_active=False,
            _selection_drag_happened=False,
            _selection_scroll_lines=0,
            _stop_selection_autoscroll=Mock(),
        )
        handled = TerminalView._on_pointer_event(view, None, event)

        self.assertFalse(handled)
        self.assertFalse(event.get_state()[1] & Gdk.ModifierType.SHIFT_MASK)

    def test_shift_selection_drag_at_edges_uses_native_vte_autoscroll(self) -> None:
        self.assertEqual(selection_autoscroll_y(4, 200, 8), -1.0)
        self.assertEqual(selection_autoscroll_y(196, 200, 8), 200.0)
        self.assertEqual(selection_autoscroll_y(100, 200, 8), 100)

        event = Gdk.Event.new(Gdk.EventType.MOTION_NOTIFY)
        event.motion.x = 40
        event.motion.y = 195
        event.state = (
            Gdk.ModifierType.BUTTON1_MASK | Gdk.ModifierType.SHIFT_MASK
        )
        terminal = SimpleNamespace(
            get_allocated_height=Mock(return_value=200),
            get_char_height=Mock(return_value=20),
        )
        view = SimpleNamespace(_selection_drag_active=False)

        handled = TerminalView._on_pointer_event(view, terminal, event)

        self.assertFalse(handled)
        self.assertEqual(event.get_coords()[2], 200.0)
        self.assertTrue(event.get_state()[1] & Gdk.ModifierType.SHIFT_MASK)

    def test_tmux_selection_autoscroll_accelerates_outside_edges(self) -> None:
        self.assertEqual(selection_autoscroll_lines(100, 200, 10), 0)
        self.assertEqual(selection_autoscroll_lines(5, 200, 10), -2)
        self.assertEqual(selection_autoscroll_lines(-20, 200, 10), -6)
        self.assertEqual(selection_autoscroll_lines(195, 200, 10), 2)
        self.assertEqual(selection_autoscroll_lines(220, 200, 10), 6)

    def test_autoscroll_tick_moves_active_tmux_selection(self) -> None:
        backend = SimpleNamespace(scroll_selection=Mock(return_value=True))
        view = SimpleNamespace(
            _selection_drag_active=True,
            _selection_scroll_lines=-3,
            _selection_autoscroll_timer_id=7,
            backend=backend,
            session=SimpleNamespace(tmux_name="mujterm-test"),
        )

        self.assertTrue(TerminalView._selection_autoscroll_tick(view))

        backend.scroll_selection.assert_called_once_with("mujterm-test", -3)

    def test_released_tmux_selection_is_synced_to_desktop_clipboard(self) -> None:
        view = SimpleNamespace(
            _selection_clipboard_timer_id=7,
            backend=SimpleNamespace(capture_buffer=Mock(return_value="long selection")),
            _copy_text=Mock(),
        )

        self.assertFalse(TerminalView._sync_tmux_selection_clipboard(view))

        view._copy_text.assert_called_once_with("long selection")

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
