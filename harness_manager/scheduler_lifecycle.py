"""Injected, reversible LaunchAgent lifecycle operations.

This module intentionally has no default command runner and never discovers a
user home.  Callers must supply an owner-safe *fixture or explicitly selected*
home and an injected runner; production wiring is a separate, approved step.
"""
from __future__ import annotations

import os
import plistlib
import stat
import uuid
import fcntl
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .scheduled_launchers import (
    AUTO_DREAM_LABEL, REVIEW_NOTIFY_LABEL, validate_launch_agent_contract,
)


LABELS = (AUTO_DREAM_LABEL, REVIEW_NOTIFY_LABEL)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class LifecycleError(RuntimeError):
    """A command or transactional filesystem operation did not complete."""


class CommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, shell: bool) -> object:
        """Run exactly argv with no shell expansion and return returncode."""

class PlistValidator(Protocol):
    def run(self, argv: tuple[str, ...], *, shell: bool, input: bytes) -> object:
        """Validate the supplied bytes without reading a filesystem pathname."""


@dataclass(frozen=True)
class _Backup:
    raw: bytes | None
    mode: int | None
    mtime_ns: int | None
    loaded: bool
    identity: tuple[int, int] | None


def _owner_safe(info: os.stat_result, *, directory: bool) -> bool:
    if info.st_uid != os.getuid():
        return False
    if directory:
        return stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022
    return stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) in {0o600, 0o644}


def _open_home(home: str | Path) -> tuple[Path, int]:
    path = Path(home)
    if not path.is_absolute() or "\x00" in str(path):
        raise LifecycleError("scheduler home must be an absolute safe path")
    fd = os.open(path.anchor, _DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    if not _owner_safe(os.fstat(fd), directory=True):
        os.close(fd)
        raise LifecycleError("scheduler home is not owner-safe")
    return path, fd


def _safe_directory(parent_fd: int, name: str, *, create: bool) -> int:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, 0o755, dir_fd=parent_fd)
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    if not _owner_safe(os.fstat(fd), directory=True):
        os.close(fd)
        raise LifecycleError("LaunchAgent directory is not owner-safe")
    return fd


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short scheduler plist write")
        view = view[count:]


def _validate_generated(label: str, raw: bytes, expected: bytes | None = None) -> None:
    if label not in LABELS or not isinstance(raw, bytes) or len(raw) > 64 * 1024:
        raise LifecycleError("generated scheduler plist is invalid")
    try:
        value = plistlib.loads(raw)
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise LifecycleError("generated scheduler plist is invalid") from exc
    allowed = {"Label", "ProgramArguments", "StartCalendarInterval", "RunAtLoad", "EnvironmentVariables"}
    argv = value.get("ProgramArguments") if isinstance(value, dict) else None
    schedule = value.get("StartCalendarInterval") if isinstance(value, dict) else None
    tail = (("maintain", "--stage-candidates", "--scheduled") if label == AUTO_DREAM_LABEL
            else ("review", "prepare", "--scheduled", "--notify"))
    if (not isinstance(value, dict) or set(value) != allowed or value.get("Label") != label
            or value.get("RunAtLoad") is not False
            or value.get("EnvironmentVariables") != {"AGENTIC_SCHEDULER_RUN": "1"}
            or not isinstance(argv, list) or len(argv) != 2 + len(tail)
            or not all(isinstance(part, str) and part and "\x00" not in part for part in argv)
            or not Path(argv[0]).is_absolute() or not Path(argv[1]).is_absolute()
            or not argv[1].endswith("/tools/memory_orchestrate.py")
            or tuple(argv[2:]) != tail
            or not isinstance(schedule, dict) or set(schedule) != {"Hour", "Minute"}
            or type(schedule["Hour"]) is not int or type(schedule["Minute"]) is not int
            or not 0 <= schedule["Hour"] <= 23 or not 0 <= schedule["Minute"] <= 59):
        raise LifecycleError("generated scheduler plist is invalid")
    if expected is not None:
        try:
            validate_launch_agent_contract(label, raw, expected)
        except ValueError as exc:
            raise LifecycleError(str(exc)) from exc


