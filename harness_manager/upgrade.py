"""Project-local .agent infrastructure upgrade."""
from __future__ import annotations

import fnmatch
import base64
import json
import os
import stat
import sys
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable

from . import profiles
from . import state
from . import skill_manifest
from .local_schedule_config import (
    DEFAULT_LOCAL_CONFIG,
    DEFAULT_LOCAL_TEMPLATE,
    load_local_schedule_config,
    validate_local_schedule_config_for_upgrade,
)


_ROLLBACK_JOURNAL = Path(".upgrade-transaction.json")
_MAX_ROLLBACK_BYTES = 64 * 1024 * 1024


def _is_reparse_point(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(info, "st_file_attributes", 0) & marker)


def _windows_path_owned_by_current_user(path: Path) -> bool:
    """Compare the NTFS owner SID with the current process user SID."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class TOKEN_OWNER(ctypes.Structure):
            _fields_ = [("Owner", ctypes.c_void_p)]

        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        )
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        )
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        advapi32.EqualSid.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token),
        ):
            return False
        security_descriptor = ctypes.c_void_p()
        try:
            required = wintypes.DWORD()
            advapi32.GetTokenInformation(
                token, 4, None, 0, ctypes.byref(required),
            )
            buffer = ctypes.create_string_buffer(required.value)
            if not advapi32.GetTokenInformation(
                token, 4, buffer, required, ctypes.byref(required),
            ):
                return False
            token_owner = ctypes.cast(buffer, ctypes.POINTER(TOKEN_OWNER)).contents
            owner_sid = ctypes.c_void_p()
            result = advapi32.GetNamedSecurityInfoW(
                str(path), 1, 0x00000001, ctypes.byref(owner_sid),
                None, None, None, ctypes.byref(security_descriptor),
            )
            if result != 0 or not owner_sid:
                return False
            return bool(
                advapi32.EqualSid(
                    owner_sid, token_owner.Owner,
                )
            )
        finally:
            if security_descriptor:
                kernel32.LocalFree(security_descriptor)
            kernel32.CloseHandle(token)
    except (AttributeError, OSError, ValueError):
        return False


def upgrade(
    target_root: Path | str,
    stack_root: Path | str,
    *,
    dry_run: bool = False,
    yes: bool = False,
    log: Callable[[str], None] | None = None,
) -> int:
    """Copy safe skeleton-owned .agent files into an installed project."""
    if log is None:
        log = print
    target_root = _safe_lexical_absolute(target_root)
    stack_root = Path(stack_root)
    src_agent = stack_root / ".agent"
    dst_agent = target_root / ".agent"
    root_fd: int | None = None
    portable_root_identity: tuple[int, int] | None = None
    try:
        if _descriptor_relative_supported():
            root_fd = _open_safe_absolute_directory(dst_agent)
        else:
            portable_root_identity = _portable_root_identity(dst_agent)
    except ValueError:
        print(f"error: {dst_agent} not found; install agentic-stack first", file=sys.stderr)
        return 2
    try:
        pending_recovery = _read_upgrade_file(
            root_fd, dst_agent, _ROLLBACK_JOURNAL,
            max_bytes=(_MAX_ROLLBACK_BYTES * 2),
        )
    except (OSError, ValueError) as exc:
        _close_root_fd(root_fd)
        print(f"error: cannot inspect interrupted upgrade: {exc}", file=sys.stderr)
        return 2

    if dry_run:
        if pending_recovery is not None:
            _close_root_fd(root_fd)
            log("would recover interrupted upgrade transaction")
            log("dry run; no files changed")
            return 0
    elif pending_recovery is not None:
        try:
            with _upgrade_lock(root_fd, target_root):
                _UpgradeRollback.recover_if_present(
                    root_fd=root_fd,
                    dst_agent=dst_agent,
                    portable_root_identity=portable_root_identity,
                )
        except (OSError, ValueError) as exc:
            _close_root_fd(root_fd)
            print(f"error: cannot recover interrupted upgrade: {exc}", file=sys.stderr)
            return 2
    try:
        profile, record_migration, migration_record, actions = (
            _validated_upgrade_plan(
                target_root=target_root,
                stack_root=stack_root,
                src_agent=src_agent,
                dst_agent=dst_agent,
            )
        )
    except ValueError as exc:
        _close_root_fd(root_fd)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not actions:
        log(f"{target_root}: .agent infrastructure already current")
    else:
        log(f"{'would update' if dry_run else 'updating'} {len(actions)} .agent file(s):")
        for src, dst in actions:
            log(f"  {'~' if dst.exists() else '+'} {dst.relative_to(target_root)}")

    if dry_run:
        _close_root_fd(root_fd)
        log("dry run; no files changed")
        return 0

    if not yes and sys.stdin.isatty():
        answer = input("apply upgrade? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            _close_root_fd(root_fd)
            log("aborted; no files changed")
            return 0
    if not yes and not sys.stdin.isatty():
        _close_root_fd(root_fd)
        print("error: upgrade needs confirmation; re-run with --yes or --dry-run", file=sys.stderr)
        return 2

    lock = _upgrade_lock(root_fd, target_root)
    try:
        with lock:
            # A concurrent upgrader may have left a durable journal after the
            # read-only plan above.  Recover and rebuild the complete plan
            # while holding the same lock used for capture and publication.
            _UpgradeRollback.recover_if_present(
                root_fd=root_fd,
                dst_agent=dst_agent,
                portable_root_identity=portable_root_identity,
            )
            profile, record_migration, migration_record, actions = (
                _validated_upgrade_plan(
                    target_root=target_root,
                    stack_root=stack_root,
                    src_agent=src_agent,
                    dst_agent=dst_agent,
                )
            )
            transaction_relatives = {
                *(dst.relative_to(dst_agent) for _src, dst in actions),
                Path(".gitignore"),
                Path("skills/_manifest.jsonl"),
            }
            if record_migration:
                transaction_relatives.add(Path("install.json"))
            rollback = _UpgradeRollback.capture(
                root_fd=root_fd,
                dst_agent=dst_agent,
                portable_root_identity=portable_root_identity,
                relatives=sorted(transaction_relatives),
            )
            rollback.persist()
            try:
                for src, dst in actions:
                    _copy_upgrade_action(
                        src, dst.relative_to(dst_agent), root_fd=root_fd,
                        dst_agent=dst_agent, src_agent=src_agent, profile=profile,
                        portable_root_identity=portable_root_identity,
                    )

                _merge_agent_gitignore_pinned(
                    src_agent, root_fd=root_fd, dst_agent=dst_agent, log=log,
                    portable_root_identity=portable_root_identity,
                )
                manifest, skill_count = skill_manifest.render_manifest(target_root)
                existing_manifest = _read_upgrade_file(
                    root_fd, dst_agent, Path("skills/_manifest.jsonl"),
                )
                _publish_upgrade_bytes(
                    manifest, Path("skills/_manifest.jsonl"),
                    root_fd=root_fd, dst_agent=dst_agent,
                    portable_root_identity=portable_root_identity,
                    mode=existing_manifest[1] if existing_manifest else 0o644,
                )
                log(
                    f"synced {skill_count} skill manifest "
                    f"entr{'y' if skill_count == 1 else 'ies'}"
                )
                if record_migration:
                    assert migration_record is not None
                    existing_state = _read_upgrade_file(
                        root_fd, dst_agent, Path("install.json"),
                    )
                    if existing_state is None:
                        raise ValueError("install.json disappeared during migration")
                    try:
                        document = json.loads(existing_state[0].decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise ValueError("install.json changed during migration") from exc
                    next_state = state.with_orchestration_profile(
                        document, migration_record,
                    )
                    _publish_upgrade_bytes(
                        (json.dumps(next_state, indent=2) + "\n").encode("utf-8"),
                        Path("install.json"), root_fd=root_fd, dst_agent=dst_agent,
                        portable_root_identity=portable_root_identity,
                        mode=existing_state[1],
                    )
                rollback.commit()
            except BaseException:
                try:
                    rollback.restore()
                    rollback.commit()
                except BaseException:
                    # Keep the durable journal for the next invocation.
                    pass
                raise
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        _close_root_fd(root_fd)


def _validated_upgrade_plan(
    *,
    target_root: Path,
    stack_root: Path,
    src_agent: Path,
    dst_agent: Path,
) -> tuple[str, bool, dict[str, object] | None, list[tuple[Path, Path]]]:
    """Return a complete read-only plan after validating all gate inputs."""
    profile, record_migration = profiles.resolve_upgrade_profile(
        state.load(target_root), dst_agent,
    )
    profiles.validate_blocked_configuration(dst_agent)
    validate_local_schedule_config_for_upgrade(dst_agent)
    migration_record = (
        profiles.profile_record(
            profile, forbidden_roots=(target_root, stack_root),
        )
        if record_migration else None
    )
    planned_relatives = [
        *_infrastructure_files(src_agent, profile),
        Path(DEFAULT_LOCAL_CONFIG),
        Path("memory/orchestration/config.json"),
        Path("skills/_index.md"),
        Path("skills/_manifest.jsonl"),
        Path(".gitignore"),
        Path("install.json"),
        Path("install.json.lock"),
    ]
    _validate_upgrade_destinations(dst_agent, planned_relatives)
    actions = _plan(src_agent, dst_agent, profile)
    _validate_upgrade_destinations(
        dst_agent, [dst.relative_to(dst_agent) for _src, dst in actions],
    )
    return profile, record_migration, migration_record, actions


def _merge_agent_gitignore_pinned(
    src_agent: Path,
    *,
    root_fd: int | None,
    dst_agent: Path,
    log: Callable[[str], None],
    portable_root_identity: tuple[int, int] | None = None,
) -> bool:
    """Upsert runtime ignores beneath the pinned target descriptor."""
    src = src_agent / ".gitignore"
    if not src.is_file():
        return False
    source_text = src.read_text(encoding="utf-8")
    required = [
        line.strip()
        for line in source_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    existing_file = _read_upgrade_file(root_fd, dst_agent, Path(".gitignore"))
    try:
        existing = existing_file[0].decode("utf-8") if existing_file else ""
    except UnicodeError as exc:
        raise ValueError("existing .agent/.gitignore is not UTF-8") from exc
    existing_lines = set(existing.splitlines())
    missing = [line for line in required if line not in existing_lines]
    if not missing:
        return False
    if existing:
        separator = "" if existing.endswith("\n") else "\n"
        addition = (
            separator
            + "\n# agentic-stack runtime coordination and health state\n"
            + "\n".join(missing)
            + "\n"
        )
        merged = existing + addition
    else:
        merged = source_text
    _publish_upgrade_bytes(
        merged.encode("utf-8"), Path(".gitignore"),
        root_fd=root_fd, dst_agent=dst_agent,
        portable_root_identity=portable_root_identity,
        mode=existing_file[1] if existing_file else stat.S_IMODE(src.stat().st_mode),
    )
    log("  ~ .agent/.gitignore (merged runtime ignores)")
    return True


def _plan(src_agent: Path, dst_agent: Path, profile: str) -> list[tuple[Path, Path]]:
    actions: list[tuple[Path, Path]] = []
    for rel in _infrastructure_files(src_agent, profile):
        src = src_agent / rel
        dst = dst_agent / rel
        if _needs_copy(src, dst, profile=profile, relative=rel):
            actions.append((src, dst))

    # Orchestration config is user-owned after first creation. Install the
    # safe `off` default for existing brains that do not have one, but never
    # replace local mode, budget, or project-alias choices during upgrade.
    orchestration_config = Path("memory/orchestration/config.json")
    src_config = src_agent / orchestration_config
    dst_config = dst_agent / orchestration_config
    if src_config.is_file() and not dst_config.exists():
        actions.append((src_config, dst_config))

    # This document is user-owned after its first creation.  Seed the safe
    # default for pre-Gate-11 brains, but never parse, normalize, or rewrite an
    # existing local file during upgrade.
    src_schedule_template = src_agent / DEFAULT_LOCAL_TEMPLATE
    dst_schedule_config = dst_agent / DEFAULT_LOCAL_CONFIG
    if src_schedule_template.is_file() and not dst_schedule_config.exists():
        actions.append((src_schedule_template, dst_schedule_config))

    src_index = src_agent / "skills" / "_index.md"
    dst_index = dst_agent / "skills" / "_index.md"
    if src_index.is_file() and _needs_copy(
        src_index, dst_index, profile=profile, relative=Path("skills/_index.md"),
    ):
        actions.append((src_index, dst_index))

    src_skills = src_agent / "skills"
    dst_skills = dst_agent / "skills"
    for skill_md in sorted(src_skills.glob("*/SKILL.md")):
        skill_dir = skill_md.parent
        if (dst_skills / skill_dir.name).exists():
            continue
        for src in sorted(p for p in skill_dir.rglob("*") if p.is_file() and not _ignored(p)):
            rel = src.relative_to(src_agent)
            actions.append((src, dst_agent / rel))
    return actions


def _infrastructure_files(src_agent: Path, profile: str) -> list[Path]:
    rels: list[Path] = []
    manifest = src_agent / "infrastructure.json"
    if manifest.is_file():
        rels.append(manifest.relative_to(src_agent))
    for base in ("harness",):
        root = src_agent / base
        if root.is_dir():
            rels.extend(p.relative_to(src_agent) for p in root.rglob("*.py") if not _ignored(p))
    for base in ("memory", "tools"):
        root = src_agent / base
        if root.is_dir():
            rels.extend(p.relative_to(src_agent) for p in root.glob("*.py") if not _ignored(p))
    orchestration = src_agent / "memory" / "orchestration"
    if orchestration.is_dir():
        rels.extend(
            p.relative_to(src_agent)
            for p in orchestration.rglob("*.py")
            if not _ignored(p)
        )
    memory_schemas = src_agent / "protocols" / "tool_schemas" / "memory"
    if memory_schemas.is_dir():
        rels.extend(p.relative_to(src_agent) for p in memory_schemas.rglob("*.json"))
    return sorted(rel for rel in rels if profiles.includes_infrastructure(rel, profile))


def profile_infrastructure_files(src_agent: Path, profile: str) -> list[Path]:
    """Public read-only inventory of stack-owned files for profile drift checks."""
    return _infrastructure_files(src_agent, profile)


def _ignored(path: Path) -> bool:
    parts = set(path.parts)
    if "__pycache__" in parts:
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in ("*.pyc", "*.pyo"))


def _needs_copy(src: Path, dst: Path, *, profile: str, relative: Path) -> bool:
    if not dst.is_file():
        return True
    try:
        expected = (
            profiles.infrastructure_bytes(src, profile)
            if relative == Path("infrastructure.json") else src.read_bytes()
        )
        return expected != dst.read_bytes()
    except OSError:
        return True


def needs_profile_copy(src: Path, dst: Path, *, profile: str, relative: Path) -> bool:
    """Public read-only byte comparison used by doctor and upgrade planning."""
    return _needs_copy(src, dst, profile=profile, relative=relative)


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _descriptor_relative_supported() -> bool:
    """Whether this Python/OS supports the secure POSIX descriptor backend."""
    return bool(
        os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
    )


def _close_root_fd(root_fd: int | None) -> None:
    if root_fd is not None:
        os.close(root_fd)


def _upgrade_lock(
    root_fd: int | None, target_root: Path,
) -> AbstractContextManager[object]:
    return (
        state.install_state_lock_at(root_fd)
        if root_fd is not None
        else state.install_state_lock(target_root)
    )


def _safe_lexical_absolute(path: str | Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if sys.platform == "darwin" and (
        absolute == Path("/var") or Path("/var") in absolute.parents
    ):
        absolute = Path("/private/var").joinpath(*absolute.parts[2:])
    return absolute


def _portable_root_identity(path: Path) -> tuple[int, int]:
    """Validate an absolute non-symlink directory path for Windows fallback."""
    if not path.is_absolute():
        raise ValueError("upgrade target must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError("upgrade target is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise ValueError("upgrade target must not traverse symbolic links")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("upgrade target must be a directory")
    info = path.stat()
    return info.st_dev, info.st_ino


def _assert_portable_root_identity(
    dst_agent: Path, expected: tuple[int, int] | None,
) -> None:
    if expected is None or _portable_root_identity(dst_agent) != expected:
        raise ValueError("upgrade target identity changed during operation")


def _open_safe_absolute_directory(path: Path) -> int:
    if not path.is_absolute():
        raise ValueError("upgrade target must be absolute")
    try:
        descriptor = os.open(path.anchor, _DIR_FLAGS)
        for component in path.parts[1:]:
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except OSError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise ValueError("upgrade target must not traverse symbolic links") from exc


def _assert_lexical_root_identity(dst_agent: Path, pinned_fd: int) -> None:
    current_fd = _open_safe_absolute_directory(dst_agent)
    try:
        current = os.fstat(current_fd)
        pinned = os.fstat(pinned_fd)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValueError("upgrade target identity changed during operation")
    finally:
        os.close(current_fd)


def _validate_upgrade_destinations(dst_agent: Path, relatives: list[Path]) -> None:
    """Reject pre-existing symlink/non-directory components before any write."""
    if _descriptor_relative_supported():
        root_fd = _open_safe_absolute_directory(dst_agent)
        os.close(root_fd)
    else:
        _portable_root_identity(dst_agent)
    for relative in relatives:
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("upgrade destination is invalid")
        current = dst_agent
        for index, component in enumerate(relative.parts):
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ValueError("upgrade destination is unavailable") from exc
            final = index == len(relative.parts) - 1
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                raise ValueError("upgrade destination must not traverse symbolic links")
            if not final and not stat.S_ISDIR(info.st_mode):
                raise ValueError("upgrade destination parent must be a directory")
            if final and not stat.S_ISREG(info.st_mode):
                raise ValueError("upgrade destination must be a regular file")


def _open_relative_parent(root_fd: int, relative: Path, *, create: bool) -> tuple[int, str]:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("upgrade destination is invalid")
    parent_fd = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            try:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o755, dir_fd=parent_fd)
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        return parent_fd, relative.parts[-1]
    except BaseException:
        os.close(parent_fd)
        raise


def _read_relative_file(
    root_fd: int, relative: Path, *, max_bytes: int = 16 * 1024 * 1024,
) -> tuple[bytes, int] | None:
    parent_fd = file_fd = -1
    try:
        try:
            parent_fd, name = _open_relative_parent(root_fd, relative, create=False)
            file_fd = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return None
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("upgrade source state must be a regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(file_fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise ValueError("upgrade source state exceeds size bound")
        return raw, stat.S_IMODE(info.st_mode)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _portable_relative_path(
    dst_agent: Path, relative: Path, *, create_parent: bool,
) -> Path:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("upgrade destination is invalid")
    current = dst_agent
    for component in relative.parts[:-1]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create_parent:
                raise
            current.mkdir(mode=0o755)
            info = current.lstat()
        except OSError as exc:
            raise ValueError("upgrade destination is unavailable") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or _is_reparse_point(info)
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise ValueError("upgrade destination parent must be a real directory")
    return current / relative.parts[-1]


def _read_path_file(
    dst_agent: Path, relative: Path, *, max_bytes: int = 16 * 1024 * 1024,
) -> tuple[bytes, int] | None:
    try:
        path = _portable_relative_path(dst_agent, relative, create_parent=False)
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("upgrade source state is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or _is_reparse_point(info)
        or not stat.S_ISREG(info.st_mode)
    ):
        raise ValueError("upgrade source state must be a regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("upgrade source state is unavailable") from exc
    if len(raw) > max_bytes:
        raise ValueError("upgrade source state exceeds size bound")
    return raw, stat.S_IMODE(info.st_mode)


def _read_upgrade_file(
    root_fd: int | None, dst_agent: Path, relative: Path,
    *, max_bytes: int = 16 * 1024 * 1024,
) -> tuple[bytes, int] | None:
    if root_fd is not None:
        return _read_relative_file(root_fd, relative, max_bytes=max_bytes)
    return _read_path_file(dst_agent, relative, max_bytes=max_bytes)


def _publish_bytes_path(
    raw: bytes,
    relative: Path,
    *,
    dst_agent: Path,
    portable_root_identity: tuple[int, int] | None,
    mode: int,
) -> None:
    path = _portable_relative_path(dst_agent, relative, create_parent=True)
    try:
        current = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if (
            stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(current)
            or not stat.S_ISREG(current.st_mode)
        ):
            raise ValueError("upgrade destination must be a regular file")
    temporary = path.parent / f".upgrade-{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _assert_portable_root_identity(dst_agent, portable_root_identity)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _publish_upgrade_bytes(
    raw: bytes,
    relative: Path,
    *,
    root_fd: int | None,
    dst_agent: Path,
    portable_root_identity: tuple[int, int] | None,
    mode: int,
) -> None:
    if root_fd is not None:
        _publish_bytes_descriptor_relative(
            raw, relative, root_fd=root_fd, dst_agent=dst_agent, mode=mode,
        )
        return
    _publish_bytes_path(
        raw, relative, dst_agent=dst_agent,
        portable_root_identity=portable_root_identity, mode=mode,
    )


def _relative_file_stat(
    root_fd: int | None, dst_agent: Path, relative: Path,
) -> os.stat_result | None:
    if root_fd is None:
        try:
            path = _portable_relative_path(dst_agent, relative, create_parent=False)
            info = path.lstat()
        except FileNotFoundError:
            return None
        if (
            stat.S_ISLNK(info.st_mode)
            or _is_reparse_point(info)
            or not stat.S_ISREG(info.st_mode)
        ):
            raise ValueError("upgrade destination must be a regular file")
        return info
    parent_fd = -1
    try:
        try:
            parent_fd, name = _open_relative_parent(root_fd, relative, create=False)
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("upgrade destination must be a regular file")
        return info
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


class _UpgradeRollback:
    """Bounded before-images for rollback of an interrupted publication pass."""

    def __init__(
        self,
        *,
        root_fd: int | None,
        dst_agent: Path,
        portable_root_identity: tuple[int, int] | None,
        snapshots: dict[Path, tuple[bytes, int, int, int] | None],
        existing_directories: set[Path],
    ) -> None:
        self.root_fd = root_fd
        self.dst_agent = dst_agent
        self.portable_root_identity = portable_root_identity
        self.snapshots = snapshots
        self.existing_directories = existing_directories

    @classmethod
    def capture(
        cls,
        *,
        root_fd: int | None,
        dst_agent: Path,
        portable_root_identity: tuple[int, int] | None,
        relatives: list[Path],
    ) -> "_UpgradeRollback":
        snapshots: dict[Path, tuple[bytes, int, int, int] | None] = {}
        existing_directories: set[Path] = {Path(".")}
        for relative in relatives:
            current = Path()
            for component in relative.parts[:-1]:
                current /= component
                candidate = dst_agent / current
                try:
                    info = candidate.lstat()
                except FileNotFoundError:
                    continue
                if (
                    stat.S_ISLNK(info.st_mode)
                    or _is_reparse_point(info)
                    or not stat.S_ISDIR(info.st_mode)
                ):
                    raise ValueError("upgrade destination parent must be a directory")
                existing_directories.add(current)
            existing = _read_upgrade_file(root_fd, dst_agent, relative)
            if existing is None:
                snapshots[relative] = None
                continue
            info = _relative_file_stat(root_fd, dst_agent, relative)
            assert info is not None
            snapshots[relative] = (
                existing[0], existing[1], info.st_atime_ns, info.st_mtime_ns,
            )
        return cls(
            root_fd=root_fd,
            dst_agent=dst_agent,
            portable_root_identity=portable_root_identity,
            snapshots=snapshots,
            existing_directories=existing_directories,
        )

    @classmethod
    def recover_if_present(
        cls,
        *,
        root_fd: int | None,
        dst_agent: Path,
        portable_root_identity: tuple[int, int] | None,
    ) -> bool:
        encoded = _read_upgrade_file(
            root_fd, dst_agent, _ROLLBACK_JOURNAL,
            max_bytes=(_MAX_ROLLBACK_BYTES * 2),
        )
        if encoded is None:
            return False
        journal_info = _relative_file_stat(root_fd, dst_agent, _ROLLBACK_JOURNAL)
        journal_path = dst_agent / _ROLLBACK_JOURNAL
        windows_owner_safe = bool(
            os.name == "nt"
            and journal_info is not None
            and not _is_reparse_point(journal_info)
            and _windows_path_owned_by_current_user(journal_path)
        )
        posix_owner_safe = bool(
            os.name != "nt"
            and journal_info is not None
            and not stat.S_IMODE(journal_info.st_mode) & 0o077
            and (
                not hasattr(os, "geteuid")
                or journal_info.st_uid == os.geteuid()
            )
        )
        if not (windows_owner_safe or posix_owner_safe):
            raise ValueError("upgrade recovery journal is not owner-safe")
        try:
            document = json.loads(encoded[0].decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("upgrade recovery journal is malformed") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "snapshots", "existing_directories"}
            or document.get("schema_version") != 1
            or not isinstance(document.get("snapshots"), list)
            or not isinstance(document.get("existing_directories"), list)
        ):
            raise ValueError("upgrade recovery journal is malformed")
        snapshots: dict[Path, tuple[bytes, int, int, int] | None] = {}
        total = 0
        for item in document["snapshots"]:
            if not isinstance(item, dict) or set(item) != {
                "path", "exists", "data", "mode", "atime_ns", "mtime_ns",
            }:
                raise ValueError("upgrade recovery journal is malformed")
            relative = Path(item.get("path") or "")
            if (
                relative == _ROLLBACK_JOURNAL
                or relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or relative.as_posix() != item.get("path")
                or relative in snapshots
            ):
                raise ValueError("upgrade recovery journal path is invalid")
            if item.get("exists") is False:
                if any(item.get(key) is not None for key in ("data", "mode", "atime_ns", "mtime_ns")):
                    raise ValueError("upgrade recovery journal is malformed")
                snapshots[relative] = None
                continue
            if item.get("exists") is not True or not isinstance(item.get("data"), str):
                raise ValueError("upgrade recovery journal is malformed")
            if any(
                not isinstance(item.get(key), int) or isinstance(item.get(key), bool)
                for key in ("mode", "atime_ns", "mtime_ns")
            ):
                raise ValueError("upgrade recovery journal is malformed")
            if not 0 <= item["mode"] <= 0o7777:
                raise ValueError("upgrade recovery journal is malformed")
            try:
                raw = base64.b64decode(item["data"], validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("upgrade recovery journal is malformed") from exc
            total += len(raw)
            if total > _MAX_ROLLBACK_BYTES:
                raise ValueError("upgrade recovery journal exceeds size bound")
            snapshots[relative] = (
                raw, item["mode"], item["atime_ns"], item["mtime_ns"],
            )
        existing_directories: set[Path] = {Path(".")}
        for value in document["existing_directories"]:
            if not isinstance(value, str):
                raise ValueError("upgrade recovery journal is malformed")
            relative = Path(value)
            if (
                relative == Path(".")
                or relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or relative.as_posix() != value
            ):
                raise ValueError("upgrade recovery journal path is invalid")
            existing_directories.add(relative)
        rollback = cls(
            root_fd=root_fd,
            dst_agent=dst_agent,
            portable_root_identity=portable_root_identity,
            snapshots=snapshots,
            existing_directories=existing_directories,
        )
        rollback.restore()
        rollback.commit()
        return True

    def persist(self) -> None:
        total = sum(
            len(snapshot[0])
            for snapshot in self.snapshots.values()
            if snapshot is not None
        )
        if total > _MAX_ROLLBACK_BYTES:
            raise ValueError("upgrade rollback data exceeds size bound")
        items: list[dict[str, object]] = []
        for relative, snapshot in self.snapshots.items():
            if snapshot is None:
                items.append({
                    "path": relative.as_posix(),
                    "exists": False,
                    "data": None,
                    "mode": None,
                    "atime_ns": None,
                    "mtime_ns": None,
                })
                continue
            raw, mode, atime_ns, mtime_ns = snapshot
            items.append({
                "path": relative.as_posix(),
                "exists": True,
                "data": base64.b64encode(raw).decode("ascii"),
                "mode": mode,
                "atime_ns": atime_ns,
                "mtime_ns": mtime_ns,
            })
        document = {
            "schema_version": 1,
            "snapshots": items,
            "existing_directories": sorted(
                item.as_posix()
                for item in self.existing_directories
                if item != Path(".")
            ),
        }
        _publish_upgrade_bytes(
            (json.dumps(document, separators=(",", ":")) + "\n").encode("utf-8"),
            _ROLLBACK_JOURNAL,
            root_fd=self.root_fd,
            dst_agent=self.dst_agent,
            portable_root_identity=self.portable_root_identity,
            mode=0o600,
        )

    def commit(self) -> None:
        self._remove_file(_ROLLBACK_JOURNAL)

    def restore(self) -> None:
        for relative in reversed(list(self.snapshots)):
            snapshot = self.snapshots[relative]
            if snapshot is None:
                self._remove_file(relative)
                continue
            raw, mode, atime_ns, mtime_ns = snapshot
            _publish_upgrade_bytes(
                raw, relative, root_fd=self.root_fd, dst_agent=self.dst_agent,
                portable_root_identity=self.portable_root_identity, mode=mode,
            )
            self._restore_times(relative, atime_ns, mtime_ns)
        created_directories = {
            parent
            for relative in self.snapshots
            for parent in relative.parents
            if parent != Path(".") and parent not in self.existing_directories
        }
        for relative in sorted(created_directories, key=lambda item: len(item.parts), reverse=True):
            self._remove_directory_if_empty(relative)

    def _remove_file(self, relative: Path) -> None:
        if self.root_fd is None:
            try:
                path = _portable_relative_path(
                    self.dst_agent, relative, create_parent=False,
                )
                info = path.lstat()
            except FileNotFoundError:
                return
            if (
                stat.S_ISLNK(info.st_mode)
                or _is_reparse_point(info)
                or not stat.S_ISREG(info.st_mode)
            ):
                raise ValueError("rollback destination changed type")
            _assert_portable_root_identity(
                self.dst_agent, self.portable_root_identity,
            )
            path.unlink()
            return
        parent_fd = -1
        try:
            try:
                parent_fd, name = _open_relative_parent(
                    self.root_fd, relative, create=False,
                )
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("rollback destination changed type")
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)

    def _restore_times(self, relative: Path, atime_ns: int, mtime_ns: int) -> None:
        if self.root_fd is None:
            path = _portable_relative_path(
                self.dst_agent, relative, create_parent=False,
            )
            try:
                os.utime(path, ns=(atime_ns, mtime_ns), follow_symlinks=False)
            except (NotImplementedError, TypeError):
                # Native Windows may not expose the follow_symlinks variant;
                # the immediately preceding lstat-validated path is safe here.
                os.utime(path, ns=(atime_ns, mtime_ns))
            return
        try:
            parent_fd, name = _open_relative_parent(
                self.root_fd, relative, create=False,
            )
        except FileNotFoundError:
            return
        try:
            os.utime(
                name, ns=(atime_ns, mtime_ns), dir_fd=parent_fd,
                follow_symlinks=False,
            )
        finally:
            os.close(parent_fd)

    def _remove_directory_if_empty(self, relative: Path) -> None:
        if self.root_fd is None:
            path = self.dst_agent / relative
            try:
                info = path.lstat()
            except FileNotFoundError:
                return
            if (
                stat.S_ISLNK(info.st_mode)
                or _is_reparse_point(info)
                or not stat.S_ISDIR(info.st_mode)
            ):
                raise ValueError("rollback destination changed type")
            try:
                path.rmdir()
            except OSError:
                pass
            return
        try:
            parent_fd, name = _open_relative_parent(
                self.root_fd, relative, create=False,
            )
        except FileNotFoundError:
            return
        try:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        finally:
            os.close(parent_fd)


def _publish_bytes_descriptor_relative(
    raw: bytes,
    relative: Path,
    *,
    root_fd: int,
    dst_agent: Path,
    mode: int,
) -> None:
    parent_fd, name = _open_relative_parent(root_fd, relative, create=True)
    temporary = f".upgrade-{uuid.uuid4().hex}.tmp"
    file_fd = -1
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(current.st_mode):
                raise ValueError("upgrade destination must be a regular file")
        file_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_fd,
        )
        os.fchmod(file_fd, mode)
        _write_all(file_fd, raw)
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = -1
        _assert_lexical_root_identity(dst_agent, root_fd)
        os.replace(
            temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
        )
        temporary = ""
        os.fsync(parent_fd)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _copy_action_descriptor_relative(
    src: Path,
    relative: Path,
    *,
    root_fd: int,
    dst_agent: Path,
    src_agent: Path,
    profile: str,
) -> None:
    """Publish one stack-owned file beneath the pinned target root."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("upgrade destination is invalid")
    raw, mode = _action_bytes_mode(
        src, src_agent=src_agent, profile=profile,
    )

    parent_fd = os.dup(root_fd)
    temporary = ""
    try:
        for component in relative.parts[:-1]:
            try:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                os.mkdir(component, 0o755, dir_fd=parent_fd)
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        name = relative.parts[-1]
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(current.st_mode):
                raise ValueError("upgrade destination must be a regular file")

        temporary = f".upgrade-{uuid.uuid4().hex}.tmp"
        file_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_fd,
        )
        try:
            os.fchmod(file_fd, mode)
            _write_all(file_fd, raw)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        _assert_lexical_root_identity(dst_agent, root_fd)
        os.replace(
            temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
        )
        temporary = ""
        os.fsync(parent_fd)
    finally:
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _action_bytes_mode(
    src: Path, *, src_agent: Path, profile: str,
) -> tuple[bytes, int]:
    if src == src_agent / DEFAULT_LOCAL_TEMPLATE:
        load_local_schedule_config(src)
        raw = src.read_bytes()
        mode = 0o600
    elif src == src_agent / "infrastructure.json":
        raw = profiles.infrastructure_bytes(src, profile)
        mode = stat.S_IMODE(src.stat().st_mode)
    else:
        raw = src.read_bytes()
        mode = stat.S_IMODE(src.stat().st_mode)
    return raw, mode


def _copy_upgrade_action(
    src: Path,
    relative: Path,
    *,
    root_fd: int | None,
    dst_agent: Path,
    src_agent: Path,
    profile: str,
    portable_root_identity: tuple[int, int] | None,
) -> None:
    if root_fd is not None:
        _copy_action_descriptor_relative(
            src, relative, root_fd=root_fd, dst_agent=dst_agent,
            src_agent=src_agent, profile=profile,
        )
        return
    raw, mode = _action_bytes_mode(src, src_agent=src_agent, profile=profile)
    _publish_bytes_path(
        raw, relative, dst_agent=dst_agent,
        portable_root_identity=portable_root_identity, mode=mode,
    )


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("upgrade write made no progress")
        view = view[written:]
