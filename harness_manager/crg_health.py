"""Read-only health classification for CRG registry entries."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


VOLATILE_ROOTS = (Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"))


@dataclass(frozen=True)
class CrgRegistrationHealth:
    status: str
    reasons: tuple[str, ...]
    repo_path: str
    data_dir: str
    nodes: int | None


def _is_volatile(path: Path) -> bool:
    absolute = Path(path).absolute()
    return any(absolute == root or root in absolute.parents for root in VOLATILE_ROOTS)


def inspect_registration(
    entry: dict[str, str], *, require_repo: bool = True
) -> CrgRegistrationHealth:
    repo = Path(entry.get("path", ""))
    data_dir = Path(entry.get("data_dir") or repo / ".code-review-graph")
    reasons: list[str] = []
    if require_repo and not repo.is_dir():
        reasons.append("missing repository")
    if _is_volatile(data_dir):
        reasons.append("volatile data directory")
    db = data_dir / "graph.db"
    nodes = None
    if not db.is_file():
        reasons.append("missing graph database")
    else:
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                nodes = int(conn.execute("select count(*) from nodes").fetchone()[0])
            if nodes == 0:
                reasons.append("zero nodes")
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            reasons.append(f"unreadable graph database: {type(exc).__name__}")
    return CrgRegistrationHealth(
        status="red" if reasons else "green",
        reasons=tuple(reasons),
        repo_path=str(repo),
        data_dir=str(data_dir),
        nodes=nodes,
    )
