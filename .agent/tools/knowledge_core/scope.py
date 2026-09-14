"""Canonical path verification and scope boundaries for Knowledge Vaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .config import VaultRegistration, FORMAL_DIR_ALLOWLIST


def is_safe_canonical_path(target_path: Path, base_dir: Path) -> tuple[bool, str]:
    """
    Verify that target_path resolves strictly within base_dir.
    Rejects traversals, parent symlinks, and leaf symlinks pointing outside base_dir,
    including dangling symlinks pointing outside.
    """
    base_resolved = base_dir.resolve()

    try:
        target_resolved = target_path.resolve()
        if not target_resolved.is_relative_to(base_resolved):
            return False, f"Symlink target resolves outside vault root: {target_resolved}"
    except (RuntimeError, ValueError) as e:
        return False, f"Error resolving symlink path: {e}"

    # Check intermediate parts: if any existing or dangling ancestor symlink points outside
    curr = target_path
    while curr != curr.parent:
        if curr.is_symlink():
            try:
                curr_resolved = curr.resolve()
                if not curr_resolved.is_relative_to(base_resolved):
                    return False, f"Symlink component '{curr.name}' resolves outside vault root: {curr_resolved}"
            except (RuntimeError, ValueError) as e:
                return False, f"Error resolving symlink component: {e}"
        curr = curr.parent

    return True, "Path is within vault root"


def validate_vault_path(
    vault: VaultRegistration,
    path_input: str | Path,
    require_formal: bool = True,
) -> tuple[bool, str, Optional[Path]]:
    """
    Validate that a given path is safe, canonically within the vault,
    and (if require_formal) within the formal directory allowlist both
    lexically and after canonical symlink resolution.

    Returns:
        (is_valid, message, canonical_resolved_path)
    """
    raw_str = str(path_input).strip()
    if not raw_str:
        return False, "Empty path provided", None

    path_obj = Path(raw_str)

    # 1. Reject explicit '..' traversal components
    if ".." in path_obj.parts:
        return False, "Path traversal with '..' is forbidden", None

    vault_base = vault.path.resolve()

    # 2. Check absolute path handling
    if path_obj.is_absolute():
        try:
            resolved_input = path_obj.resolve()
            if not resolved_input.is_relative_to(vault_base):
                return False, f"Absolute path points outside vault {vault.vault_id}: {path_obj}", None
            rel_path = resolved_input.relative_to(vault_base)
        except ValueError:
            return False, f"Absolute path points outside vault {vault.vault_id}: {path_obj}", None
    else:
        rel_path = path_obj

    if not rel_path.parts:
        return False, "Path is empty or vault root", None

    # 3. Lexical formal directory allowlist check
    allowed = vault.allowed_dirs if vault.allowed_dirs else list(FORMAL_DIR_ALLOWLIST)
    if require_formal and not vault.is_personal:
        top_dir = rel_path.parts[0]
        if top_dir not in allowed:
            return False, f"Directory '{top_dir}' is not in the formal directory allowlist: {allowed}", None

    # 4. Construct candidate target and verify intermediate components for external symlinks
    candidate_target = vault_base / rel_path

    accum = vault_base
    for part in rel_path.parts:
        accum = accum / part
        if accum.is_symlink():
            try:
                sym_target = accum.resolve()
                if not sym_target.is_relative_to(vault_base):
                    return False, f"Symlink '{part}' points outside vault boundary to '{sym_target}'", None
            except Exception as e:
                return False, f"Error resolving symlink '{part}': {e}", None

    is_safe, reason = is_safe_canonical_path(candidate_target, vault_base)
    if not is_safe:
        return False, reason, None

    # 5. Resolve canonical target
    # Path.resolve() resolves symlinks even if target does not yet exist (including dangling symlinks)
    try:
        canonical_target = candidate_target.resolve()
    except (RuntimeError, ValueError) as e:
        return False, f"Failed to resolve path: {e}", None

    try:
        canonical_rel = canonical_target.relative_to(vault_base)
    except ValueError:
        return False, f"Canonical path resolves outside vault root: {canonical_target}", None

    # 6. Canonical formal directory allowlist check
    # Prevents internal symlinks (e.g. 10-Projects/draft_link -> ../00-Inbox/draft.md
    # or dangling 10-Projects/dangling_link -> ../00-Inbox/future.md)
    # from smuggling non-formal directories into formal retrieval!
    if require_formal and not vault.is_personal:
        if not canonical_rel.parts:
            return False, "Resolved canonical path is vault root, not in formal directory allowlist", None
        canonical_top_dir = canonical_rel.parts[0]
        if canonical_top_dir not in allowed:
            return (
                False,
                f"Resolved canonical directory '{canonical_top_dir}' is not in the formal directory allowlist: {allowed}",
                None,
            )

    return True, "Path is valid", canonical_target
