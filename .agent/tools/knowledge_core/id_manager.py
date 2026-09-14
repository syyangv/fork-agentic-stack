"""Stable ID management for Agent Knowledge notes."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Regex to extract YAML frontmatter
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
ID_PATTERN = re.compile(r"^id:\s*[\"']?([a-zA-Z0-9_-]+)[\"']?\s*$", re.MULTILINE)


def generate_note_id(prefix: str = "k-") -> str:
    """Generate a collision-resistant stable note ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    random_part = secrets.token_hex(4)
    return f"{prefix}{ts}-{random_part}"


def extract_note_id(content: str) -> Optional[str]:
    """Extract stable note ID from frontmatter if present."""
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return None
    fm_text = match.group(1)
    id_match = ID_PATTERN.search(fm_text)
    if id_match:
        return id_match.group(1).strip()
    return None


def identify_note(rel_path: Path | str, content: str) -> str:
    """
    Identify a note by its frontmatter stable ID if present;
    otherwise fallback to its relative path without modifying the note.
    """
    note_id = extract_note_id(content)
    if note_id:
        return note_id
    return str(Path(rel_path).as_posix())


def ensure_note_id(content: str, note_id: Optional[str] = None) -> tuple[str, str]:
    """
    Ensure note content contains a stable frontmatter ID.
    If already present, returns (content, existing_id) untouched.
    If missing, inserts id into frontmatter (or creates frontmatter) and returns (new_content, assigned_id).
    This must ONLY be called when generating/proposing a new note, never in batch operations.
    """
    existing_id = extract_note_id(content)
    if existing_id:
        return content, existing_id

    assigned_id = note_id or generate_note_id()
    match = FRONTMATTER_PATTERN.match(content)

    if match:
        # Insert id right after opening ---
        start_idx = match.start(1)
        new_content = content[:start_idx] + f"id: {assigned_id}\n" + content[start_idx:]
        return new_content, assigned_id
    else:
        # Prepend new frontmatter
        new_content = f"---\nid: {assigned_id}\n---\n\n{content}"
        return new_content, assigned_id
