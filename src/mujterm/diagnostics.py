from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Vte  # noqa: E402

from . import __version__
from .build_info import BUILD_COMMIT, BUILD_KIND
from .tmux_backend import TMUX_SELECTION_BINDINGS, TmuxBackend


def _command_version(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            arguments,
            check=False,
            text=True,
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    output = (result.stdout or result.stderr).splitlines()
    return output[0].strip() if output else "unavailable"


def _source_commit() -> str:
    if BUILD_COMMIT:
        return BUILD_COMMIT
    project_root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short=12", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
            timeout=2,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain"],
            check=True,
            text=True,
            capture_output=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"
    return f"{commit}-dirty" if dirty else commit


def _launcher_path(environment: Mapping[str, str]) -> str:
    launcher = environment.get("MUJTERM_LAUNCHER") or sys.argv[0]
    try:
        return str(Path(launcher).expanduser().resolve())
    except OSError:
        return launcher


def _mouse_binding_state(config_path: Path) -> str:
    try:
        lines = {
            line.strip()
            for line in config_path.read_text(encoding="utf-8").splitlines()
        }
    except OSError:
        return "unavailable"
    return (
        "application passthrough enabled"
        if all(binding in lines for binding in TMUX_SELECTION_BINDINGS)
        else "configuration needs reload"
    )


def _f10_state() -> str:
    settings = Gtk.Settings.get_default()
    if settings is None:
        return "unavailable (no GTK display)"
    accelerator = settings.get_property("gtk-menu-bar-accel")
    return (
        "terminal passthrough enabled"
        if not accelerator
        else f"reserved by GTK as {accelerator}"
    )


def diagnostic_report(
    backend: TmuxBackend,
    environment: Optional[Mapping[str, str]] = None,
) -> str:
    values = environment if environment is not None else os.environ
    gtk_version = ".".join(
        str(value)
        for value in (
            Gtk.get_major_version(),
            Gtk.get_minor_version(),
            Gtk.get_micro_version(),
        )
    )
    vte_version = ".".join(
        str(value)
        for value in (
            Vte.get_major_version(),
            Vte.get_minor_version(),
            Vte.get_micro_version(),
        )
    )
    session = values.get("XDG_SESSION_TYPE", "unknown")
    display = values.get("WAYLAND_DISPLAY") or values.get("DISPLAY") or "unknown"
    lines = (
        "MujTerm diagnostics",
        f"Version: {__version__}",
        f"Build: {BUILD_KIND} ({_source_commit()})",
        f"Launcher: {_launcher_path(values)}",
        f"Code path: {Path(__file__).resolve().parent}",
        f"Platform: {platform.platform()}",
        f"Python: {platform.python_version()} ({sys.executable})",
        f"PyGObject: {gi.__version__}",
        f"GTK: {gtk_version}",
        f"VTE: {vte_version}",
        f"tmux: {_command_version(['tmux', '-V'])}",
        f"htop: {_command_version(['htop', '--version'])}",
        f"Display: {session} ({display})",
        f"TERM: {values.get('TERM', 'unknown')}",
        f"F10 routing: {_f10_state()}",
        f"Mouse routing: {_mouse_binding_state(backend.config_path)}",
        f"tmux socket: {backend.socket_path}",
        f"tmux config: {backend.config_path}",
    )
    return "\n".join(lines)
