from __future__ import annotations

import unittest
from dataclasses import replace

from mujterm.models import AgentKind, AgentStatus, ListeningService, TerminalSnapshot
from mujterm.status_display import (
    CpuHistory,
    hud_status_pill,
    row_status_pill,
    sparkline_points,
    row_location,
    sparkline_tone,
    state_button_label,
    group_by_status,
)


def _snapshot(
    status: AgentStatus = AgentStatus.SHELL,
    agent: AgentKind | None = None,
    cpu_percent: float = 0.0,
    memory_bytes: int = 0,
) -> TerminalSnapshot:
    return TerminalSnapshot(
        terminal_id="t1",
        cwd="/tmp",
        command="bash",
        branch=None,
        git_root=None,
        agent=agent,
        status=status,
        cpu_percent=cpu_percent,
        memory_bytes=memory_bytes,
    )


class RowStatusPillTests(unittest.TestCase):
    def test_agent_states_use_short_labels_and_distinct_styles(self) -> None:
        cases = {
            AgentStatus.NEEDS_ACTION: ("Needs input", "pill-action"),
            AgentStatus.WORKING: ("Working", "pill-working"),
            AgentStatus.UNKNOWN: ("Connecting", "pill-working"),
            AgentStatus.READY: ("Ready", "pill-ready"),
            AgentStatus.ERROR: ("Failed", "pill-error"),
            AgentStatus.ENDED: ("Ended", "pill-error"),
        }
        for status, (label, css_class) in cases.items():
            with self.subTest(status=status):
                pill = row_status_pill(_snapshot(status, AgentKind.CLAUDE))
                self.assertEqual((pill.label, pill.css_class), (label, css_class))

    def test_idle_shell_has_no_pill(self) -> None:
        self.assertIsNone(row_status_pill(_snapshot()))
        self.assertIsNone(row_status_pill(None))

    def test_idle_agent_row_shows_a_quiet_agent_name(self) -> None:
        pill = row_status_pill(_snapshot(agent=AgentKind.CODEX))

        self.assertEqual((pill.label, pill.css_class), ("Codex", "pill-shell"))

    def test_busy_shell_reports_high_load(self) -> None:
        pill = row_status_pill(_snapshot(cpu_percent=96.0))

        self.assertEqual((pill.label, pill.css_class), ("High load", "pill-hot"))
        memory_pill = row_status_pill(_snapshot(memory_bytes=2 * 1024**3))
        self.assertEqual(memory_pill.label, "High load")

    def test_agent_state_wins_over_resource_pressure(self) -> None:
        pill = row_status_pill(
            _snapshot(AgentStatus.WORKING, AgentKind.CODEX, cpu_percent=180.0)
        )

        self.assertEqual(pill.label, "Working")


class HudStatusPillTests(unittest.TestCase):
    def test_agent_states_name_the_agent(self) -> None:
        working = hud_status_pill(
            "working", _snapshot(AgentStatus.WORKING, AgentKind.CLAUDE)
        )
        waiting = hud_status_pill(
            "attention", _snapshot(AgentStatus.NEEDS_ACTION, AgentKind.CODEX)
        )

        self.assertEqual(working.label, "Claude working")
        self.assertEqual(waiting.label, "Codex needs input")
        self.assertEqual(waiting.css_class, "pill-action")

    def test_shell_commands_report_running_done_and_failed(self) -> None:
        shell = _snapshot()

        self.assertEqual(hud_status_pill("working", shell).label, "Running")
        self.assertEqual(hud_status_pill("ready", shell).label, "Done")
        self.assertEqual(hud_status_pill("error", shell).label, "Failed")

    def test_ended_terminal_and_failed_agent_are_distinguished(self) -> None:
        ended = hud_status_pill("error", _snapshot(AgentStatus.ENDED))
        failed = hud_status_pill(
            "error", _snapshot(AgentStatus.ERROR, AgentKind.CLAUDE)
        )

        self.assertEqual(ended.label, "Ended")
        self.assertEqual(failed.label, "Claude failed")

    def test_hot_working_agent_keeps_its_label_with_pressure_colour(self) -> None:
        pill = hud_status_pill(
            "hot", _snapshot(AgentStatus.WORKING, AgentKind.CLAUDE, cpu_percent=99)
        )

        self.assertEqual((pill.label, pill.css_class), ("Claude working", "pill-hot"))
        self.assertEqual(hud_status_pill("hot", _snapshot(cpu_percent=99)).label, "High load")

    def test_idle_and_service_shells_have_no_pill(self) -> None:
        service = replace(_snapshot(), services=(ListeningService(8000, 1),))

        self.assertIsNone(hud_status_pill("idle", _snapshot()))
        self.assertIsNone(hud_status_pill("service", service))
        self.assertIsNone(hud_status_pill("idle", None))

    def test_idle_agent_shows_a_quiet_agent_name(self) -> None:
        pill = hud_status_pill("idle", _snapshot(agent=AgentKind.CODEX))

        self.assertEqual((pill.label, pill.css_class), ("Codex", "pill-shell"))


