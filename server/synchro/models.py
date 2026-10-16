"""Values shared by the player adapter and session coordinator.

Volume deliberately has no representation here: it belongs to each device.
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlayerSnapshot:
    path: Path
    position_ms: int
    duration_ms: int
    paused: bool
    audio_index: int = -1
    subtitle_index: int = -1
    subtitle_enabled: bool = False
    subtitle_delay_ms: int = 0
    rate: float = 1.0


class PlayerUnavailable(RuntimeError):
    """PotPlayer is missing, busy, or cannot apply the requested state."""


class SessionConflict(RuntimeError):
    """A receiver sent progress for a session it no longer owns."""
