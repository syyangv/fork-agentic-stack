"""Read-only safety audit for the legacy macOS scheduled reviewer."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


UNSAFE_MARKERS = (
    "graduate.py",
    "auto_graduated",
    "auto-graduated:",
)


@dataclass(frozen=True)
class ScheduledReviewHealth:
    status: str
    path: str
    reasons: tuple[str, ...]


def default_scheduler_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Scripts" / "agentic_stack_review_notify.py"


def inspect_scheduler(path: Path) -> ScheduledReviewHealth:
    if not path.exists():
        return ScheduledReviewHealth("green", str(path), ())
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ScheduledReviewHealth(
            "red", str(path), (f"unreadable scheduled reviewer: {type(exc).__name__}",)
        )
    found = tuple(marker for marker in UNSAFE_MARKERS if marker in source)
    reasons = tuple(f"unsafe automatic graduation marker: {marker}" for marker in found)
    return ScheduledReviewHealth("red" if reasons else "green", str(path), reasons)
