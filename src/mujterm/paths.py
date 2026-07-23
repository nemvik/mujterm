from __future__ import annotations

import os
from pathlib import Path


APP_DIR_NAME = "mujterm"


def _xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else fallback


def config_dir() -> Path:
    return _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config") / APP_DIR_NAME


def data_dir() -> Path:
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_DIR_NAME


def state_dir() -> Path:
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP_DIR_NAME


def runtime_dir() -> Path:
    fallback = Path("/tmp") / f"mujterm-{os.getuid()}"
    return _xdg_path("XDG_RUNTIME_DIR", fallback) / APP_DIR_NAME


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path
