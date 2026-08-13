from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from mujterm.agent_listener import AgentSocketListener
from mujterm.database import Database
from mujterm.tmux_backend import TmuxBackend, TmuxError
from mujterm.ui import TerminalView


class ExactBlocksIntegrationTests(unittest.TestCase):
    @staticmethod
    def _git(root: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_exact_block_and_impact_lens_cross_shell_tmux_and_gtk(self) -> None:
        if not Gtk.init_check([])[0]:
            self.skipTest("GTK display is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            repository = root / "repository"
            home.mkdir()
            repository.mkdir()
            self._git(repository, "init", "-q")
            self._git(repository, "config", "user.name", "Test")
            self._git(repository, "config", "user.email", "test@example.com")
            (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
            self._git(repository, "add", ".")
            self._git(repository, "commit", "-qm", "initial")
            environment = {
                "HOME": str(home),
                "SHELL": "/bin/bash",
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            }
            with patch.dict(os.environ, environment, clear=False):
                database = Database(root / "state.db")
                terminal = database.create_terminal(None, "Impact", str(repository))
                backend = TmuxBackend(
                    socket_path=root / "tmux.sock",
                    config_path=root / "tmux.conf",
                )
                window = Gtk.Window(title="MujTerm Exact Blocks Smoke")
                window.set_default_size(1100, 760)
                view: TerminalView | None = None
                capture_count = 0
                capture_lock = threading.Lock()
                capture_recent_output = backend.capture_recent_output

                def counted_capture(*args: object, **kwargs: object) -> str:
                    nonlocal capture_count
                    with capture_lock:
                        capture_count += 1
                    return capture_recent_output(*args, **kwargs)

                backend.capture_recent_output = counted_capture  # type: ignore[method-assign]

                def dispatch(payload: dict[str, object]) -> bool:
                    if view is not None:
                        view.handle_shell_event(payload)
                    return False

                listener = AgentSocketListener(
                    lambda payload: GLib.idle_add(dispatch, payload)
                )
                try:
                    try:
                        listener.start()
                    except PermissionError as exc:
                        if "Operation not permitted" in str(exc):
                            self.skipTest("sandbox blocks Unix sockets")
                        raise
                    try:
                        backend.create_session(terminal)
                    except TmuxError as exc:
                        if "Operation not permitted" in str(exc):
                            self.skipTest("sandbox blocks tmux Unix sockets")
                        raise
                    view = TerminalView(
                        terminal,
                        backend,
                        lambda _terminal_id: None,
                        lambda _terminal_id: None,
                        lambda _terminal_id: None,
                        lambda _event: False,
                        lambda _uri: None,
                        lambda _query, _case: None,
                    )
                    window.add(view)
                    window.show_all()
                    self._iterate_for(0.25)
                    backend.send_command(
                        terminal.tmux_name,
                        [
                            "sh",
                            "-c",
                            "sleep 0.4; printf 'after\\n' > tracked.txt; printf 'new\\n' > new.txt",
                        ],
                    )
                    deadline = time.monotonic() + 7
                    while time.monotonic() < deadline:
                        self._iterate_for(0.04)
                        if view.command_blocks and not view.command_blocks[-1].running:
                            break

                    self.assertTrue(view.command_blocks)
                    block = view.command_blocks[-1]
                    self.assertTrue(block.exact)
                    self.assertEqual(block.shell, "bash")
                    self.assertEqual(block.exit_code, 0)
                    self.assertIn("tracked.txt", block.git_impact.modified)
                    self.assertIn("new.txt", block.git_impact.created)
                    self.assertTrue(block.impact_ready)
                    self.assertEqual(
                        capture_count,
                        2,
                        "a hidden exact block should capture only at start and end",
                    )
                    if os.environ.get("MUJTERM_VISUAL_SMOKE") == "1":
                        view.command_blocks_button.set_active(True)
                        view._expanded_command_blocks.add(block.id)
                        view._render_command_blocks()
                        self._iterate_for(4.0)
                finally:
                    if view is not None:
                        view.destroy()
                    window.destroy()
                    backend.kill_session(terminal.tmux_name)
                    listener.stop()
                    database.close()

    @staticmethod
    def _iterate_for(duration: float) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
