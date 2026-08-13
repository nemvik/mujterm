from __future__ import annotations

import fcntl
import os
import pty
import shutil
import signal
import struct
import tempfile
import termios
import time
import unittest
from pathlib import Path
from typing import Callable

from mujterm.tmux_backend import TmuxBackend, TmuxError


class TerminalCompatibilityTests(unittest.TestCase):
    TMUX_NAME = "mujterm-2dd2c1f6114d4c9fbef768353170ab23"

    @unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
    @unittest.skipUnless(shutil.which("htop"), "htop is not installed")
    def test_htop_receives_f10_and_mouse_quit_through_tmux_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            backend = TmuxBackend(
                socket_path=root / "tmux.sock",
                config_path=root / "tmux.conf",
            )
            target = f"={self.TMUX_NAME}:"
            client_pid: int | None = None
            master: int | None = None
            try:
                result = backend._run(
                    [
                        "new-session",
                        "-d",
                        "-s",
                        self.TMUX_NAME,
                        "-x",
                        "100",
                        "-y",
                        "30",
                        "-e",
                        f"HOME={home}",
                        "-e",
                        f"XDG_CONFIG_HOME={root / 'config'}",
                        "bash",
                        "--noprofile",
                        "--norc",
                    ],
                    check=False,
                )
                if result.returncode != 0:
                    if "Operation not permitted" in result.stderr:
                        self.skipTest("sandbox blocks tmux Unix sockets")
                    raise TmuxError(result.stderr.strip() or "Could not start test tmux")

                client_pid, master = pty.fork()
                if client_pid == 0:
                    environment = os.environ.copy()
                    environment.update(
                        {
                            "HOME": str(home),
                            "TERM": "xterm-256color",
                            "XDG_CONFIG_HOME": str(root / "config"),
                        }
                    )
                    command = backend.attach_command(self.TMUX_NAME)
                    os.execvpe(command[0], command, environment)

                fcntl.ioctl(
                    master,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", 30, 100, 0, 0),
                )
                time.sleep(0.25)

                self._start_htop(backend)
                os.write(master, b"\x1b[21~")
                self.assertTrue(
                    self._wait_until(
                        lambda: self._pane_command(backend, target) != "htop"
                    ),
                    "F10 did not leave htop",
                )

                self._start_htop(backend)
                self.assertTrue(
                    self._wait_until(
                        lambda: self._pane_value(
                            backend, target, "#{mouse_any_flag}"
                        )
                        == "1"
                    ),
                    "htop did not enable terminal mouse reporting",
                )
                screen: list[str] = []
                quit_button: tuple[int, int] | None = None

                def find_quit_button() -> bool:
                    nonlocal screen, quit_button
                    screen = backend._run(
                        ["capture-pane", "-p", "-t", target], check=False
                    ).stdout.splitlines()
                    quit_button = next(
                        (
                            (line.index("F10") + 3, row)
                            for row, line in enumerate(screen)
                            if "F10" in line and "Quit" in line
                        ),
                        None,
                    )
                    return quit_button is not None

                self.assertTrue(
                    self._wait_until(find_quit_button),
                    f"htop's Quit control was not rendered: {screen!r}",
                )
                self.assertIsNotNone(quit_button, screen)
                column, row = quit_button or (0, 0)
                click = (
                    f"\x1b[<0;{column + 1};{row + 1}M"
                    f"\x1b[<0;{column + 1};{row + 1}m"
                ).encode()
                os.write(master, click)
                self.assertTrue(
                    self._wait_until(
                        lambda: self._pane_command(backend, target) != "htop"
                    ),
                    "clicking htop's F10 Quit button did not leave htop",
                )
            finally:
                if client_pid is not None:
                    self._stop_child(client_pid)
                if master is not None:
                    try:
                        os.close(master)
                    except OSError:
                        pass
                backend._run(["kill-server"], check=False)

    def _start_htop(self, backend: TmuxBackend) -> None:
        backend.send_command(self.TMUX_NAME, ["htop", "-C", "-d", "20"])
        target = f"={self.TMUX_NAME}:"
        self.assertTrue(
            self._wait_until(lambda: self._pane_command(backend, target) == "htop"),
            "htop did not start in the test pane",
        )

    @staticmethod
    def _pane_value(backend: TmuxBackend, target: str, value: str) -> str:
        return backend._run(
            ["display-message", "-p", "-t", target, value], check=False
        ).stdout.strip()

    @classmethod
    def _pane_command(cls, backend: TmuxBackend, target: str) -> str:
        return cls._pane_value(backend, target, "#{pane_current_command}")

    @staticmethod
    def _wait_until(predicate: Callable[[], bool], timeout: float = 4.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _stop_child(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                finished, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if finished:
                return
            time.sleep(0.02)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


if __name__ == "__main__":
    unittest.main()
