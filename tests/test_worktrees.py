from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mujterm.worktrees import compare_worktree, create_race_worktrees


class WorktreeTests(unittest.TestCase):
    def test_creates_two_isolated_branches_and_compares_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            (repo / "README.md").write_text("start\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data")}):
                race = create_race_worktrees("project", str(repo), "Improve parser")
            self.assertTrue(Path(race.codex_path).is_dir())
            self.assertTrue(Path(race.claude_path).is_dir())
            self.assertNotEqual(race.codex_branch, race.claude_branch)
            (Path(race.codex_path) / "README.md").write_text("start\nchange\n", encoding="utf-8")
            comparison = compare_worktree(race.codex_path, race.base_commit)
            self.assertEqual((comparison.files, comparison.additions), (1, 1))


if __name__ == "__main__":
    unittest.main()
