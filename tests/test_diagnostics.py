from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mujterm import __version__
from mujterm.database import SCHEMA_VERSION
from mujterm.diagnostics import diagnostic_report
from mujterm.logging_config import (
    _reset_logging_for_tests,
    configure_logging,
    record_runtime_error,
)
from mujterm.tmux_backend import TMUX_CONFIG


class DiagnosticsTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_logging_for_tests()

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

    def test_report_contains_database_health_last_error_and_recent_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "tmux.conf"
            config.write_text(TMUX_CONFIG, encoding="utf-8")
            backend = SimpleNamespace(
                config_path=config,
                socket_path=root / "tmux.sock",
            )
            database = SimpleNamespace(
                path=root / "state.db",
                schema_version=SCHEMA_VERSION,
                integrity_status="ok",
                last_backup_path=root / "state.db.backup-v0-to-v1-test",
            )
            log_path = configure_logging(root / "mujterm.log")
            record_runtime_error("Background monitoring failed", "tmux unavailable")

            report = diagnostic_report(backend, {}, database)

        self.assertIn(f"Log file: {log_path}", report)
        self.assertIn("Last runtime error:", report)
        self.assertIn("Background monitoring failed: tmux unavailable", report)
        self.assertIn(f"Database schema: {SCHEMA_VERSION}/{SCHEMA_VERSION}", report)
        self.assertIn("Database integrity: ok", report)
        self.assertIn("Recent log", report)


if __name__ == "__main__":
    unittest.main()
