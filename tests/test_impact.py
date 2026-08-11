from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from mujterm.impact import capture_git_impact, compare_git_impact


class ImpactTests(unittest.TestCase):
    @staticmethod
    def _git(root: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_detects_created_modified_deleted_and_resolved_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            self._git(root, "config", "user.name", "Test")
            self._git(root, "config", "user.email", "test@example.com")
            (root / "modified.txt").write_text("before\n", encoding="utf-8")
            (root / "deleted.txt").write_text("delete me\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-qm", "initial")
            (root / "dirty.txt").write_text("dirty before\n", encoding="utf-8")
            before = capture_git_impact(str(root))

            (root / "modified.txt").write_text("after and larger\n", encoding="utf-8")
            (root / "created.txt").write_text("new\n", encoding="utf-8")
            (root / "deleted.txt").unlink()
            (root / "dirty.txt").unlink()
            after = capture_git_impact(str(root))
            impact = compare_git_impact(before, after)

            self.assertIn("created.txt", impact.created)
            self.assertIn("modified.txt", impact.modified)
            self.assertIn("deleted.txt", impact.deleted)
            self.assertIn("dirty.txt", impact.resolved)
            self.assertFalse(impact.commit_changed)

    def test_detects_commit_and_branch_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            self._git(root, "config", "user.name", "Test")
            self._git(root, "config", "user.email", "test@example.com")
            (root / "file.txt").write_text("one\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-qm", "one")
            before = capture_git_impact(str(root))

            self._git(root, "switch", "-qc", "impact-test")
            (root / "file.txt").write_text("two\n", encoding="utf-8")
            self._git(root, "commit", "-qam", "two")
            after = capture_git_impact(str(root))
            impact = compare_git_impact(before, after)

            self.assertTrue(impact.branch_changed)
            self.assertEqual(impact.branch_after, "impact-test")
            self.assertTrue(impact.commit_changed)


if __name__ == "__main__":
    unittest.main()
