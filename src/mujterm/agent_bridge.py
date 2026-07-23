from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional


INTEGRATION_ID = "mujterm-v1"
VALID_AGENTS = {"codex", "claude"}
VALID_STATUSES = {"working", "needs_action", "ready", "error"}


def _home() -> Path:
    return Path.home()


def state_root() -> Path:
    value = os.environ.get("XDG_STATE_HOME")
    return (Path(value).expanduser() if value else _home() / ".local" / "state") / "mujterm"


def runtime_root() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(value).expanduser() if value else Path("/tmp") / f"mujterm-{os.getuid()}"
    return base / "mujterm"


def event_status(payload: dict[str, Any]) -> Optional[str]:
    event = str(payload.get("hook_event_name", ""))
    if event == "SessionStart":
        return "ready"
    if event == "UserPromptSubmit":
        return "working"
    if event in {"PermissionRequest", "Elicitation"}:
        return "needs_action"
    if event == "PreToolUse":
        tool = str(payload.get("tool_name", "")).lower().replace("_", "")
        if "askuserquestion" in tool or "requestuserinput" in tool:
            return "needs_action"
        return None
    if event == "Notification":
        notification = str(payload.get("notification_type", ""))
        if notification in {
            "permission_prompt",
            "elicitation_dialog",
            "agent_needs_input",
        }:
            return "needs_action"
        if notification in {"idle_prompt", "agent_completed"}:
            return "ready"
        return None
    if event == "Stop":
        return "ready"
    if event == "StopFailure":
        return "error"
    return None


def build_state(agent: str, payload: dict[str, Any], terminal_id: str) -> Optional[dict[str, Any]]:
    status = event_status(payload)
    if agent not in VALID_AGENTS or status not in VALID_STATUSES:
        return None
    return {
        "version": 1,
        "terminal_id": terminal_id,
        "agent": agent,
        "status": status,
        "event": str(payload.get("hook_event_name", "")),
        "timestamp": time.time(),
        "cwd": payload.get("cwd"),
        "session_id": payload.get("session_id"),
    }


def persist_state(state: dict[str, Any], root: Optional[Path] = None) -> Path:
    agents_dir = (root or state_root()) / "agents"
    agents_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        agents_dir.chmod(0o700)
    except OSError:
        pass
    destination = agents_dir / f"{state['terminal_id']}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".agent-", dir=agents_dir)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return destination


def notify_application(state: dict[str, Any], socket_path: Optional[Path] = None) -> None:
    target = socket_path or runtime_root() / "agent.sock"
    message = json.dumps(state, separators=(",", ":")).encode("utf-8")
    if len(message) > 8192:
        return
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.settimeout(0.05)
        client.sendto(message, str(target))
    except OSError:
        pass
    finally:
        client.close()


def _valid_terminal_id(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        import uuid

        uuid.UUID(value)
        return True
    except ValueError:
        return False


def parse_input(stream: Any) -> dict[str, Any]:
    raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--agent", choices=sorted(VALID_AGENTS), required=True)
    parser.add_argument("--integration-id", default=INTEGRATION_ID)
    arguments, _unknown = parser.parse_known_args(argv)
    terminal_id = os.environ.get("MUJTERM_TERMINAL_ID")
    if arguments.integration_id != INTEGRATION_ID or not _valid_terminal_id(terminal_id):
        return 0
    payload = parse_input(sys.stdin)
    state = build_state(arguments.agent, payload, terminal_id)
    if not state:
        return 0
    try:
        persist_state(state)
        notify_application(state)
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
