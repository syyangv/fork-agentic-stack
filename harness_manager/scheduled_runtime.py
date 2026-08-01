"""Validated Python runtime records for the two scheduled maintenance jobs.

This module intentionally executes only the selected interpreter in isolated
mode to read ``sys.version_info``.  It never imports, executes, or resolves a
project's deployed ``.agent`` code.
"""
from __future__ import annotations

import os
import ntpath
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


SUPPORTED_PYTHON_MINORS = frozenset(range(9, 15))
_VERSION_RE = re.compile(r"^(3)\.(\d+)\.(\d+)$")
_VERSION_QUERY = "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')"


@dataclass(frozen=True)
class ScheduledRuntime:
    path: str
    version: str

    def record(self) -> dict[str, str]:
        return {"path": self.path, "version": self.version}


def select_runtime(
    candidate: str | Path | None = None, *,
    forbidden_roots: Iterable[str | Path] = (),
) -> ScheduledRuntime:
    """Validate and record one interpreter without executing target code."""
    # The installer itself may run through a package-manager convenience
    # symlink (for example Homebrew's ``opt`` path).  Canonicalize only that
    # trusted default, then validate every component of the persisted path.
    raw = Path(sys.executable).resolve(strict=True) if candidate is None else candidate
    try:
        supplied = Path(raw)
    except TypeError as exc:
        raise ValueError("scheduled Python runtime path must be an absolute path") from exc
    if "\x00" in str(supplied) or not supplied.is_absolute():
        raise ValueError("scheduled Python runtime path must be an absolute path")
    path = Path(os.path.abspath(os.fspath(supplied)))
    _reject_symlink_components(path)
    if _inside_any_root(path, forbidden_roots):
        raise ValueError("scheduled Python runtime must be outside project and deployed code roots")
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("scheduled Python runtime must exist") from exc
    except OSError as exc:
        raise ValueError("scheduled Python runtime cannot be inspected") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("scheduled Python runtime must be a regular executable file")
    major, minor, patch = _python_version(path)
    if major != 3 or minor not in SUPPORTED_PYTHON_MINORS:
        raise ValueError("scheduled Python runtime must be a supported Python 3.9-3.14 interpreter")
    return ScheduledRuntime(path=str(path), version=f"{major}.{minor}.{patch}")


def validate_record_data(record: object) -> ScheduledRuntime:
    """Validate persisted runtime data without executing its referenced path."""
    if not isinstance(record, Mapping) or set(record) != {"path", "version"}:
        raise ValueError("scheduled Python runtime record is malformed")
    path, version = record["path"], record["version"]
    if not isinstance(path, str) or not isinstance(version, str):
        raise ValueError("scheduled Python runtime record is malformed")
    if "\x00" in path or not Path(path).is_absolute():
        raise ValueError("scheduled Python runtime record is malformed")
    if _parse_supported_version(version) is None:
        raise ValueError("scheduled Python runtime record has an unsupported version")
    return ScheduledRuntime(path=path, version=version)


def runtime_from_record(
    record: object, *, forbidden_roots: Iterable[str | Path] = (),
) -> ScheduledRuntime:
    """Fail closed unless a persisted runtime is complete and still consistent."""
    persisted = validate_record_data(record)
    try:
        selected = select_runtime(persisted.path, forbidden_roots=forbidden_roots)
    except ValueError as exc:
        raise ValueError(f"scheduled Python runtime drift: {exc}") from exc
    if selected.version != persisted.version:
        raise ValueError(
            f"scheduled Python runtime version drift: recorded {persisted.version}, observed {selected.version}"
        )
    return selected


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("scheduled Python runtime cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("scheduled Python runtime must not traverse symbolic links")


def _inside_any_root(path: Path, roots: Iterable[str | Path]) -> bool:
    for value in roots:
        try:
            root = Path(os.path.abspath(os.fspath(value)))
        except (TypeError, ValueError):
            continue
        if path == root or root in path.parents:
            return True
    return False


def _python_version(path: Path) -> tuple[int, int, int]:
    """Query only the interpreter's own version in an isolated process."""
    try:
        result = subprocess.run(
            [str(path), "-I", "-c", _VERSION_QUERY], cwd=str(path.anchor),
            env=_version_query_environment(),
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("scheduled Python runtime version could not be read") from exc
    if result.returncode != 0:
        raise ValueError("scheduled Python runtime version could not be read")
    parsed = _parse_supported_version(result.stdout.strip(), permit_unsupported=True)
    if parsed is None:
        raise ValueError("scheduled Python runtime version could not be read")
    return parsed


def _version_query_environment() -> dict[str, str]:
    """Return the minimal environment required by an isolated interpreter."""
    environment = {
        "PATH": os.defpath,
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": tempfile.gettempdir(),
    }
    if os.name == "nt":
        # CreateProcess requires SystemRoot when callers provide a replacement
        # environment, notably for side-by-side assemblies on CPython 3.9.
        environment["SystemRoot"] = _validated_windows_system_root(
            _native_windows_directory(),
        )
    return environment


def _native_windows_directory() -> str:
    """Read the local OS directory from Windows rather than inherited env."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
        if length <= 0 or length >= len(buffer):
            raise ValueError
        return buffer.value
    except (AttributeError, OSError, ValueError) as exc:
        raise ValueError("scheduled Python runtime environment is unavailable") from exc


def _validated_windows_system_root(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("scheduled Python runtime environment is unavailable")
    normalized = ntpath.normpath(value)
    drive, tail = ntpath.splitdrive(normalized)
    if (
        re.fullmatch(r"[A-Za-z]:", drive) is None
        or not tail.startswith(("\\", "/"))
    ):
        raise ValueError("scheduled Python runtime environment is unavailable")
    return normalized


def _parse_supported_version(value: str, *, permit_unsupported: bool = False) -> tuple[int, int, int] | None:
    matched = _VERSION_RE.fullmatch(value)
    if matched is None:
        return None
    parsed = tuple(int(part) for part in matched.groups())
    if not permit_unsupported and (parsed[0] != 3 or parsed[1] not in SUPPORTED_PYTHON_MINORS):
        return None
    return parsed  # type: ignore[return-value]
