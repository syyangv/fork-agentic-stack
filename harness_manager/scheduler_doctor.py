"""Read-only scheduler diagnostics for injected fixtures or an explicit home."""
from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import re
import stat
from pathlib import Path
from typing import Mapping

from .scheduled_launchers import (
    AUTO_DREAM_LABEL, REVIEW_NOTIFY_LABEL, validate_launch_agent_contract,
)

LABELS = (AUTO_DREAM_LABEL, REVIEW_NOTIFY_LABEL)

GREEN, YELLOW, RED = 0, 1, 2
_EXPECTED = {
    AUTO_DREAM_LABEL: (3, 0, ("maintain", "--stage-candidates", "--scheduled")),
    REVIEW_NOTIFY_LABEL: (9, 0, ("review", "prepare", "--scheduled", "--notify")),
}
_HEALTH_KEYS = frozenset({"schema", "label", "status", "started_at", "completed_at", "duration_ms",
                          "tool_version", "source_revision", "candidate_count", "rejection_count",
                          "notification", "run_token"})
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_LIMIT = 64 * 1024
_VERSION_QUERY = "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')"


def _time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def _valid_health(value: object, label: str, now: dt.datetime) -> tuple[bool, str]:
    if not isinstance(value, Mapping) or set(value) != _HEALTH_KEYS:
        return False, "health invalid"
    if value.get("schema") != "agentic.scheduler-health.v1" or value.get("label") != label:
        return False, "health invalid"
    if value.get("status") not in {"running", "success", "failure"} or value.get("notification") not in {"not_requested", "deferred", "sent", "failed"}:
        return False, "health invalid"
    started, completed = _time(value.get("started_at")), _time(value.get("completed_at"))
    if (started is None or started > now + dt.timedelta(minutes=5)
            or not isinstance(value.get("tool_version"), str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value["tool_version"])
            or not isinstance(value.get("source_revision"), str)
            or not re.fullmatch(r"(?:unknown|[0-9a-f]{7,64})", value["source_revision"])
            or not isinstance(value.get("run_token"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["run_token"])):
        return False, "health invalid"
    if any(value.get(key) is not None and
           (type(value.get(key)) is not int or not 0 <= value[key] <= 1_000_000)
           for key in ("duration_ms", "candidate_count", "rejection_count")):
        return False, "health invalid"
    if value.get("status") == "running":
        if completed is not None or value.get("duration_ms") is not None:
            return False, "health invalid"
        return True, "health running"
    if (completed is None or completed < started or completed > now + dt.timedelta(minutes=5)
            or type(value.get("duration_ms")) is not int):
        return False, "health invalid"
    if value.get("status") == "failure":
        return True, "health failure"
    if now - completed > dt.timedelta(hours=30):
        return True, "health stale"
    return True, "health fresh"


def audit_scheduler_fixture(fixture: Mapping[str, object], *, now: str) -> tuple[int, list[str]]:
    """Validate only supplied plist bytes and injected launchd observations."""
    observations = fixture.get("observations") if isinstance(fixture, Mapping) else None
    plists = fixture.get("plists") if isinstance(fixture, Mapping) else None
    runtime = fixture.get("runtime") if isinstance(fixture, Mapping) else None
    expected_plists = fixture.get("expected_plists") if isinstance(fixture, Mapping) else None
    current = _time(now)
    if (not isinstance(observations, Mapping) or not isinstance(plists, Mapping) or not isinstance(runtime, Mapping)
            or not isinstance(runtime.get("path"), str) or not re.fullmatch(r"3\.(?:9|1[0-4])\.\d+", str(runtime.get("version"))) or current is None):
        return RED, ["scheduler fixture malformed"]
    status, lines = GREEN, []
    for label in LABELS:
        raw, observed = plists.get(label), observations.get(label)
        if not isinstance(raw, bytes) or not isinstance(observed, Mapping):
            status = RED; lines.append(label + ": missing fixture"); continue
        try:
            parsed = plistlib.loads(raw)
            expected_raw = expected_plists.get(label) if isinstance(expected_plists, Mapping) else None
            expected_parsed = plistlib.loads(expected_raw) if isinstance(expected_raw, bytes) else None
            if expected_parsed is not None:
                validate_launch_agent_contract(label, raw, expected_raw)
            hour, minute, argv_tail = _EXPECTED[label]
            valid = (isinstance(parsed, dict) and parsed.get("Label") == label and
                     (parsed == expected_parsed if expected_parsed is not None else
                      parsed.get("StartCalendarInterval") == {"Hour": hour, "Minute": minute}) and
                     set(parsed) == {"Label", "ProgramArguments", "StartCalendarInterval",
                                     "RunAtLoad", "EnvironmentVariables"} and
                     parsed.get("RunAtLoad") is False and
                     parsed.get("EnvironmentVariables") == {"AGENTIC_SCHEDULER_RUN": "1"} and
                     isinstance(parsed.get("ProgramArguments"), list) and
                     len(parsed["ProgramArguments"]) == 2 + len(argv_tail) and
                     parsed["ProgramArguments"][0] == runtime["path"] and
                     tuple(parsed["ProgramArguments"][-len(argv_tail):]) == argv_tail and
                     "/tools/memory_orchestrate.py" in str(parsed["ProgramArguments"][1]) and
                     not any("accept" in str(part).lower() or "graduate" in str(part).lower() for part in parsed["ProgramArguments"]))
        except (ValueError, plistlib.InvalidFileException, IndexError):
            valid = False
        if not valid:
            status = RED; lines.append(label + ": generated plist invalid or acceptance path present"); continue
        if observed.get("owner_uid") != __import__("os").getuid() or observed.get("mode") not in {0o600, 0o644}:
            status = RED; lines.append(label + ": plist permissions invalid")
        loaded, exit_code = observed.get("loaded"), observed.get("last_exit")
        if type(loaded) is not bool or type(exit_code) is not int:
            status = RED; lines.append(label + ": launchd observation invalid")
        else:
            lines.append(label + (": loaded last-exit=0" if loaded and exit_code == 0 else ": launchd degraded"))
            if not loaded or exit_code != 0:
                status = max(status, YELLOW)
        valid_health, message = _valid_health(observed.get("health"), label, current)
        if not valid_health:
            status = RED
        elif (message.endswith("stale") or message.endswith("running")
              or message.endswith("failure")):
            status = max(status, YELLOW)
        lines.append(label + ": " + message)
    return status, lines


def _open_directory(path: Path) -> int:
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("scheduler path is invalid")
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


def _read_bounded(path: Path, *, limit: int = _READ_LIMIT) -> tuple[bytes, os.stat_result]:
    parent_fd = _open_directory(path.parent)
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022):
            raise ValueError("scheduler file is not an owner-safe regular file")
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("scheduler file identity changed")
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, min(8192, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk); size += len(chunk)
                if size > limit:
                    raise ValueError("scheduler file exceeds bound")
        finally:
            os.close(fd)
        after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("scheduler file identity changed")
        return b"".join(chunks), opened
    finally:
        os.close(parent_fd)


