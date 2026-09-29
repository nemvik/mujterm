from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from mujterm.sidebar import ProjectSection


class ProjectSummaryTests(unittest.TestCase):
    def _section(self, summary: str, expanded: bool) -> SimpleNamespace:
        rows = Mock()
        rows.get_visible.return_value = expanded
        return SimpleNamespace(_summary_text=summary, rows=rows, alert=Mock())

    def test_summary_is_hidden_while_rows_show_their_own_pills(self) -> None:
        section = self._section("! 1", expanded=True)

        ProjectSection._sync_alert_visibility(section)

        section.alert.set_visible.assert_called_once_with(False)

    def test_summary_is_shown_for_a_collapsed_project(self) -> None:
        section = self._section("! 1", expanded=False)

        ProjectSection._sync_alert_visibility(section)

        section.alert.set_visible.assert_called_once_with(True)

    def test_empty_summary_stays_hidden_when_collapsed(self) -> None:
        section = self._section("", expanded=False)

        ProjectSection._sync_alert_visibility(section)

        section.alert.set_visible.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
