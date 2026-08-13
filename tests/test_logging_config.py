from __future__ import annotations

import logging
import stat
import tempfile
import unittest
from pathlib import Path

from mujterm.logging_config import (
    _reset_logging_for_tests,
    configure_logging,
    last_runtime_error,
    recent_log_lines,
    record_runtime_error,
)


class LoggingConfigurationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_logging_for_tests()

    def test_runtime_log_is_private_rotated_and_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "mujterm.log"
            configure_logging(path, max_bytes=240, backup_count=2)
            configure_logging(path, max_bytes=240, backup_count=2)
            logger = logging.getLogger("mujterm.test")

            for index in range(12):
                logger.info("rotation entry %02d %s", index, "x" * 90)
            for handler in logging.getLogger("mujterm").handlers:
                handler.flush()

            handlers = [
                handler
                for handler in logging.getLogger("mujterm").handlers
                if getattr(handler, "_mujterm_rotating_handler", False)
            ]
            self.assertEqual(len(handlers), 1)
            self.assertTrue(path.exists())
            self.assertTrue(path.with_name("mujterm.log.1").exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_last_error_and_recent_log_are_available_to_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mujterm.log"
            configure_logging(path)

            try:
                raise RuntimeError("snapshot exploded")
            except RuntimeError as exc:
                record = record_runtime_error("Background monitoring failed", exc)
            for handler in logging.getLogger("mujterm").handlers:
                handler.flush()

            self.assertIs(last_runtime_error(), record)
            self.assertIn("Background monitoring failed", record.summary())
            self.assertTrue(
                any("snapshot exploded" in line for line in recent_log_lines(path))
            )


if __name__ == "__main__":
    unittest.main()