class StatusGroupTests(unittest.TestCase):
    def test_groups_keep_terminal_order_in_urgency_order(self) -> None:
        groups = group_by_status(
            {
                "a": AgentStatus.READY,
                "b": AgentStatus.NEEDS_ACTION,
                "c": AgentStatus.WORKING,
                "d": AgentStatus.UNKNOWN,
                "e": AgentStatus.READY,
                "f": AgentStatus.ERROR,
                "g": AgentStatus.ENDED,
                "h": AgentStatus.SHELL,
            }
        )

        self.assertEqual(
            groups,
            (
                ("action", ("b",)),
                ("working", ("c", "d")),
                ("ready", ("a", "e")),
                ("failed", ("f",)),
                ("ended", ("g",)),
            ),
        )

    def test_header_labels_count_each_group(self) -> None:
        self.assertEqual(state_button_label("action", 1), "1 needs input")
        self.assertEqual(state_button_label("working", 2), "◌ 2 working")
        self.assertEqual(state_button_label("ready", 3), "● 3 ready")
        self.assertEqual(state_button_label("failed", 1), "× 1 failed")
        self.assertEqual(state_button_label("ended", 2), "× 2 ended")


class RowLocationTests(unittest.TestCase):
    def test_project_root_is_named_by_its_folder(self) -> None:
        self.assertEqual(
            row_location("/home/me/Projects/mujterm", "/home/me/Projects/mujterm", "/home/me"),
            "mujterm",
        )

    def test_folders_inside_the_project_are_relative(self) -> None:
        self.assertEqual(
            row_location("/home/me/Projects/mujterm/src/mujterm", "/home/me/Projects/mujterm/", "/home/me"),
            "src/mujterm",
        )

    def test_folders_outside_the_project_keep_the_home_relative_path(self) -> None:
        self.assertEqual(
            row_location("/home/me/Projects/mujterm-old", "/home/me/Projects/mujterm", "/home/me"),
            "~/Projects/mujterm-old",
        )
        self.assertEqual(row_location("/srv/app", None, "/home/me"), "/srv/app")


class SparklineTests(unittest.TestCase):
    def test_history_keeps_only_the_last_window(self) -> None:
        history = CpuHistory(window=60.0)
        history.add("t1", 0.0, 10.0)
        history.add("t1", 30.0, 20.0)
        history.add("t1", 61.0, 30.0)

        self.assertEqual(history.samples("t1"), ((30.0, 20.0), (61.0, 30.0)))
        self.assertEqual(history.samples("missing"), ())

    def test_history_forgets_closed_terminals(self) -> None:
        history = CpuHistory()
        history.add("t1", 0.0, 10.0)
        history.add("t2", 0.0, 10.0)

        history.retain({"t2"})

        self.assertEqual(history.samples("t1"), ())
        self.assertEqual(len(history.samples("t2")), 1)

    def test_points_place_newest_sample_at_right_edge_and_clamp_load(self) -> None:
        points = sparkline_points(
            ((40.0, 0.0), (70.0, 50.0), (100.0, 250.0)),
            now=100.0,
            window=60.0,
            width=60.0,
            height=20.0,
        )

        self.assertEqual(points, ((0.0, 20.0), (30.0, 10.0), (60.0, 0.0)))

    def test_points_drop_samples_older_than_the_window(self) -> None:
        points = sparkline_points(
            ((10.0, 80.0), (50.0, 0.0)),
            now=100.0,
            window=60.0,
            width=60.0,
            height=20.0,
        )

        self.assertEqual(points, ((10.0, 20.0),))

    def test_tone_highlights_only_working_agents_and_pressure(self) -> None:
        self.assertEqual(
            sparkline_tone(_snapshot(AgentStatus.WORKING, AgentKind.CLAUDE)),
            "spark-working",
        )
        self.assertEqual(sparkline_tone(_snapshot(cpu_percent=90.0)), "spark-hot")
        self.assertEqual(sparkline_tone(_snapshot(AgentStatus.READY)), "spark-idle")
        self.assertEqual(sparkline_tone(None), "spark-idle")


if __name__ == "__main__":
    unittest.main()
