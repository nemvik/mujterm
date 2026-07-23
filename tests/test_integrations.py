from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mujterm.integrations import IntegrationManager


class IntegrationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.manager = IntegrationManager(self.home, hook_command="/usr/bin/true")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_install_is_idempotent_and_preserves_hooks(self) -> None:
        self.manager.claude_path.parent.mkdir(parents=True)
        self.manager.claude_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "/usr/bin/existing"}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        self.manager.install()
        first = self.manager.claude_path.read_text(encoding="utf-8")
        self.manager.install()
        second = self.manager.claude_path.read_text(encoding="utf-8")
        self.assertEqual(first, second)
        document = json.loads(second)
        stop_groups = document["hooks"]["Stop"]
        self.assertEqual(len(stop_groups), 2)
        self.assertTrue(self.manager.status().complete)

    def test_uninstall_removes_only_mujterm_groups(self) -> None:
        self.manager.install()
        document = json.loads(self.manager.codex_path.read_text(encoding="utf-8"))
        document["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": "/usr/bin/keep-me"}]}
        )
        self.manager.codex_path.write_text(json.dumps(document), encoding="utf-8")
        self.manager.uninstall()
        remaining = json.loads(self.manager.codex_path.read_text(encoding="utf-8"))
        self.assertIn("keep-me", json.dumps(remaining))
        self.assertNotIn("mujterm-v1", json.dumps(remaining))


if __name__ == "__main__":
    unittest.main()
