from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .agent_bridge import INTEGRATION_ID


class IntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class IntegrationStatus:
    codex: bool
    claude: bool

    @property
    def complete(self) -> bool:
        return self.codex and self.claude


class IntegrationManager:
    def __init__(self, home: Optional[Path] = None, hook_command: Optional[str] = None) -> None:
        self.home = home or Path.home()
        self.codex_path = self.home / ".codex" / "hooks.json"
        self.claude_path = self.home / ".claude" / "settings.json"
        self.hook_command = hook_command or self._resolve_hook_command()

    def status(self) -> IntegrationStatus:
        return IntegrationStatus(
            codex=self._file_has_integration(self.codex_path),
            claude=self._file_has_integration(self.claude_path),
        )

    def install(self) -> IntegrationStatus:
        self._install_codex()
        self._install_claude()
        return self.status()

    def uninstall(self) -> IntegrationStatus:
        self._remove_from_file(self.codex_path)
        self._remove_from_file(self.claude_path)
        return self.status()

    def _install_codex(self) -> None:
        document = self._read_json(self.codex_path)
        hooks = document.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise IntegrationError(f"Invalid hooks object in {self.codex_path}")
        definitions = {
            "SessionStart": self._group("codex", "startup|resume|clear|compact"),
            "UserPromptSubmit": self._group("codex"),
            "PermissionRequest": self._group("codex", ".*"),
            "PreToolUse": self._group("codex", "request_user_input|AskUserQuestion"),
            "Stop": self._group("codex"),
        }
        changed = self._merge_groups(hooks, definitions)
        if "description" not in document:
            document["description"] = "User lifecycle hooks, including optional MujTerm status integration."
            changed = True
        if changed or not self.codex_path.exists():
            self._write_json(self.codex_path, document)

    def _install_claude(self) -> None:
        document = self._read_json(self.claude_path)
        hooks = document.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise IntegrationError(f"Invalid hooks object in {self.claude_path}")
        definitions = {
            "SessionStart": self._group("claude", "startup|resume|clear|compact"),
            "UserPromptSubmit": self._group("claude"),
            "PermissionRequest": self._group("claude", ".*"),
            "PreToolUse": self._group("claude", "AskUserQuestion"),
            "Notification": self._group(
                "claude",
                "permission_prompt|idle_prompt|elicitation_dialog|agent_needs_input|agent_completed",
            ),
            "Elicitation": self._group("claude"),
            "Stop": self._group("claude"),
            "StopFailure": self._group("claude"),
        }
        changed = self._merge_groups(hooks, definitions)
        if changed or not self.claude_path.exists():
            self._write_json(self.claude_path, document)

    def _group(self, agent: str, matcher: Optional[str] = None) -> dict[str, Any]:
        executable = shlex.quote(self.hook_command)
        command = (
            f"test ! -x {executable} || {executable} --agent {agent} "
            f"--integration-id {INTEGRATION_ID}"
        )
        group: dict[str, Any] = {
            "hooks": [{"type": "command", "command": command, "timeout": 5}]
        }
        if matcher is not None:
            group["matcher"] = matcher
        return group

    def _merge_groups(self, hooks: dict[str, Any], definitions: dict[str, dict[str, Any]]) -> bool:
        changed = False
        for event, group in definitions.items():
            groups = hooks.setdefault(event, [])
            if not isinstance(groups, list):
                raise IntegrationError(f"Invalid {event} hook list")
            if not any(self._contains_integration(existing) for existing in groups):
                groups.append(group)
                changed = True
        return changed

    def _remove_from_file(self, path: Path) -> None:
        if not path.exists():
            return
        document = self._read_json(path)
        hooks = document.get("hooks")
        if not isinstance(hooks, dict):
            return
        changed = False
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            filtered = [group for group in groups if not self._contains_integration(group)]
            if len(filtered) != len(groups):
                changed = True
            if filtered:
                hooks[event] = filtered
            else:
                hooks.pop(event)
        if changed:
            self._write_json(path, document)

    def _file_has_integration(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            document = self._read_json(path)
        except IntegrationError:
            return False
        hooks = document.get("hooks", {})
        return isinstance(hooks, dict) and any(
            self._contains_integration(group)
            for groups in hooks.values()
            if isinstance(groups, list)
            for group in groups
        )

    @staticmethod
    def _contains_integration(value: Any) -> bool:
        try:
            return INTEGRATION_ID in json.dumps(value, sort_keys=True)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrationError(f"Cannot parse {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise IntegrationError(f"Expected a JSON object in {path}")
        return document

    def _write_json(self, path: Path, document: dict[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        original_mode = 0o600
        if path.exists():
            original_mode = stat.S_IMODE(path.stat().st_mode)
            backup = path.with_name(f"{path.name}.mujterm-backup-{int(time.time())}")
            if not backup.exists():
                shutil.copy2(path, backup)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            os.fchmod(descriptor, original_mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _resolve_hook_command() -> str:
        installed = shutil.which("mujterm-agent-hook")
        if installed:
            return installed
        development_launcher = Path(__file__).resolve().parents[2] / "bin" / "mujterm-agent-hook"
        return str(development_launcher)
