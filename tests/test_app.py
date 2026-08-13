from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mujterm.app import MujTermApplication, disable_menu_bar_accelerator
from mujterm.database import DatabaseError


class ApplicationTests(unittest.TestCase):
    def test_f10_is_not_reserved_for_the_gtk_menu(self) -> None:
        settings = Mock()

        disable_menu_bar_accelerator(settings)

        settings.set_property.assert_called_once_with("gtk-menu-bar-accel", None)

    def test_database_startup_failure_is_logged_and_shown(self) -> None:
        application = SimpleNamespace(window=None, database=None, _fatal=Mock())
        failure = DatabaseError("integrity check failed")

        with patch("mujterm.app.Database", side_effect=failure), patch(
            "mujterm.app.record_runtime_error"
        ) as record:
            MujTermApplication.do_activate(application)

        record.assert_called_once_with("State database initialization failed", failure)
        application._fatal.assert_called_once_with(
            "Could not open MujTerm state", "integrity check failed"
        )


if __name__ == "__main__":
    unittest.main()