def _validate_with_plutil(validator: PlistValidator, raw: bytes, plutil: str) -> None:
    result = validator.run((plutil, "-lint", "-"), shell=False, input=raw)
    if getattr(result, "returncode", 1) != 0:
        raise LifecycleError("generated scheduler plist failed injected plutil validation")


def _require_contract_paths(raw: bytes) -> None:
    """Bind the configured runtime and entrypoint without following components."""
    value = plistlib.loads(raw)
    for index, name in ((0, "runtime"), (1, "entrypoint")):
        path = Path(value["ProgramArguments"][index])
        descriptor = os.open(path.anchor, _DIR_FLAGS)
        try:
            for component in path.parts[1:-1]:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_fd
            fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                         dir_fd=descriptor)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise LifecycleError(f"scheduler {name} is not a regular file")
                if index == 0 and not info.st_mode & 0o111:
                    raise LifecycleError("scheduler runtime is not executable")
            finally:
                os.close(fd)
        except OSError as exc:
            raise LifecycleError(f"scheduler {name} is unavailable") from exc
        finally:
            os.close(descriptor)


def _read_backup(parent_fd: int, name: str, loaded: bool) -> _Backup:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _Backup(None, None, None, loaded, None)
    if not _owner_safe(info, directory=False):
        raise LifecycleError("existing LaunchAgent plist is not owner-safe")
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or not _owner_safe(opened, directory=False)):
            raise LifecycleError("existing LaunchAgent plist identity changed")
        raw = b""
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            raw += chunk
            if len(raw) > 64 * 1024:
                raise LifecycleError("existing LaunchAgent plist exceeds bound")
    finally:
        os.close(fd)
    return _Backup(
        raw, stat.S_IMODE(opened.st_mode), opened.st_mtime_ns, loaded,
        (opened.st_dev, opened.st_ino),
    )


def _current_identity(parent_fd: int, name: str) -> tuple[int, int] | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not _owner_safe(info, directory=False):
        raise LifecycleError("LaunchAgent plist is not owner-safe")
    return info.st_dev, info.st_ino


