from __future__ import annotations

import unittest
from unittest.mock import Mock

from mujterm.app import disable_menu_bar_accelerator


class ApplicationTests(unittest.TestCase):
    def test_f10_is_not_reserved_for_the_gtk_menu(self) -> None:
        settings = Mock()

        disable_menu_bar_accelerator(settings)

        settings.set_property.assert_called_once_with("gtk-menu-bar-accel", None)


if __name__ == "__main__":
    unittest.main()
