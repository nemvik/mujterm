from __future__ import annotations

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mujterm.agent_bridge import build_state, event_status, main, persist_state


class AgentBridgeTests(unittest.TestCase):
    def test_maps_lifecycle_events(self) -> None:
        self.assertEqual(event_status({"hook_event_name": "UserPromptSubmit"}), "working")
        self.assertEqual(event_status({"hook_event_name": "PermissionRequest"}), "needs_action")
        self.assertEqual(event_status({"hook_event_name": "Stop"}), "ready")
        self.assertEqual(event_status({"hook_event_name": "StopFailure"}), "error")
        self.assertEqual(
            event_status({"hook_event_name": "PreToolUse", "tool_name": "request_user_input"}),
            "needs_action",
        )

    def test_maps_claude_notifications(self) -> None:
        self.assertEqual(
            event_status({"hook_event_name": "Notification", "notification_type": "permission_prompt"}),
            "needs_action",
        )
        self.assertEqual(
            event_status({"hook_event_name": "Notification", "notification_type": "idle_prompt"}),
            "ready",
        )

    def test_persists_minimal_state(self) -> None:
        terminal_id = "2dd2c1f6-114d-4c9f-bef7-68353170ab23"
        state = build_state(
            "codex",
            {"hook_event_name": "Stop", "session_id": "thread-1", "cwd": "/tmp/project"},
            terminal_id,
        )
        self.assertIsNotNone(state)
        with tempfile.TemporaryDirectory() as directory:
            path = persist_state(state or {}, Path(directory))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["terminal_id"], terminal_id)
            self.assertEqual(payload["status"], "ready")
            self.assertNotIn("prompt", payload)

    def test_cli_ignores_prompt_content_and_writes_for_mujterm_session(self) -> None:
        terminal_id = "2dd2c1f6-114d-4c9f-bef7-68353170ab23"
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "MUJTERM_TERMINAL_ID": terminal_id,
                "XDG_STATE_HOME": directory,
                "XDG_RUNTIME_DIR": str(Path(directory) / "runtime"),
            }
            hook_input = StringIO(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "this must not be stored",
                        "cwd": "/tmp/project",
                    }
                )
            )
            with patch.dict(os.environ, environment), patch("sys.stdin", hook_input):
                self.assertEqual(main(["--agent", "claude"]), 0)
            state_path = Path(directory) / "mujterm" / "agents" / f"{terminal_id}.json"
            payload = state_path.read_text(encoding="utf-8")
            self.assertIn('"status":"working"', payload)
            self.assertNotIn("must not be stored", payload)


if __name__ == "__main__":
    unittest.main()
