"""How terminal and agent states are named, counted and charted in the UI.

Kept free of GTK so the vocabulary stays identical in the sidebar, the pane
header and the header bar, and so it can be tested without a display.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .models import AgentStatus, TerminalSnapshot

HOT_CPU_PERCENT = 85.0
HOT_MEMORY_BYTES = 1536 * 1024 * 1024
CPU_HISTORY_SECONDS = 60.0

PILL_CLASSES = (
    "pill-action",
    "pill-working",
    "pill-ready",
    "pill-error",
    "pill-hot",
    "pill-shell",
)
SPARKLINE_TONES = ("spark-working", "spark-hot", "spark-idle")
# Header-bar groups in urgency order; each maps to the statuses it counts.
STATUS_GROUPS: tuple[tuple[str, frozenset[AgentStatus]], ...] = (
    ("action", frozenset({AgentStatus.NEEDS_ACTION})),
    ("working", frozenset({AgentStatus.WORKING, AgentStatus.UNKNOWN})),
    ("ready", frozenset({AgentStatus.READY})),
    ("failed", frozenset({AgentStatus.ERROR})),
    ("ended", frozenset({AgentStatus.ENDED})),
)
# Same words and glyphs as the row pills, so a count reads like its rows.
STATE_BUTTON_LABELS = {
    "action": "{count} needs input",
    "working": "◌ {count} working",
    "ready": "● {count} ready",
    "failed": "× {count} failed",
    "ended": "× {count} ended",
}
STATE_BUTTON_TOOLTIPS = {
    "action": "Jump to the next agent that needs your input (Ctrl+Shift+A)",
    "working": "Jump to the next working agent",
    "ready": "Jump to the next agent that is ready",
    "failed": "Jump to the next agent that stopped with an error",
    "ended": "Jump to the next terminal that has ended",
}


@dataclass(frozen=True)
class StatusPill:
    label: str
    css_class: str
    glyph: str = ""


def under_pressure(snapshot: TerminalSnapshot) -> bool:
    return (
        snapshot.cpu_percent >= HOT_CPU_PERCENT
        or snapshot.memory_bytes >= HOT_MEMORY_BYTES
    )


def _agent_name(snapshot: Optional[TerminalSnapshot]) -> Optional[str]:
    return snapshot.agent.value.title() if snapshot and snapshot.agent else None


def row_location(
    cwd: str, project_root: Optional[str], home: Optional[str] = None
) -> str:
    """Short sidebar location: relative to the project where possible."""
    if project_root:
        root = project_root.rstrip(os.sep) or os.sep
        if cwd == root:
            return os.path.basename(root) or root
        if cwd.startswith(root + os.sep):
            return cwd[len(root) + 1 :]
    home = home if home is not None else str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home) :]
    return cwd


def row_status_pill(snapshot: Optional[TerminalSnapshot]) -> Optional[StatusPill]:
    """Short sidebar label; agent state wins over resource pressure."""
    if snapshot is None:
        return None
    status = snapshot.status
    if status == AgentStatus.NEEDS_ACTION:
        return StatusPill("Needs input", "pill-action")
    if status == AgentStatus.ERROR:
        return StatusPill("Failed", "pill-error", "×")
    if status == AgentStatus.ENDED:
        return StatusPill("Ended", "pill-error", "×")
    if status == AgentStatus.WORKING:
        return StatusPill("Working", "pill-working", "◌")
    if status == AgentStatus.UNKNOWN:
        return StatusPill("Connecting", "pill-working", "◌")
    if under_pressure(snapshot):
        return StatusPill("High load", "pill-hot")
    if status == AgentStatus.READY:
        return StatusPill("Ready", "pill-ready", "●")
    agent = _agent_name(snapshot)
    return StatusPill(agent, "pill-shell") if agent else None


def hud_status_pill(
    radar_state: str, snapshot: Optional[TerminalSnapshot]
) -> Optional[StatusPill]:
    """Pane-header label for a quiet-radar state, naming the agent if any."""
    agent = _agent_name(snapshot)
    status = snapshot.status if snapshot else None
    if radar_state == "attention":
        return StatusPill(f"{agent or 'Agent'} needs input", "pill-action")
    if radar_state == "error":
        if status == AgentStatus.ENDED:
            label = "Ended"
        elif agent and status == AgentStatus.ERROR:
            label = f"{agent} failed"
        else:
            label = "Failed"
        return StatusPill(label, "pill-error", "×")
    if radar_state == "hot":
        if agent and status in (AgentStatus.WORKING, AgentStatus.UNKNOWN):
            return StatusPill(f"{agent} working", "pill-hot", "◌")
        return StatusPill("High load", "pill-hot")
    if radar_state == "working":
        if not agent:
            return StatusPill("Running", "pill-working", "◌")
        verb = "connecting" if status == AgentStatus.UNKNOWN else "working"
        return StatusPill(f"{agent} {verb}", "pill-working", "◌")
    if radar_state == "ready":
        if agent and status == AgentStatus.READY:
            return StatusPill(f"{agent} ready", "pill-ready", "●")
        return StatusPill("Done", "pill-ready", "✓")
    return StatusPill(agent, "pill-shell") if agent else None


def group_by_status(
    statuses: Mapping[str, AgentStatus],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Terminal ids per header-bar group, keeping the given terminal order."""
    return tuple(
        (key, tuple(terminal_id for terminal_id, status in statuses.items() if status in members))
        for key, members in STATUS_GROUPS
    )


def state_button_label(key: str, count: int) -> str:
    return STATE_BUTTON_LABELS[key].format(count=count)


def sparkline_tone(snapshot: Optional[TerminalSnapshot]) -> str:
    if snapshot is None:
        return "spark-idle"
    if under_pressure(snapshot):
        return "spark-hot"
    if snapshot.status in (AgentStatus.WORKING, AgentStatus.UNKNOWN):
        return "spark-working"
    return "spark-idle"


class CpuHistory:
    """Recent CPU samples per terminal, bounded by time rather than count.

    The snapshot interval changes with window focus, so a time window keeps
    the sparkline's horizontal scale stable.
    """

    def __init__(self, window: float = CPU_HISTORY_SECONDS) -> None:
        self.window = window
        self._samples: dict[str, deque[tuple[float, float]]] = {}

    def add(self, terminal_id: str, timestamp: float, cpu_percent: float) -> None:
        samples = self._samples.setdefault(terminal_id, deque())
        samples.append((timestamp, cpu_percent))
        cutoff = timestamp - self.window
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def samples(self, terminal_id: str) -> tuple[tuple[float, float], ...]:
        return tuple(self._samples.get(terminal_id, ()))

    def retain(self, terminal_ids: Iterable[str]) -> None:
        keep = set(terminal_ids)
        for terminal_id in tuple(self._samples):
            if terminal_id not in keep:
                del self._samples[terminal_id]


def sparkline_points(
    samples: Iterable[tuple[float, float]],
    now: float,
    window: float,
    width: float,
    height: float,
    ceiling: float = 100.0,
) -> tuple[tuple[float, float], ...]:
    """Map samples to drawing coordinates; the newest sits at the right edge.

    Load is clamped to one full core so rows share one vertical scale.
    """
    points = []
    for timestamp, cpu_percent in samples:
        age = now - timestamp
        if age > window or age < 0:
            continue
        load = min(max(cpu_percent, 0.0), ceiling)
        points.append(
            (
                width * (window - age) / window,
                height - height * load / ceiling,
            )
        )
    return tuple(points)
