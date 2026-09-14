"""Vault initialization and bootstrapping for Agent Knowledge Vault."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import ALL_DEFAULT_DIRS
from .templates import (
    HOME_TEMPLATE,
    INBOX_README_TEMPLATE,
    DEFAULT_TEMPLATES,
)


def _preflight_vault_destinations(vp: Path, runtime_dir: Path | str | None = None) -> Path:
    """
    Preflight planned target directories, files, and runtime destination before any writes.
    Validates:
    - Target vault root and ancestors are valid directories (no non-directory collisions).
    - Rejects escaping symlinks (including symlinks to existing outside directories).
    - Rejects dangling/broken symlinks across vault and runtime destinations.
    - Rejects planned directory collisions with existing regular files.
    - Rejects planned file collisions with existing directories.
    - Rejects invalid runtime destination types or non-directory ancestors.
    Supports normal macOS /var -> /private/var alias via Path.resolve().
    """
    # 1. Inspect target vault root and ancestors
    curr = vp
    while curr != curr.parent:
        if os.path.islink(curr):
            try:
                resolved_ancestor = curr.resolve()
                if not resolved_ancestor.exists():
                    raise ValueError(f"Ancestor directory '{curr}' is a dangling symlink; initialization refused.")
                if not resolved_ancestor.is_dir():
                    raise NotADirectoryError(
                        f"Ancestor directory '{curr}' resolves to a non-directory file; initialization refused."
                    )
            except (RuntimeError, ValueError) as e:
                raise ValueError(f"Unsafe ancestor symlink '{curr}': {e}")
        elif curr.exists() and not curr.is_dir():
            raise NotADirectoryError(f"Preflight check failed: destination vault ancestor '{curr}' is a non-directory file.")
        curr = curr.parent

    if os.path.islink(vp):
        try:
            resolved_root = vp.resolve()
            if not resolved_root.exists():
                raise ValueError(f"Destination vault path '{vp}' is a dangling symlink; initialization refused.")
            if not resolved_root.is_dir():
                raise FileExistsError(f"Destination vault path '{vp}' symlink resolves to a non-directory.")
        except (RuntimeError, ValueError) as e:
            raise ValueError(f"Unsafe symlink destination '{vp}': {e}")
    elif vp.exists() and not vp.is_dir():
        raise FileExistsError(f"Destination vault path '{vp}' exists and is a non-directory file.")

    vault_base = vp.resolve()

    # 2. Inspect all planned target directories
    planned_dirs = [vp / d for d in ALL_DEFAULT_DIRS] + [vp / "_templates"]
    for p_dir in planned_dirs:
        if os.path.islink(p_dir):
            try:
                resolved_dir = p_dir.resolve()
                if not resolved_dir.is_relative_to(vault_base):
                    raise ValueError(
                        f"Preflight check failed: planned directory '{p_dir.name}' is an escaping symlink "
                        f"pointing outside vault boundary to '{resolved_dir}'."
                    )
                if not resolved_dir.exists():
                    raise ValueError(f"Preflight check failed: planned directory '{p_dir.name}' is a dangling symlink.")
                if not resolved_dir.is_dir():
                    raise FileExistsError(
                        f"Preflight check failed: planned directory '{p_dir.name}' symlink resolves to a non-directory."
                    )
            except (RuntimeError, ValueError) as e:
                raise ValueError(f"Preflight check failed for directory '{p_dir}': {e}")
        elif p_dir.exists() and not p_dir.is_dir():
            raise FileExistsError(
                f"Preflight check failed: planned directory '{p_dir.name}' collides with an existing non-directory file."
            )

    # 3. Inspect all planned target files
    planned_files = [vp / "Home.md", vp / "00-Inbox" / "README.md"]
    for tmpl_name in DEFAULT_TEMPLATES:
        planned_files.append(vp / "_templates" / tmpl_name)

    for p_file in planned_files:
        try:
            resolved_file = p_file.resolve()
            if not resolved_file.is_relative_to(vault_base):
                raise ValueError(
                    f"Preflight check failed: planned file '{p_file}' resolves outside vault boundary "
                    f"to '{resolved_file}'."
                )
            if os.path.islink(p_file) and not resolved_file.exists():
                raise ValueError(f"Preflight check failed: planned file '{p_file}' is a dangling symlink.")
        except (RuntimeError, ValueError) as e:
            raise ValueError(f"Preflight check failed for file '{p_file}': {e}")

        if p_file.exists() and p_file.is_dir():
            raise FileExistsError(f"Preflight check failed: planned file '{p_file.name}' collides with an existing directory.")

    # 4. Inspect runtime directory and its ancestors if provided
    if runtime_dir:
        rt = Path(runtime_dir)
        curr_rt = rt
        while curr_rt != curr_rt.parent:
            if os.path.islink(curr_rt):
                try:
                    resolved_rt_anc = curr_rt.resolve()
                    if not resolved_rt_anc.exists():
                        raise ValueError(f"Runtime ancestor directory '{curr_rt}' is a dangling symlink; initialization refused.")
                    if not resolved_rt_anc.is_dir():
                        raise NotADirectoryError(
                            f"Runtime ancestor directory '{curr_rt}' resolves to a non-directory file; initialization refused."
                        )
                except (RuntimeError, ValueError) as e:
                    raise ValueError(f"Unsafe runtime ancestor symlink '{curr_rt}': {e}")
            elif curr_rt.exists() and not curr_rt.is_dir():
                raise NotADirectoryError(f"Preflight check failed: runtime ancestor '{curr_rt}' is a non-directory file.")
            curr_rt = curr_rt.parent

        if os.path.islink(rt):
            try:
                resolved_rt = rt.resolve()
                if not resolved_rt.exists():
                    raise ValueError(f"Runtime destination '{rt}' is a dangling symlink; initialization refused.")
                if not resolved_rt.is_dir():
                    raise FileExistsError(f"Runtime destination symlink '{rt}' resolves to a non-directory.")
            except (RuntimeError, ValueError) as e:
                raise ValueError(f"Unsafe runtime destination symlink '{rt}': {e}")
        elif rt.exists() and not rt.is_dir():
            raise FileExistsError(f"Runtime destination '{rt}' exists and is a non-directory file.")

    return vault_base


def init_work_vault(
    vault_path: Path | str,
    runtime_dir: Path | str | None = None,
    allow_nonempty: bool = False,
) -> dict[str, Any]:
    """
    Initialize a new work vault with exclusive non-overwriting file creation.
    Preflights all planned target directories, files, and runtime destinations before any writes.
    Refuses initialization if destination directory exists and is nonempty,
    unless allow_nonempty is explicitly True.
    Rejects escaping symlinks, dangling symlinks, and file/directory type collisions.

    Note: Exclusive file creation (mode="x" / O_EXCL) prevents concurrent overwrite
    within the local file system, but does not claim an OS sandbox or cross-process
    CAS against external writers.
    """
    vp = Path(vault_path)

    # 1. Preflight all destinations before making any filesystem modifications
    vault_base = _preflight_vault_destinations(vp, runtime_dir=runtime_dir)

    # 2. Refuse nonempty destination unless explicitly allowed
    if vp.exists() and any(vp.iterdir()) and not allow_nonempty:
        raise FileExistsError(
            f"Destination directory '{vp}' is not empty; initialization refused to protect existing content."
        )

    vp.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    skipped: list[str] = []

    # 3. Create directory structure
    for dir_name in ALL_DEFAULT_DIRS:
        sub_dir = vp / dir_name
        if not sub_dir.exists():
            sub_dir.mkdir(parents=True, exist_ok=True)
            created.append(f"{dir_name}/")
        else:
            skipped.append(f"{dir_name}/")

    templates_dir = vp / "_templates"
    if not templates_dir.exists():
        templates_dir.mkdir(parents=True, exist_ok=True)
        created.append("_templates/")
    else:
        skipped.append("_templates/")

    # 4. Exclusive non-overwriting file creation (O_CREAT | O_EXCL)
    files_to_write = {
        "Home.md": HOME_TEMPLATE,
        "00-Inbox/README.md": INBOX_README_TEMPLATE,
    }
    for tmpl_name in DEFAULT_TEMPLATES.items():
        files_to_write[f"_templates/{tmpl_name[0]}"] = tmpl_name[1]

    for rel_path, content in files_to_write.items():
        target_file = vp / rel_path
        target_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Atomic exclusive creation prevents TOCTOU overwrite race conditions
            with open(target_file, "x", encoding="utf-8") as f:
                f.write(content)
            created.append(rel_path)
        except FileExistsError:
            # File already exists or was concurrently created; preserve it
            skipped.append(rel_path)

    # 5. Optional runtime dir initialization (private state)
    if runtime_dir:
        rt = Path(runtime_dir)
        rt.mkdir(parents=True, exist_ok=True)

    return {
        "success": True,
        "vault_path": str(vp),
        "created": created,
        "skipped": skipped,
    }
