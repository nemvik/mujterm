from __future__ import annotations

import os
import tempfile
import time
import unittest
from subprocess import CompletedProcess
from pathlib import Path
from unittest.mock import patch

from mujterm.database import Database
from mujterm.tmux_backend import TMUX_SELECTION_BINDINGS, TmuxBackend, TmuxError


class TmuxBackendTests(unittest.TestCase):
    def test_send_command_shell_quotes_prompt_and_presses_enter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = TmuxBackend(socket_path=root / "tmux.sock", config_path=root / "tmux.conf")
            success = CompletedProcess([], 0, "", "")
            with patch.object(backend, "_run", return_value=success) as run:
                backend.send_command("mujterm-test", ["codex", "fix $HOME; don't execute"])
            literal = run.call_args_list[0].args[0]
            self.assertEqual(literal[:5], ["send-keys", "-t", "=mujterm-test:", "-l", "--"])
            self.assertEqual(literal[5], "codex 'fix $HOME; don'\"'\"'t execute'")
            self.assertEqual(run.call_args_list[1].args[0][-1], "Enter")

    def test_send_text_inserts_literal_text_without_pressing_enter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = TmuxBackend(
                socket_path=root / "tmux.sock", config_path=root / "tmux.conf"
            )
            success = CompletedProcess([], 0, "", "")
            text = "printf '%s' \"$HOME\" | sed 's/a/b/'; true"
            with patch.object(backend, "_run", return_value=success) as run:
                backend.send_text("mujterm-test", text)

            run.assert_called_once_with(
                ["send-keys", "-t", "=mujterm-test:", "-l", "--", text],
                check=False,
            )

    def test_send_text_rejects_multiple_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = TmuxBackend(
                socket_path=root / "tmux.sock", config_path=root / "tmux.conf"
            )
            with patch.object(backend, "_run") as run:
                with self.assertRaisesRegex(TmuxError, "one line"):
                    backend.send_text("mujterm-test", "pwd\nls")
                run.assert_not_called()

    def test_capture_output_includes_complete_joined_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = TmuxBackend(
                socket_path=root / "tmux.sock", config_path=root / "tmux.conf"
            )
            success = CompletedProcess([], 0, "first line\nsecond line\n", "")
            with patch.object(backend, "_run", return_value=success) as run:
                output = backend.capture_output("mujterm-test")

            self.assertEqual(output, success.stdout)
            run.assert_called_once_with(
                [
                    "capture-pane",
                    "-p",
                    "-J",
                    "-S",
                    "-",
                    "-t",
                    "=mujterm-test:",
                ],
                check=False,
                timeout=2,
            )

    def test_capture_recent_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = TmuxBackend(
                socket_path=root / "tmux.sock", config_path=root / "tmux.conf"
            )
            success = CompletedProcess([], 0, "recent output\n", "")
            with patch.object(backend, "_run", return_value=success) as run:
                output = backend.capture_recent_output("mujterm-test", 320)

            self.assertEqual(output, "recent output\n")
            run.assert_called_once_with(
                [
                    "capture-pane",
                    "-p",
                    "-J",
                    "-S",
                    "-320",
                    "-t",
                    "=mujterm-test:",
                ],
                check=False,
                timeout=1,
            )

    def test_capture_cursor_context_uses_cursor_row_and_preceding_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = TmuxBackend(
                socket_path=root / "tmux.sock", config_path=root / "tmux.conf"
            )
            cursor = CompletedProcess([], 0, "7\n", "")
            context = CompletedProcess([], 0, "user@host$ echo hello\n", "")
            with patch.object(backend, "_run", side_effect=(cursor, context)) as run:
                output = backend.capture_cursor_context("mujterm-test")

            self.assertEqual(output, "user@host$ echo hello")
            self.assertEqual(
                run.call_args_list[0].args[0],
                [
                    "display-message",
                    "-p",
                    "-t",
                    "=mujterm-test:",
                    "#{cursor_y}",
                ],
            )
            self.assertEqual(
                run.call_args_list[1].args[0],
                [
                    "capture-pane",
                    "-p",
                    "-J",
                    "-S",
                    "4",
                    "-E",
                    "7",
                    "-t",
                    "=mujterm-test:",
                ],
            )

    def test_scroll_selection_uses_tmux_copy_mode_direction_and_speed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = TmuxBackend(
                socket_path=root / "tmux.sock", config_path=root / "tmux.conf"
            )
            success = CompletedProcess([], 0, "", "")
            with patch.object(backend, "_run", return_value=success) as run:
                self.assertTrue(backend.scroll_selection("mujterm-test", -4))

            run.assert_called_once_with(
                [
                    "send-keys",
                    "-t",
                    "=mujterm-test:",
                    "-X",
                    "-N",
                    "4",
                    "cursor-up",
                ],
                check=False,
                timeout=1,
            )

    def test_capture_buffer_returns_latest_tmux_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = TmuxBackend(
                socket_path=root / "tmux.sock", config_path=root / "tmux.conf"
            )
            success = CompletedProcess([], 0, "selected text", "")
            with patch.object(backend, "_run", return_value=success) as run:
                self.assertEqual(backend.capture_buffer(), "selected text")

            run.assert_called_once_with(["show-buffer"], check=False, timeout=1)

    def test_existing_config_enables_mouse_without_losing_custom_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "tmux.conf"
            config.write_text(
                "set -g mouse off\nset -g history-limit 12345\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "XDG_RUNTIME_DIR": str(root / "runtime"),
                    "XDG_CONFIG_HOME": str(root / "config"),
                },
            ):
                TmuxBackend(socket_path=root / "tmux.sock", config_path=config)
            migrated = config.read_text(encoding="utf-8")
            self.assertIn("set -g mouse on", migrated)
            self.assertNotIn("set -g mouse off", migrated)
            self.assertIn("set -g history-limit 12345", migrated)
            for binding in TMUX_SELECTION_BINDINGS:
                self.assertEqual(migrated.count(binding), 1)

    def test_create_list_and_kill_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "XDG_CONFIG_HOME": str(root / "config"),
            }
            with patch.dict(os.environ, environment):
                database = Database(root / "state.db")
                terminal = database.create_terminal(None, "Test", str(root))
                backend = TmuxBackend(
                    socket_path=root / "tmux.sock",
                    config_path=root / "tmux.conf",
                )
                try:
                    try:
                        backend.create_session(terminal)
                    except TmuxError as exc:
                        if "Operation not permitted" in str(exc):
                            self.skipTest("sandbox blocks tmux Unix sockets")
                        raise
                    time.sleep(0.1)
                    self.assertTrue(backend.has_session(terminal.tmux_name))
                    panes = backend.list_panes()
                    self.assertIn(terminal.id, panes)
                finally:
                    backend.kill_session(terminal.tmux_name)
                    database.close()


if __name__ == "__main__":
    unittest.main()
