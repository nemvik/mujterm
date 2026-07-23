from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mujterm.metadata import GitInfoCache, ProcessUsageSampler, collect_snapshots, git_info
from mujterm.models import AgentKind, GitInfo, PaneInfo


class MetadataTests(unittest.TestCase):
    def test_git_branch_and_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
            info = git_info(str(root))
            self.assertEqual(info.root, str(root))
            self.assertEqual(info.branch, "main")
            outside = Path(directory) / "outside"
            outside.mkdir()
            self.assertIsNone(git_info(str(outside)).root)

    def test_process_tree_includes_all_descendants(self) -> None:
        children = {10: [11, 12], 11: [13], 99: [100]}
        self.assertEqual(ProcessUsageSampler._process_tree(10, children), {10, 11, 12, 13})

    def test_snapshot_carries_session_resource_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pane = PaneInfo(
                terminal_id="terminal-1",
                tmux_name="mujterm-test",
                cwd=directory,
                command="bash",
                pane_pid=123456789,
                dead=False,
            )
            snapshot = collect_snapshots(
                {pane.terminal_id: pane},
                {pane.pane_pid: (37.25, 96 * 1024 * 1024, (), (443,))},
            )[pane.terminal_id]
            self.assertEqual(snapshot.cpu_percent, 37.25)
            self.assertEqual(snapshot.memory_bytes, 96 * 1024 * 1024)
            self.assertEqual(snapshot.connected_ports, (443,))

    def test_snapshot_uses_precomputed_agents_without_rescanning_proc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pane = PaneInfo(
                terminal_id="terminal-1",
                tmux_name="mujterm-test",
                cwd=directory,
                command="node",
                pane_pid=1234,
                dead=False,
            )
            with patch(
                "mujterm.metadata.process_agents",
                side_effect=AssertionError("unexpected second process scan"),
            ):
                snapshot = collect_snapshots(
                    {pane.terminal_id: pane},
                    detected_agents={pane.pane_pid: AgentKind.CODEX},
                )[pane.terminal_id]
            self.assertEqual(snapshot.agent, AgentKind.CODEX)

    def test_git_cache_reuses_result_until_expired(self) -> None:
        cache = GitInfoCache(ttl=10)
        expected = GitInfo("/tmp/repo", "main")
        with patch("mujterm.metadata.git_info", return_value=expected) as lookup:
            self.assertEqual(cache.get("/tmp/repo"), expected)
            self.assertEqual(cache.get("/tmp/repo"), expected)
        lookup.assert_called_once_with("/tmp/repo")

    def test_sampler_detects_listening_service_in_process_tree(self) -> None:
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except PermissionError:
            self.skipTest("sandbox blocks network sockets")
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            _cpu, _memory, services, _connections = ProcessUsageSampler().sample([os.getpid()])[os.getpid()]
            self.assertIn(port, {service.port for service in services})
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
