"""Deterministic thin LaunchAgent definitions; never activates launchd.

The write helper is deliberately fixture-only.  It uses directory descriptors so
tests of the generated definitions cannot accidentally follow a redirected
``Library/LaunchAgents`` path, and it has no replace/backup/launchctl surface.

Gate 8 validates only the absolute, symlink-free interpreter path shape.  Gate 9
owns selection and executable/version validation.  Likewise, a plist necessarily
stores the versioned entrypoint by pathname; doctor/lifecycle work in Gate 12
must detect any later deployed-path substitution.  Failed fixture writes clean
up files they published, but may retain newly created owner-only ``Library`` and
``LaunchAgents`` fixture directories.
"""
from __future__ import annotations

import os
import plistlib
import stat
import uuid
from pathlib import Path

from . import scheduled_runtime
from .local_schedule_config import load_local_schedule_config


AUTO_DREAM_LABEL = "com.syang.agentic-stack.auto-dream"
REVIEW_NOTIFY_LABEL = "com.syang.agentic-stack.review-notify"
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_PLIST_KEYS = frozenset({
    "Label", "ProgramArguments", "StartCalendarInterval", "RunAtLoad",
    "EnvironmentVariables",
})


def _absolute(value: str | Path, name: str) -> Path:
    path = Path(value)
    if "\x00" in str(path) or not path.is_absolute():
        raise ValueError(f"{name} must be an absolute safe path")
    return path


