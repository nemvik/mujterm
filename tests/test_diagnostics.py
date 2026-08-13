from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mujterm import __version__
from mujterm.diagnostics import diagnostic_report
from mujterm.tmux_backend import TMUX_CONFIG


class DiagnosticsTests(unittest.TestCase):
    def test_report_identifies_runtime_and_terminal_input_routing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "tmux.conf"
            config.write_text(TMUX_CONFIG, encoding="utf-8")
            backend = SimpleNamespace(
                config_path=config,
                socket_path=root / "tmux.sock",
            )
            environment = {
                "MUJTERM_LAUNCHER": "/usr/bin/mujterm",
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-test",
                "TERM": "xterm-256color",
            }

            with patch(
                "mujterm.diagnostics._command_version",
                side_effect=("tmux 3.2a", "htop 3.0.5"),
            ), patch("mujterm.diagnostics._source_commit", return_value="abc123"):
                report = diagnostic_report(backend, environment)

        self.assertIn(f"Version: {__version__}", report)
        self.assertIn("Build: development (abc123)", report)
        self.assertIn("Launcher: /usr/bin/mujterm", report)
        self.assertIn("tmux: tmux 3.2a", report)
        self.assertIn("htop: htop 3.0.5", report)
        self.assertIn("Display: wayland (wayland-test)", report)
        self.assertIn("Mouse routing: application passthrough enabled", report)
        self.assertIn("tmux socket:", report)


if __name__ == "__main__":
    unittest.main()
