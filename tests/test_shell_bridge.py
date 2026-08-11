from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mujterm.agent_listener import AgentSocketListener
from mujterm.database import Database
from mujterm.shell_bridge import (
    build_shell_event,
    emit_shell_event,
    write_shell_files,
)
from mujterm.tmux_backend import TmuxBackend, TmuxError


TERMINAL_ID = "2dd2c1f6-114d-4c9f-bef7-68353170ab23"


class ShellBridgeTests(unittest.TestCase):
    def test_builds_bounded_ephemeral_shell_events(self) -> None:
        start = build_shell_event(
            "start",
            TERMINAL_ID,
            "/bin/bash",
            "/tmp/project",
            command="printf 'hello'",
        )
        end = build_shell_event(
            "end", TERMINAL_ID, "bash", "/tmp/project", status=7
        )

        self.assertEqual(start and start["event"], "command_start")
        self.assertEqual(start and start["command"], "printf 'hello'")
        self.assertEqual(end and end["status"], 7)
        self.assertIsNone(
            build_shell_event("start", "not-a-uuid", "bash", "/tmp", command="x")
        )
        self.assertIsNone(
            build_shell_event("end", TERMINAL_ID, "bash", "/tmp", status=999)
        )

    def test_emits_to_socket_without_persisting_shell_content(self) -> None:
        environment = {
            "MUJTERM_TERMINAL_ID": TERMINAL_ID,
            "MUJTERM_SHELL_INTEGRATION": "1",
        }
        with patch.dict(os.environ, environment, clear=False), patch(
            "mujterm.shell_bridge.notify_application"
        ) as notify:
            self.assertEqual(
                emit_shell_event(
                    "start", "bash", "/tmp/project", command="secret command"
                ),
                0,
            )

        notify.assert_called_once()
        payload = notify.call_args.args[0]
        self.assertEqual(payload["kind"], "shell")
        self.assertEqual(payload["command"], "secret command")

    def test_writes_private_shell_files_with_osc_133_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = write_shell_files(root)

            self.assertIn(
                "133;C",
                (files["bash"] / "integration.bash").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "command_start",
                json.dumps(
                    build_shell_event(
                        "start", TERMINAL_ID, "zsh", "/tmp", command="true"
                    )
                ),
            )
            self.assertEqual(
                (files["bash"] / "integration.bash").stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(files["zsh"].stat().st_mode & 0o777, 0o700)

    def test_bash_launcher_reports_exact_command_and_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            config = root / "config"
            home.mkdir()
            profile_marker = root / "bash-profile-loaded"
            (home / ".bash_profile").write_text(
                "printf loaded > \"$MUJTERM_TEST_PROFILE\"\n",
                encoding="utf-8",
            )
            event_log = root / "events.jsonl"
            hook = root / "hook.py"
            hook.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "with open(os.environ['MUJTERM_TEST_EVENTS'], 'a', encoding='utf-8') as output:\n"
                "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "SHELL": "/bin/bash",
                    "XDG_CONFIG_HOME": str(config),
                    "MUJTERM_TERMINAL_ID": TERMINAL_ID,
                    "MUJTERM_SHELL_HOOK": str(hook),
                    "MUJTERM_SHELL_INTEGRATION": "1",
                    "MUJTERM_TEST_EVENTS": str(event_log),
                    "MUJTERM_TEST_PROFILE": str(profile_marker),
                }
            )
            result = subprocess.run(
                [sys.executable, "-m", "mujterm.shell_bridge", "launch"],
                input="printf 'hello\\n'\nfalse\nexit\n",
                text=True,
                capture_output=True,
                timeout=6,
                env=environment,
                check=False,
            )

            self.assertIn(result.returncode, (0, 1), result.stderr)
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            starts = [event for event in events if event[0] == "start"]
            ends = [event for event in events if event[0] == "end"]
            self.assertTrue(any("printf 'hello" in event[-1] for event in starts))
            self.assertTrue(any("false" in event[-1] for event in starts))
            self.assertIn("0", [event[-1] for event in ends])
            self.assertIn("1", [event[-1] for event in ends])
            terminal_stream = result.stdout + result.stderr
            self.assertIn("\x1b]133;C\x07", terminal_stream)
            self.assertIn("\x1b]133;D;1\x07", terminal_stream)
            self.assertEqual(profile_marker.read_text(encoding="utf-8"), "loaded")

    def test_tmux_shell_events_reach_application_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "HOME": str(root / "home"),
                "SHELL": "/bin/bash",
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            }
            Path(environment["HOME"]).mkdir()
            events: list[dict[str, object]] = []
            with patch.dict(os.environ, environment, clear=False):
                database = Database(root / "state.db")
                terminal = database.create_terminal(None, "Exact", str(root))
                backend = TmuxBackend(
                    socket_path=root / "tmux.sock",
                    config_path=root / "tmux.conf",
                )
                listener = AgentSocketListener(events.append)
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
                    time.sleep(0.15)
                    backend.send_command(
                        terminal.tmux_name,
                        ["printf", "%s\\n", "mujterm-exact-smoke"],
                    )
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        if any(
                            event.get("event") == "command_end" for event in events
                        ):
                            break
                        time.sleep(0.05)

                    starts = [
                        event
                        for event in events
                        if event.get("event") == "command_start"
                    ]
                    ends = [
                        event
                        for event in events
                        if event.get("event") == "command_end"
                    ]
                    self.assertTrue(
                        any("mujterm-exact-smoke" in str(event.get("command")) for event in starts),
                        events,
                    )
                    self.assertTrue(any(event.get("status") == 0 for event in ends), events)
                    self.assertIn(
                        "mujterm-exact-smoke",
                        backend.capture_recent_output(terminal.tmux_name),
                    )
                finally:
                    backend.kill_session(terminal.tmux_name)
                    listener.stop()
                    database.close()

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_zsh_launcher_reports_exact_command_and_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            profile_marker = root / "zsh-profile-loaded"
            (home / ".zprofile").write_text(
                "printf loaded > \"$MUJTERM_TEST_PROFILE\"\n",
                encoding="utf-8",
            )
            event_log = root / "events.jsonl"
            hook = root / "hook.py"
            hook.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "with open(os.environ['MUJTERM_TEST_EVENTS'], 'a', encoding='utf-8') as output:\n"
                "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "SHELL": str(shutil.which("zsh")),
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "MUJTERM_TERMINAL_ID": TERMINAL_ID,
                    "MUJTERM_SHELL_HOOK": str(hook),
                    "MUJTERM_SHELL_INTEGRATION": "1",
                    "MUJTERM_TEST_EVENTS": str(event_log),
                    "MUJTERM_TEST_PROFILE": str(profile_marker),
                }
            )
            result = subprocess.run(
                [sys.executable, "-m", "mujterm.shell_bridge", "launch"],
                input="print -r -- hello\nfalse\nexit\n",
                text=True,
                capture_output=True,
                timeout=6,
                env=environment,
                check=False,
            )

            self.assertIn(result.returncode, (0, 1), result.stderr)
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(event[0] == "start" and event[-1] == "false" for event in events),
                events,
            )
            self.assertTrue(
                any(event[0] == "end" and event[-1] == "1" for event in events),
                events,
            )
            terminal_stream = result.stdout + result.stderr
            self.assertIn("\x1b]133;C\x07", terminal_stream)
            self.assertIn("\x1b]133;D;1\x07", terminal_stream)
            self.assertEqual(profile_marker.read_text(encoding="utf-8"), "loaded")


if __name__ == "__main__":
    unittest.main()
