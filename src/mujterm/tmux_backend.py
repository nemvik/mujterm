from __future__ import annotations

import os
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from .models import PaneInfo, TerminalSession
from .paths import config_dir, ensure_private_dir, runtime_dir


TMUX_CONFIG = """\
set -g status off
set -g mouse on
set -g history-limit 50000
set -g set-clipboard on
set -g allow-rename off
set -g default-terminal \"screen-256color\"
"""


class TmuxError(RuntimeError):
    pass


class TmuxBackend:
    def __init__(
        self,
        socket_path: Optional[Path] = None,
        config_path: Optional[Path] = None,
    ) -> None:
        runtime = ensure_private_dir(runtime_dir())
        self.socket_path = socket_path or runtime / "tmux.sock"
        config_root = ensure_private_dir(config_dir())
        self.config_path = config_path or config_root / "tmux.conf"
        self._ensure_config()

    def _ensure_config(self) -> None:
        if not self.config_path.exists():
            content = TMUX_CONFIG
        else:
            try:
                lines = self.config_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            mouse_option = next(
                (index for index, line in enumerate(lines) if line.strip().startswith("set -g mouse ")),
                None,
            )
            if mouse_option is None:
                lines.append("set -g mouse on")
            else:
                lines[mouse_option] = "set -g mouse on"
            content = "\n".join(lines).rstrip() + "\n"
        self.config_path.write_text(content, encoding="utf-8")
        self.config_path.chmod(0o600)

    def reload_config(self) -> bool:
        result = self._run(["source-file", str(self.config_path)], check=False)
        return result.returncode == 0

    @property
    def base_command(self) -> list[str]:
        return ["tmux", "-S", str(self.socket_path), "-f", str(self.config_path)]

    def available(self) -> bool:
        return subprocess.run(
            ["sh", "-c", "command -v tmux >/dev/null 2>&1"], check=False
        ).returncode == 0

    def has_session(self, tmux_name: str) -> bool:
        result = self._run(["has-session", "-t", f"={tmux_name}"], check=False)
        return result.returncode == 0

    def create_session(self, terminal: TerminalSession, cwd: Optional[str] = None) -> None:
        if self.has_session(terminal.tmux_name):
            return
        start_directory = self._safe_cwd(cwd or terminal.last_cwd)
        result = self._run(
            [
                "new-session",
                "-d",
                "-s",
                terminal.tmux_name,
                "-c",
                start_directory,
                "-e",
                f"MUJTERM_TERMINAL_ID={terminal.id}",
            ],
            check=False,
        )
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "Could not create tmux session")

    def attach_command(self, tmux_name: str) -> list[str]:
        return self.base_command + ["attach-session", "-t", f"={tmux_name}"]

    def kill_session(self, tmux_name: str) -> None:
        self._run(["kill-session", "-t", f"={tmux_name}"], check=False)

    def send_command(self, tmux_name: str, arguments: list[str]) -> None:
        """Type one shell-safe argv command into a managed session."""
        command = shlex.join(arguments)
        result = self._run(
            ["send-keys", "-t", f"={tmux_name}", "-l", "--", command],
            check=False,
        )
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "Could not send terminal command")
        result = self._run(
            ["send-keys", "-t", f"={tmux_name}", "Enter"], check=False
        )
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "Could not start terminal command")

    def list_panes(self) -> dict[str, PaneInfo]:
        separator = "\x1f"
        format_string = separator.join(
            (
                "#{session_name}",
                "#{pane_current_path}",
                "#{pane_current_command}",
                "#{pane_pid}",
                "#{pane_dead}",
            )
        )
        result = self._run(
            ["list-panes", "-a", "-F", format_string], check=False, timeout=2
        )
        if result.returncode != 0:
            return {}
        panes: dict[str, PaneInfo] = {}
        for line in result.stdout.splitlines():
            fields = line.split(separator)
            if len(fields) != 5 or not fields[0].startswith("mujterm-"):
                continue
            terminal_id = self.terminal_id_from_name(fields[0])
            if not terminal_id:
                continue
            try:
                pane_pid = int(fields[3])
            except ValueError:
                pane_pid = 0
            panes[terminal_id] = PaneInfo(
                terminal_id=terminal_id,
                tmux_name=fields[0],
                cwd=fields[1],
                command=fields[2],
                pane_pid=pane_pid,
                dead=fields[4] == "1",
            )
        return panes

    def managed_session_names(self) -> list[str]:
        result = self._run(
            ["list-sessions", "-F", "#{session_name}"], check=False, timeout=2
        )
        if result.returncode != 0:
            return []
        return [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("mujterm-")
        ]

    @staticmethod
    def terminal_id_from_name(tmux_name: str) -> Optional[str]:
        raw = tmux_name.removeprefix("mujterm-")
        try:
            return str(uuid.UUID(hex=raw))
        except ValueError:
            return None

    def _run(
        self,
        arguments: list[str],
        check: bool = True,
        timeout: int = 5,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self.base_command + arguments,
                check=check,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise TmuxError("tmux is not installed") from exc
        except subprocess.CalledProcessError as exc:
            raise TmuxError(exc.stderr.strip() or str(exc)) from exc

    @staticmethod
    def _safe_cwd(cwd: str) -> str:
        path = Path(cwd).expanduser()
        if path.is_dir():
            return str(path.resolve())
        return str(Path.home())
