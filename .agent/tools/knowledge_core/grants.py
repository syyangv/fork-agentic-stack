"""Task-scoped in-memory personal vault authorization grants."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import VaultRegistration
from .scope import validate_vault_path


@dataclass
class PersonalTaskGrant:
    task_id: str
    vault_id: str
    vault_root: Path
    granted_paths: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None

    def __post_init__(self):
        self.vault_root = Path(self.vault_root).resolve()

    def allows(self, target_rel_path: str | Path) -> bool:
        """
        Check if the target relative path is covered by this grant.
        Supports exact file matches and directory boundary prefix matches.
        """
        target_path = Path(target_rel_path)
        target_posix = target_path.as_posix()
        target_parts = target_path.parts

        for granted in self.granted_paths:
            granted_path = Path(granted)
            granted_posix = granted_path.as_posix()

            # Exact match
            if target_posix == granted_posix:
                return True

            # Directory prefix match: granted path parts must be prefix of target parts
            granted_parts = granted_path.parts
            if len(target_parts) > len(granted_parts):
                if target_parts[: len(granted_parts)] == granted_parts:
                    return True

        return False


class TaskGrantManager:
    """
    In-memory task grant manager.
    Grants NEVER persist across processes or write to disk.
    Grants are strictly bound to (task_id, vault_id) AND the canonical vault_root.
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        # Key: (task_id, vault_id) -> PersonalTaskGrant
        self._grants: dict[tuple[str, str], PersonalTaskGrant] = {}
        self._clock: Callable[[], float] = clock if clock is not None else time.time

    def is_persisted(self) -> bool:
        """Task grants are strictly in-memory and never persisted."""
        return False

    def _clean_if_expired(self, key: tuple[str, str], now: float) -> Optional[PersonalTaskGrant]:
        """Purge grant if expired and return active grant or None."""
        grant = self._grants.get(key)
        if grant is not None and grant.expires_at is not None and now >= grant.expires_at:
            del self._grants[key]
            return None
        return grant

    def grant(
        self,
        task_id: str,
        relative_path: str | Path,
        personal_vault: VaultRegistration,
        expires_in_seconds: Optional[float] = None,
    ) -> PersonalTaskGrant:
        """
        Grant a specific note or directory within the personal vault to a task.
        Rejects traversal, escaping paths, or invalid vault locations.
        Binds the grant to personal_vault.vault_id and canonical personal_vault.path.
        Fails closed for zero, negative, or nonfinite TTL.
        Purges any existing expired grant so old paths are never resurrected.
        """
        if not personal_vault.is_personal:
            raise ValueError(f"Vault {personal_vault.vault_id} is not personal; grants only apply to personal vaults")

        # Validate TTL: must be a positive finite number (> 0)
        if expires_in_seconds is not None:
            if (
                not isinstance(expires_in_seconds, (int, float))
                or math.isnan(expires_in_seconds)
                or math.isinf(expires_in_seconds)
                or expires_in_seconds <= 0
            ):
                raise ValueError(
                    f"Invalid expires_in_seconds: {expires_in_seconds}. "
                    "Must be a positive finite number greater than 0."
                )

        ok, msg, resolved = validate_vault_path(personal_vault, relative_path, require_formal=False)
        if not ok or resolved is None:
            raise ValueError(f"Cannot grant invalid personal vault path '{relative_path}': {msg}")

        vault_base = personal_vault.path.resolve()
        rel_posix = resolved.relative_to(vault_base).as_posix()

        now = self._clock()
        expires_at = now + expires_in_seconds if expires_in_seconds else None
        key = (task_id, personal_vault.vault_id)

        # Check and purge any existing expired grant before adding new paths
        existing_grant = self._clean_if_expired(key, now)

        if existing_grant is not None:
            # Verify canonical root matches to prevent cross-root collisions under same vault_id
            if existing_grant.vault_root != vault_base:
                raise ValueError(
                    f"Conflicting vault roots for vault_id '{personal_vault.vault_id}': "
                    f"existing {existing_grant.vault_root} vs new {vault_base}"
                )
            existing_grant.granted_paths.add(rel_posix)
            if expires_at is not None:
                existing_grant.expires_at = expires_at
            return existing_grant
        else:
            grant = PersonalTaskGrant(
                task_id=task_id,
                vault_id=personal_vault.vault_id,
                vault_root=vault_base,
                granted_paths={rel_posix},
                created_at=now,
                expires_at=expires_at,
            )
            self._grants[key] = grant
            return grant

    def revoke(self, task_id: str, vault_id: Optional[str] = None) -> bool:
        """
        Immediately revoke personal grants.
        If vault_id is specified, revokes only that vault's grant for task_id.
        Otherwise revokes all grants for task_id across any vaults.
        """
        if vault_id:
            return self._grants.pop((task_id, vault_id), None) is not None

        keys_to_remove = [k for k in self._grants if k[0] == task_id]
        for k in keys_to_remove:
            self._grants.pop(k, None)
        return len(keys_to_remove) > 0

    def check_access(
        self,
        task_id: str,
        relative_path: str | Path,
        personal_vault: VaultRegistration,
    ) -> bool:
        """
        Verify whether the task has active in-memory authorization for the given path
        in the specified personal vault (binding to vault_id AND canonical vault_root).
        """
        if not personal_vault.is_personal:
            return True

        key = (task_id, personal_vault.vault_id)
        now = self._clock()

        # Expiry check & purge
        grant = self._clean_if_expired(key, now)
        if not grant:
            return False

        # Canonical root check: grant must strictly match current vault's resolved root
        vault_base = personal_vault.path.resolve()
        if grant.vault_root != vault_base:
            return False

        # Path validation check
        ok, _, resolved = validate_vault_path(personal_vault, relative_path, require_formal=False)
        if not ok or resolved is None:
            return False

        try:
            rel_posix = resolved.relative_to(vault_base).as_posix()
        except ValueError:
            return False

        return grant.allows(rel_posix)

    def get_cli_seam_notice(self) -> str:
        return (
            "Task-scoped personal grants are process-scoped in memory and cannot be claimed "
            "across independent CLI invocations. Personal vault authorization must be passed "
            "directly via the in-process API by the supervising runner (P4 integration seam)."
        )