def _regular_path(path: Path, *, executable: bool = False) -> bool:
    parent_fd = -1
    try:
        parent_fd = _open_directory(path.parent)
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
                return False
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return False
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    return (stat.S_ISREG(info.st_mode) and info.st_uid in {0, os.getuid()}
            and not info.st_mode & 0o022
            and (not executable or bool(info.st_mode & 0o111)))


def _launchd_state(runner: object, launchctl: str, uid: int, label: str) -> tuple[bool, int]:
    result = runner.run((launchctl, "print", f"gui/{uid}/{label}"), shell=False)
    output = getattr(result, "stdout", b"")
    error = getattr(result, "stderr", b"")
    if isinstance(output, str):
        output = output.encode("utf-8", "strict")
    if isinstance(error, str):
        error = error.encode("utf-8", "strict")
    if (not isinstance(output, bytes) or not isinstance(error, bytes)
            or len(output) > _READ_LIMIT or len(error) > _READ_LIMIT):
        raise ValueError("launchctl observation exceeds bound")
    if getattr(result, "returncode", 1) != 0:
        try:
            error_text = error.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ValueError("launchctl observation is malformed") from exc
        bound_domain = (f"gui/{uid}" in error_text
                        or re.search(rf"(?i)\bgui\s*:\s*{uid}\b", error_text))
        if (getattr(result, "returncode", None) == 113
                and re.search(r"(?i)could not find service", error_text)
                and label in error_text and bound_domain):
            return False, 0
        raise ValueError("launchctl observation failed without not-found attestation")
    text = output.decode("utf-8", "strict")
    state_match = re.search(r"(?mi)^\s*state\s*=\s*([a-z-]+)\s*$", text)
    match = re.search(r"(?mi)^\s*last exit (?:code|status)\s*=\s*(-?\d+)\s*$", text)
    if state_match is None or match is None or state_match.group(1) not in {
        "running", "waiting", "exited", "spawned",
    }:
        raise ValueError("launchctl observation is malformed")
    last_exit = int(match.group(1))
    if not -(2 ** 31) <= last_exit < 2 ** 31:
        raise ValueError("launchctl last exit is invalid")
    return True, last_exit


def observe_loaded_state(
    runner: object, *, launchctl: str, uid: int,
) -> dict[str, bool]:
    """Collect exact managed-label loaded intent before any mutation."""
    if type(uid) is not int or uid < 0 or not Path(launchctl).is_absolute():
        raise ValueError("scheduler launchctl observation inputs are invalid")
    return {
        label: _launchd_state(runner, launchctl, uid, label)[0]
        for label in LABELS
    }