def _atomic_write(parent_fd: int, name: str, raw: bytes, *, mode: int = 0o600,
                  mtime_ns: int | None = None,
                  expected_identity: tuple[int, int] | None) -> tuple[int, int]:
    temporary = "." + name + "." + uuid.uuid4().hex + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o600, dir_fd=parent_fd)
    try:
        try:
            os.fchmod(fd, mode)
            _write_all(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        if _current_identity(parent_fd, name) != expected_identity:
            raise LifecycleError("LaunchAgent plist changed during scheduler transaction")
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        if mtime_ns is not None:
            os.utime(name, ns=(mtime_ns, mtime_ns), dir_fd=parent_fd, follow_symlinks=False)
        identity = _current_identity(parent_fd, name)
        if identity is None:
            raise LifecycleError("LaunchAgent plist publication is missing")
        os.fsync(parent_fd)
        return identity
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise


def _remove(
    parent_fd: int, name: str, *,
    expected_identity: tuple[int, int] | None,
) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not _owner_safe(info, directory=False):
        raise LifecycleError("refusing to remove non-owner-safe LaunchAgent plist")
    if expected_identity is None or (info.st_dev, info.st_ino) != expected_identity:
        raise LifecycleError("LaunchAgent plist changed during scheduler transaction")
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise LifecycleError("LaunchAgent plist identity changed before removal")
    finally:
        os.close(fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise LifecycleError("LaunchAgent plist identity changed before removal")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _run(runner: CommandRunner, argv: Sequence[str]) -> None:
    exact = tuple(argv)
    result = runner.run(exact, shell=False)
    if getattr(result, "returncode", 1) != 0:
        raise LifecycleError("launchctl lifecycle command failed")


def capture_launch_agents(
    home: str | Path, *, loaded: Mapping[str, bool],
) -> dict[str, dict[str, object]]:
    """Capture bounded managed plist state under the lifecycle transaction lock."""
    if (not isinstance(loaded, Mapping) or set(loaded) != set(LABELS)
            or any(type(value) is not bool for value in loaded.values())):
        raise LifecycleError("scheduler loaded state must contain both strict booleans")
    _root, home_fd = _open_home(home)
    library_fd = agents_fd = lock_fd = -1
    try:
        try:
            library_fd = _safe_directory(home_fd, "Library", create=False)
            agents_fd = _safe_directory(library_fd, "LaunchAgents", create=False)
        except FileNotFoundError:
            return {
                label: {"raw": None, "mode": None, "mtime_ns": None,
                        "loaded": loaded[label], "identity": None}
                for label in LABELS
            }
        try:
            lock_fd = os.open(".agentic-stack-scheduler.lock",
                              os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                              dir_fd=agents_fd)
        except FileNotFoundError:
            if any(_read_backup(agents_fd, label + ".plist", False).raw is not None
                   for label in LABELS):
                raise LifecycleError("scheduler transaction lock is missing")
            return {
                label: {"raw": None, "mode": None, "mtime_ns": None,
                        "loaded": loaded[label], "identity": None}
                for label in LABELS
            }
        if not _owner_safe(os.fstat(lock_fd), directory=False):
            raise LifecycleError("scheduler transaction lock is not owner-safe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        result: dict[str, dict[str, object]] = {}
        for label in LABELS:
            backup = _read_backup(agents_fd, label + ".plist", loaded[label])
            result[label] = {
                "raw": backup.raw, "mode": backup.mode,
                "mtime_ns": backup.mtime_ns, "loaded": backup.loaded,
                "identity": backup.identity,
            }
        return result
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if agents_fd >= 0:
            os.close(agents_fd)
        if library_fd >= 0:
            os.close(library_fd)
        os.close(home_fd)


def apply_launch_agents(home: str | Path, generated: Mapping[str, bytes], runner: CommandRunner,
                        *, loaded: Mapping[str, bool] | None = None, uid: int | None = None,
                        launchctl: str = "launchctl", intent: str = "install",
                        expected: Mapping[str, bytes],
                        desired_loaded: Mapping[str, bool] | None = None,
                        desired_metadata: Mapping[str, Mapping[str, object]] | None = None,
                        expected_current_identities: Mapping[str, tuple[int, int]] | None = None,
                        plist_validator: PlistValidator | None = None,
                        plutil: str = "/usr/bin/plutil") -> dict[str, Path]:
    """Atomically replace both plists, then bootout/bootstrap via ``runner``.

    Any failure restores prior bytes, mode, mtime, and the prior loaded-state
    intent. Unknown labels are rejected and never inspected or touched.
    """
    if intent not in {"install", "upgrade", "rollback"}:
        raise LifecycleError("scheduler lifecycle intent is invalid")
    if not isinstance(generated, Mapping) or set(generated) != set(LABELS):
        raise LifecycleError("scheduler lifecycle requires exactly the managed labels")
    if not isinstance(expected, Mapping) or set(expected) != set(LABELS):
        raise LifecycleError("expected scheduler contract requires exactly the managed labels")
    for label, raw in generated.items():
        _validate_generated(label, raw, expected[label])
        _require_contract_paths(expected[label])
        if plist_validator is not None:
            _validate_with_plutil(plist_validator, raw, plutil)
    supplied_loaded = loaded or {}
    if (not isinstance(supplied_loaded, Mapping) or not set(supplied_loaded).issubset(LABELS)
            or any(type(value) is not bool for value in supplied_loaded.values())):
        raise LifecycleError("scheduler loaded state must contain strict booleans")
    prior_loaded = {label: supplied_loaded.get(label, False) for label in LABELS}
    desired = ({label: True for label in LABELS} if desired_loaded is None
               else desired_loaded)
    if (not isinstance(desired, Mapping) or set(desired) != set(LABELS)
            or any(type(value) is not bool for value in desired.values())):
        raise LifecycleError("scheduler desired loaded state must contain both strict booleans")
    metadata = desired_metadata or {}
    if (not isinstance(metadata, Mapping) or not set(metadata).issubset(LABELS)
            or any(not isinstance(value, Mapping)
                   or set(value) != {"mode", "mtime_ns"}
                   or value.get("mode") not in {0o600, 0o644}
                   or type(value.get("mtime_ns")) is not int
                   or value["mtime_ns"] < 0 for value in metadata.values())):
        raise LifecycleError("scheduler desired plist metadata is invalid")
    expected_identities = expected_current_identities
    if (expected_identities is not None
            and (not isinstance(expected_identities, Mapping)
                 or set(expected_identities) != set(LABELS)
                 or any(not isinstance(identity, tuple) or len(identity) != 2
                        or any(type(part) is not int or part < 0 for part in identity)
                        for identity in expected_identities.values()))):
        raise LifecycleError("scheduler expected current identities are invalid")
    effective_uid = os.getuid() if uid is None else uid
    if type(effective_uid) is not int or effective_uid < 0:
        raise LifecycleError("scheduler uid is invalid")
    root, home_fd = _open_home(home)
    library_fd = agents_fd = -1
    backups: dict[str, _Backup] = {}
    installed: dict[str, tuple[int, int]] = {}
    booted_out: set[str] = set()
    bootstrapped: set[str] = set()
    try:
        library_fd = _safe_directory(home_fd, "Library", create=True)
        agents_fd = _safe_directory(library_fd, "LaunchAgents", create=True)
        lock_fd = os.open(".agentic-stack-scheduler.lock",
                          os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                          0o600, dir_fd=agents_fd)
        if not _owner_safe(os.fstat(lock_fd), directory=False):
            raise LifecycleError("scheduler transaction lock is not owner-safe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        for label in LABELS:
            backups[label] = _read_backup(agents_fd, label + ".plist", prior_loaded[label])
            if (expected_identities is not None
                    and backups[label].identity != expected_identities[label]):
                raise LifecycleError("LaunchAgent changed before compensation")
        for label in LABELS:
            installed[label] = _atomic_write(
                agents_fd, label + ".plist", generated[label],
                mode=metadata.get(label, {}).get("mode", 0o644),
                mtime_ns=metadata.get(label, {}).get("mtime_ns"),
                expected_identity=backups[label].identity,
            )
        domain = "gui/" + str(effective_uid)
        for label in LABELS:
            if prior_loaded[label]:
                booted_out.add(label)  # command failure is ambiguous; compensate.
                _run(runner, (launchctl, "bootout", domain + "/" + label))
        for label in LABELS:
            if not desired[label]:
                continue
            path = root / "Library" / "LaunchAgents" / (label + ".plist")
            bootstrapped.add(label)
            _run(runner, (launchctl, "bootstrap", domain, str(path)))
        return {label: root / "Library" / "LaunchAgents" / (label + ".plist") for label in LABELS}
    except BaseException as original:
        rollback_errors: list[BaseException] = []
        restored: dict[str, tuple[int, int]] = {}
        if agents_fd >= 0:
            domain = "gui/" + str(effective_uid)
            for label in reversed(LABELS):
                if label in bootstrapped:
                    try:
                        _run(runner, (launchctl, "bootout", domain + "/" + label))
                    except LifecycleError as exc:
                        rollback_errors.append(exc)
            for label in reversed(LABELS):
                backup = backups.get(label)
                if backup is None:
                    continue
                try:
                    if backup.raw is None:
                        _remove(
                            agents_fd, label + ".plist",
                            expected_identity=installed.get(label),
                        )
                    else:
                        restored[label] = _atomic_write(
                            agents_fd, label + ".plist", backup.raw,
                            mode=backup.mode or 0o600, mtime_ns=backup.mtime_ns,
                            expected_identity=installed.get(label),
                        )
                except (OSError, LifecycleError) as exc:
                    rollback_errors.append(exc)
            for label in LABELS:
                backup = backups.get(label)
                if (backup is not None and backup.loaded and label in booted_out
                        and backup.raw is not None and label in restored):
                    try:
                        path = root / "Library" / "LaunchAgents" / (label + ".plist")
                        if _current_identity(
                            agents_fd, label + ".plist",
                        ) != restored[label]:
                            raise LifecycleError("restored LaunchAgent identity changed")
                        _run(runner, (launchctl, "bootstrap", domain, str(path)))
                    except LifecycleError as exc:
                        rollback_errors.append(exc)
        if rollback_errors:
            raise LifecycleError("LaunchAgent lifecycle failed and rollback was incomplete") from original
        raise LifecycleError("LaunchAgent lifecycle failed; rollback completed") from original
    finally:
        if "lock_fd" in locals():
            os.close(lock_fd)
        if agents_fd >= 0:
            os.close(agents_fd)
        if library_fd >= 0:
            os.close(library_fd)
        os.close(home_fd)


def remove_compatibility_shim(path: str | Path, doctor_evidence: Mapping[str, object]) -> bool:
    """Remove the one-release shim only after injected doctor evidence proves it safe."""
    target = Path(path)
    jobs = doctor_evidence.get("jobs")
    expected_keys = {
        "schema", "versioned_entrypoint_active", "home", "shim_path",
        "runtime_path", "entrypoint", "shim_identity", "jobs",
    }
    if (set(doctor_evidence) != expected_keys
            or doctor_evidence.get("schema") != "agentic.scheduler-doctor.v1"
            or doctor_evidence.get("versioned_entrypoint_active") is not True
            or not isinstance(doctor_evidence.get("home"), str)
            or not isinstance(doctor_evidence.get("runtime_path"), str)
            or not isinstance(doctor_evidence.get("entrypoint"), str)
            or doctor_evidence.get("shim_path") != str(target)
            or target != Path(str(doctor_evidence["home"])) / "Library" / "Scripts" / "agentic_stack_review_notify.py"
            or (not isinstance(doctor_evidence.get("shim_identity"), list)
                or len(doctor_evidence["shim_identity"]) != 2
                or any(type(part) is not int or part < 0
                       for part in doctor_evidence["shim_identity"]))
            or not isinstance(jobs, Mapping) or set(jobs) != set(LABELS)
            or any(not isinstance(jobs[label], Mapping)
                   or set(jobs[label]) != {
                       "plist_valid", "loaded", "healthy", "device", "inode",
                   }
                   or any(jobs[label].get(key) is not True
                          for key in ("plist_valid", "loaded", "healthy"))
                   or any(type(jobs[label].get(key)) is not int
                          or jobs[label][key] < 0 for key in ("device", "inode"))
                   for label in LABELS)):
        return False
    if not target.is_absolute() or "\x00" in str(target):
        raise LifecycleError("compatibility shim path is invalid")
    parent_fd = _open_home(target.parent)[1]
    try:
        info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if not _owner_safe(info, directory=False):
            raise LifecycleError("compatibility shim is not owner-safe")
        if [info.st_dev, info.st_ino] != doctor_evidence["shim_identity"]:
            raise LifecycleError("compatibility shim changed after doctor attestation")
        fd = os.open(target.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise LifecycleError("compatibility shim identity changed")
        finally:
            os.close(fd)
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise LifecycleError("compatibility shim identity changed")
        os.unlink(target.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


def uninstall_launch_agents(home: str | Path, runner: CommandRunner, *, loaded: Mapping[str, bool] | None = None,
                            uid: int | None = None, launchctl: str = "launchctl",
                            compatibility_shim: str | Path | None = None,
                            doctor_evidence: Mapping[str, object] | None = None,
                            expected_current_identities: Mapping[str, tuple[int, int]] | None = None) -> None:
    """Remove only known managed plists and restore them if bootout/remove fails.

    The optional compatibility shim is intentionally retained unless supplied
    injected doctor evidence proves the versioned entrypoint is active.
    """
    supplied_loaded = loaded or {}
    if (not isinstance(supplied_loaded, Mapping) or not set(supplied_loaded).issubset(LABELS)
            or any(type(value) is not bool for value in supplied_loaded.values())):
        raise LifecycleError("scheduler loaded state must contain strict booleans")
    prior_loaded = {label: supplied_loaded.get(label, False) for label in LABELS}
    if (expected_current_identities is not None
            and (not isinstance(expected_current_identities, Mapping)
                 or set(expected_current_identities) != set(LABELS)
                 or any(not isinstance(identity, tuple) or len(identity) != 2
                        for identity in expected_current_identities.values()))):
        raise LifecycleError("scheduler expected current identities are invalid")
    effective_uid = os.getuid() if uid is None else uid
    if type(effective_uid) is not int or effective_uid < 0:
        raise LifecycleError("scheduler uid is invalid")
    root, home_fd = _open_home(home)
    library_fd = agents_fd = -1
    backups: dict[str, _Backup] = {}
    booted_out: set[str] = set()
    removed: set[str] = set()
    try:
        try:
            library_fd = _safe_directory(home_fd, "Library", create=False)
            agents_fd = _safe_directory(library_fd, "LaunchAgents", create=False)
        except FileNotFoundError:
            return
        try:
            lock_fd = os.open(".agentic-stack-scheduler.lock",
                              os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                              dir_fd=agents_fd)
        except FileNotFoundError:
            if all(_read_backup(agents_fd, label + ".plist", False).raw is None for label in LABELS):
                return
            raise LifecycleError("scheduler transaction lock is missing")
        if not _owner_safe(os.fstat(lock_fd), directory=False):
            raise LifecycleError("scheduler transaction lock is not owner-safe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        for label in LABELS:
            backups[label] = _read_backup(agents_fd, label + ".plist", prior_loaded[label])
            if (expected_current_identities is not None
                    and backups[label].identity != expected_current_identities[label]):
                raise LifecycleError("LaunchAgent changed before compensation")
        if compatibility_shim is not None:
            evidence_jobs = (doctor_evidence or {}).get("jobs")
            if not isinstance(evidence_jobs, Mapping):
                raise LifecycleError("scheduler uninstall lacks structured doctor evidence")
            for label in LABELS:
                job = evidence_jobs.get(label)
                backup = backups[label]
                if (not isinstance(job, Mapping) or backup.identity is None
                        or [backup.identity[0], backup.identity[1]]
                        != [job.get("device"), job.get("inode")]):
                    raise LifecycleError("LaunchAgent changed after doctor attestation")
        domain = "gui/" + str(effective_uid)
        for label in LABELS:
            if prior_loaded[label]:
                booted_out.add(label)
                _run(runner, (launchctl, "bootout", domain + "/" + label))
        for label in LABELS:
            _remove(
                agents_fd, label + ".plist",
                expected_identity=backups[label].identity,
            )
            if backups[label].raw is not None:
                removed.add(label)
        if compatibility_shim is not None:
            remove_compatibility_shim(compatibility_shim, doctor_evidence or {})
    except BaseException as original:
        rollback_failed = False
        restored: dict[str, tuple[int, int]] = {}
        if agents_fd >= 0:
            domain = "gui/" + str(effective_uid)
            for label in LABELS:
                backup = backups.get(label)
                if label in removed and backup is not None and backup.raw is not None:
                    try:
                        restored[label] = _atomic_write(
                            agents_fd, label + ".plist", backup.raw,
                            mode=backup.mode or 0o600, mtime_ns=backup.mtime_ns,
                            expected_identity=None,
                        )
                    except (OSError, LifecycleError):
                        rollback_failed = True
            for label in LABELS:
                backup = backups.get(label)
                safe_identity = None
                if backup is not None:
                    safe_identity = (restored.get(label) if label in removed
                                     else backup.identity)
                if (backup is not None and backup.loaded and label in booted_out
                        and backup.raw is not None and safe_identity is not None):
                    try:
                        if _current_identity(
                            agents_fd, label + ".plist",
                        ) != safe_identity:
                            raise LifecycleError("original LaunchAgent identity changed")
                        _run(runner, (launchctl, "bootstrap", domain,
                                      str(root / "Library" / "LaunchAgents" / (label + ".plist"))))
                    except LifecycleError:
                        rollback_failed = True
        if locals().get("rollback_failed", False):
            raise LifecycleError("LaunchAgent uninstall failed and rollback was incomplete") from original
        raise LifecycleError("LaunchAgent uninstall failed; rollback completed") from original
    finally:
        if "lock_fd" in locals():
            os.close(lock_fd)
        if agents_fd >= 0: os.close(agents_fd)
        if library_fd >= 0: os.close(library_fd)
        os.close(home_fd)