def _safe_components(path: Path, name: str) -> None:
    """Reject an extant symlink anywhere in an absolute input shape."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"{name} must not traverse symbolic links")


def _require_owner_safe_directory(info: os.stat_result, name: str) -> None:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError(f"{name} is not owner-safe")


def _validate_entrypoint(root: Path) -> Path:
    """Bind the required source entrypoint without following a swapped path."""
    try:
        root_fd = _open_absolute_directory(root)
    except OSError as exc:
        raise ValueError("agent root must contain a real versioned orchestration entrypoint") from exc
    tools_fd = entry_fd = -1
    try:
        tools_fd = os.open("tools", _DIR_FLAGS, dir_fd=root_fd)
        entry_fd = os.open(
            "memory_orchestrate.py", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=tools_fd,
        )
        if not stat.S_ISREG(os.fstat(entry_fd).st_mode):
            raise ValueError("agent root must contain a real versioned orchestration entrypoint")
    except OSError as exc:
        raise ValueError("agent root must contain a real versioned orchestration entrypoint") from exc
    finally:
        if entry_fd >= 0:
            os.close(entry_fd)
        if tools_fd >= 0:
            os.close(tools_fd)
        os.close(root_fd)
    return root / "tools" / "memory_orchestrate.py"


def build_launch_agents(interpreter: str | Path, agent_root: str | Path) -> dict[str, bytes]:
    """Build the two thin, deterministic definitions without writing anything."""
    python = _absolute(interpreter, "interpreter")
    root = _absolute(agent_root, "agent root")
    _safe_components(python, "interpreter")
    _safe_components(root, "agent root")
    entrypoint = _validate_entrypoint(root)
    local_config = load_local_schedule_config(root / "memory/orchestration/scheduled-local.json")
    result: dict[str, bytes] = {}
    for label, schedule, argv in (
        (AUTO_DREAM_LABEL, local_config.maintenance_schedule,
         [str(python), str(entrypoint), "maintain", "--stage-candidates", "--scheduled"]),
        (REVIEW_NOTIFY_LABEL, local_config.review_schedule,
         [str(python), str(entrypoint), "review", "prepare", "--scheduled", "--notify"]),
    ):
        result[label] = plistlib.dumps({
            "Label": label,
            "ProgramArguments": argv,
            "StartCalendarInterval": {"Hour": schedule["hour"], "Minute": schedule["minute"]},
            "RunAtLoad": False,
            "EnvironmentVariables": {"AGENTIC_SCHEDULER_RUN": "1"},
        }, fmt=plistlib.FMT_XML, sort_keys=True)
    return result


def validate_launch_agent_contract(label: str, raw: bytes, expected: bytes) -> dict[str, object]:
    """Validate a generated/deployed plist against the exact configured definition."""
    if label not in {AUTO_DREAM_LABEL, REVIEW_NOTIFY_LABEL}:
        raise ValueError("scheduler label is unmanaged")
    if not isinstance(raw, bytes) or not isinstance(expected, bytes):
        raise ValueError("scheduler plist must be bounded bytes")
    if len(raw) > 64 * 1024 or len(expected) > 64 * 1024:
        raise ValueError("scheduler plist exceeds bound")
    try:
        value = plistlib.loads(raw)
        wanted = plistlib.loads(expected)
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise ValueError("scheduler plist is invalid") from exc
    if (not isinstance(value, dict) or not isinstance(wanted, dict)
            or set(value) != _PLIST_KEYS or value != wanted):
        raise ValueError("scheduler plist drifts from configured contract")
    argv = value.get("ProgramArguments")
    tail = (["maintain", "--stage-candidates", "--scheduled"]
            if label == AUTO_DREAM_LABEL
            else ["review", "prepare", "--scheduled", "--notify"])
    if (value.get("Label") != label or value.get("RunAtLoad") is not False
            or value.get("EnvironmentVariables") != {"AGENTIC_SCHEDULER_RUN": "1"}
            or not isinstance(argv, list) or len(argv) != len(tail) + 2
            or argv[2:] != tail
            or any(not isinstance(part, str) or not part or "\x00" in part for part in argv)
            or not Path(argv[0]).is_absolute() or not Path(argv[1]).is_absolute()
            or not argv[1].endswith("/tools/memory_orchestrate.py")):
        raise ValueError("scheduler plist is not an exact thin launcher")
    schedule = value.get("StartCalendarInterval")
    if (not isinstance(schedule, dict) or set(schedule) != {"Hour", "Minute"}
            or type(schedule["Hour"]) is not int or type(schedule["Minute"]) is not int
            or not 0 <= schedule["Hour"] <= 23 or not 0 <= schedule["Minute"] <= 59):
        raise ValueError("scheduler schedule is invalid")
    return value


def build_launch_agents_from_state(install_state: object, agent_root: str | Path) -> dict[str, bytes]:
    """Build both definitions from the one validated installer runtime record."""
    if not isinstance(install_state, dict):
        raise ValueError("install-state is malformed")
    orchestration = install_state.get("orchestration")
    if not isinstance(orchestration, dict):
        raise ValueError("installation profile is missing or malformed")
    root = _absolute(agent_root, "agent root")
    runtime = scheduled_runtime.runtime_from_record(
        orchestration.get("scheduled_runtime"),
        forbidden_roots=(root.parent,),
    )
    return build_launch_agents(runtime.path, root)


def _build_review_compatibility_shim(
    interpreter: str | Path, agent_root: str | Path,
) -> bytes:
    """Generate the one-release legacy-path adapter without writing it anywhere.

    Gate 12 owns installation, activation, doctor confirmation, and eventual
    removal.  This adapter has one fixed exec target and deliberately carries
    no review policy or local-service behavior.
    """
    python = _absolute(interpreter, "interpreter")
    root = _absolute(agent_root, "agent root")
    _safe_components(python, "interpreter")
    _safe_components(root, "agent root")
    entrypoint = _validate_entrypoint(root)
    argv = [str(python), str(entrypoint), "review", "prepare", "--scheduled", "--notify"]
    source = (
        '"""One-release compatibility adapter for the versioned review command."""\n'
        "import os\n\n"
        "def main():\n"
        f"    os.execv({str(python)!r}, {argv!r})\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    return source.encode("utf-8")


def build_review_compatibility_shim(
    interpreter: str | Path, agent_root: str | Path,
) -> bytes:
    """Public data-only builder for doctor/lifecycle integration."""
    return _build_review_compatibility_shim(interpreter, agent_root)


def build_review_compatibility_shim_from_state(
    install_state: object, agent_root: str | Path,
) -> bytes:
    """Generate the shim from the same validated runtime record as both jobs."""
    if not isinstance(install_state, dict):
        raise ValueError("install-state is malformed")
    orchestration = install_state.get("orchestration")
    if not isinstance(orchestration, dict):
        raise ValueError("installation profile is missing or malformed")
    root = _absolute(agent_root, "agent root")
    runtime = scheduled_runtime.runtime_from_record(
        orchestration.get("scheduled_runtime"),
        forbidden_roots=(root.parent,),
    )
    return _build_review_compatibility_shim(runtime.path, root)


def _open_absolute_directory(path: Path) -> int:
    """Open an absolute directory component-by-component without following links."""
    descriptor = os.open(path.anchor, _DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_dir(parent_fd: int, name: str) -> tuple[int, bool]:
    """Open (or safely create) one owner-safe directory below ``parent_fd``."""
    created = False
    try:
        descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        _require_owner_safe_directory(os.fstat(descriptor), "LaunchAgent directory")
        return descriptor, created
    except BaseException:
        os.close(descriptor)
        raise


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short launcher write")
        view = view[count:]


def _matches_identity(parent_fd: int, name: str, identity: tuple[int, int]) -> bool:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity


def _unlink_if_identity(parent_fd: int, name: str, identity: tuple[int, int]) -> None:
    """Never unlink a replacement an attacker put under a temporary/final name."""
    if _matches_identity(parent_fd, name, identity):
        os.unlink(name, dir_fd=parent_fd)


def _publish_temp(agents_fd: int, temp: str, final: str, identity: tuple[int, int]) -> None:
    if not _matches_identity(agents_fd, temp, identity):
        raise OSError("launcher temporary identity changed")
    # link(2) is an atomic no-replace publication for files in this directory.
    os.link(temp, final, src_dir_fd=agents_fd, dst_dir_fd=agents_fd, follow_symlinks=False)
    if not _matches_identity(agents_fd, final, identity):
        raise OSError("launcher publication identity changed")


def write_launch_agents(home: str | Path, interpreter: str | Path, agent_root: str | Path) -> dict[str, Path]:
    """Fixture-only no-clobber write. Activation/backup are later-gate concerns."""
    # Validate all definition inputs before creating ``Library`` or any temporary.
    outputs = build_launch_agents(interpreter, agent_root)
    root = _absolute(home, "fixture home")
    _safe_components(root, "fixture home")
    home_fd = _open_absolute_directory(root)
    library_fd = agents_fd = -1
    staged: dict[str, tuple[str, int, tuple[int, int]]] = {}
    published: dict[str, tuple[str, tuple[int, int]]] = {}
    try:
        _require_owner_safe_directory(os.fstat(home_fd), "fixture home")
        library_fd, library_created = _open_dir(home_fd, "Library")
        if library_created:
            os.fsync(home_fd)
        agents_fd, agents_created = _open_dir(library_fd, "LaunchAgents")
        if agents_created:
            os.fsync(library_fd)
        # Preflight both before staging either: Gate 12 owns replacement/rollback.
        for label in outputs:
            try:
                os.stat(f"{label}.plist", dir_fd=agents_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError(f"refusing to replace existing {label}.plist")
        for label, raw in outputs.items():
            temp = f".{label}.{uuid.uuid4().hex}.tmp"
            fd = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=agents_fd,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("launcher temporary is not regular")
                staged[label] = (temp, fd, (info.st_dev, info.st_ino))
                fd = -1  # retained until publication/identity-safe cleanup.
                staged_fd = staged[label][1]
                os.fchmod(staged_fd, 0o600)
                _write_all(staged_fd, raw)
                os.fsync(staged_fd)
            finally:
                if fd >= 0:
                    os.close(fd)
        for label, (temp, fd, identity) in staged.items():
            final = f"{label}.plist"
            _publish_temp(agents_fd, temp, final, identity)
            published[label] = (final, identity)
        os.fsync(agents_fd)
        return {
            label: root / "Library" / "LaunchAgents" / f"{label}.plist"
            for label in outputs
        }
    except BaseException:
        # Only remove files whose inode is one we created; never delete a race winner.
        if agents_fd >= 0:
            for final, identity in published.values():
                try:
                    _unlink_if_identity(agents_fd, final, identity)
                except OSError:
                    pass
            # Make successful rollback unlinks durable without hiding the
            # original publication failure if directory fsync itself fails.
            try:
                os.fsync(agents_fd)
            except OSError:
                pass
        raise
    finally:
        if agents_fd >= 0:
            for temp, fd, identity in staged.values():
                try:
                    try:
                        _unlink_if_identity(agents_fd, temp, identity)
                    except OSError:
                        # Cleanup never follows/replaces another actor's file; a
                        # harmless residue is safer than a broad retry here.
                        pass
                finally:
                    os.close(fd)
            os.close(agents_fd)
        if library_fd >= 0:
            os.close(library_fd)
        os.close(home_fd)