def _runtime_version(runner: object, path: str, expected: str) -> bool:
    result = runner.run((path, "-I", "-c", _VERSION_QUERY), shell=False)
    output = getattr(result, "stdout", b"")
    if isinstance(output, str):
        output = output.encode("utf-8", "strict")
    if (getattr(result, "returncode", 1) != 0 or not isinstance(output, bytes)
            or len(output) > 64 or b"\x00" in output):
        return False
    try:
        return output.decode("ascii").strip() == expected
    except UnicodeDecodeError:
        return False


def collect_scheduler_attestation(
    *, target_root: Path, home: Path, expected_plists: Mapping[str, bytes],
    runtime: Mapping[str, object], expected_shim: bytes, runner: object,
    launchctl: str, uid: int, now: str,
) -> tuple[int, list[str], dict[str, object]]:
    """Read exact deployed jobs and health, then collect bounded launchd output."""
    if (set(expected_plists) != set(LABELS) or not isinstance(runtime, Mapping)
            or set(runtime) != {"path", "version"} or type(uid) is not int or uid < 0):
        raise ValueError("scheduler doctor inputs are malformed")
    runtime_path = runtime.get("path")
    if not isinstance(runtime_path, str) or not isinstance(runtime.get("version"), str):
        raise ValueError("scheduler runtime record is malformed")
    parsed_expected = plistlib.loads(expected_plists[AUTO_DREAM_LABEL])
    entrypoint = parsed_expected.get("ProgramArguments", [None, None])[1]
    if not isinstance(entrypoint, str):
        raise ValueError("scheduler entrypoint is malformed")
    observations: dict[str, object] = {}
    plists: dict[str, bytes] = {}
    jobs: dict[str, dict[str, object]] = {}
    preliminary_status, preliminary_lines = GREEN, []
    try:
        paths_ok = (_regular_path(Path(runtime_path), executable=True)
                    and _regular_path(Path(entrypoint))
                    and _runtime_version(runner, runtime_path, runtime["version"]))
    except Exception:
        paths_ok = False
    if not paths_ok:
        preliminary_status = RED
        preliminary_lines.append("scheduler runtime or versioned entrypoint unavailable")
    for label in LABELS:
        plist_path = home / "Library" / "LaunchAgents" / f"{label}.plist"
        health_path = target_root / ".agent" / "runtime" / "scheduler-health" / f"{label}.json"
        try:
            raw, info = _read_bounded(plist_path)
            plists[label] = raw
            plist_valid = raw == expected_plists[label]
        except (OSError, ValueError):
            raw, info, plist_valid = b"", None, False
            plists[label] = raw
        try:
            health_raw, _health_info = _read_bounded(health_path, limit=4096)
            health = json.loads(health_raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            health = {}
        try:
            loaded, last_exit = _launchd_state(runner, launchctl, uid, label)
        except Exception:
            loaded, last_exit = False, -1
            preliminary_status = RED
            preliminary_lines.append(label + ": launchctl observation failed")
        observations[label] = {
            "loaded": loaded, "last_exit": last_exit,
            "mode": stat.S_IMODE(info.st_mode) if info else None,
            "owner_uid": info.st_uid if info else None,
            "health": health,
        }
        health_valid, health_message = _valid_health(
            health, label, _time(now) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        )
        healthy = health_valid and health_message == "health fresh" and last_exit == 0
        jobs[label] = {
            "plist_valid": plist_valid, "loaded": loaded, "healthy": healthy,
            "device": info.st_dev if info else None,
            "inode": info.st_ino if info else None,
        }
    fixture = {
        "runtime": dict(runtime), "plists": plists, "expected_plists": dict(expected_plists),
        "observations": observations,
    }
    status, lines = audit_scheduler_fixture(fixture, now=now)
    status = max(status, preliminary_status)
    lines = preliminary_lines + lines
    shim_path = home / "Library" / "Scripts" / "agentic_stack_review_notify.py"
    shim_identity = None
    try:
        shim_raw, shim_info = _read_bounded(shim_path)
        shim_identity = [shim_info.st_dev, shim_info.st_ino]
        if shim_raw != expected_shim:
            status = RED
            lines.append("unsafe legacy scheduler shim detected")
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        status = RED
        lines.append("unsafe legacy scheduler shim detected")
    active = paths_ok and status != RED and all(
        item["plist_valid"] and item["loaded"] and item["healthy"] for item in jobs.values()
    )
    evidence: dict[str, object] = {
        "schema": "agentic.scheduler-doctor.v1",
        "versioned_entrypoint_active": active,
        "home": str(home),
        "shim_path": str(shim_path),
        "runtime_path": runtime_path,
        "entrypoint": entrypoint,
        "shim_identity": shim_identity,
        "jobs": jobs,
    }
    return status, lines, evidence
