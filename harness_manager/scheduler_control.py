"""Explicit user-level scheduler lifecycle integration.

This is the only production wiring between the CLI and the fixture-oriented
Gate 12 lifecycle.  It never guesses a home: callers must select an absolute
home and the installation target must be that same directory.
"""
from __future__ import annotations

import os
import stat
import subprocess
import base64
import json
import uuid
import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Mapping

from . import scheduled_launchers, scheduled_runtime, scheduler_doctor, scheduler_lifecycle, state

_ROLLBACK_SCHEMA = "agentic.scheduler-rollback.v1"
_ROLLBACK_MAX = 256 * 1024


def _read_optional(path: Path, limit: int = 64 * 1024) -> dict[str, object]:
    try:
        raw, info = scheduler_doctor._read_bounded(path, limit=limit)
    except FileNotFoundError:
        return {"raw": None, "mode": None, "mtime_ns": None, "identity": None}
    return {
        "raw": raw, "mode": stat.S_IMODE(info.st_mode),
        "mtime_ns": info.st_mtime_ns, "identity": (info.st_dev, info.st_ino),
    }


def _encode_snapshot(jobs: Mapping[str, Mapping[str, object]],
                     config: Mapping[str, object], *,
                     target: Path, home: Path, uid: int) -> bytes:
    def item(value: Mapping[str, object], *, loaded: bool = False) -> dict[str, object]:
        raw = value.get("raw")
        result = {
            "data": None if raw is None else base64.b64encode(raw).decode("ascii"),
            "mode": value.get("mode"), "mtime_ns": value.get("mtime_ns"),
            "sha256": None if raw is None else hashlib.sha256(raw).hexdigest(),
        }
        if loaded:
            result["loaded"] = value.get("loaded")
        return result
    document = {
        "schema": _ROLLBACK_SCHEMA,
        "target": str(target), "home": str(home), "uid": uid,
        "jobs": {label: item(jobs[label], loaded=True)
                 for label in scheduler_lifecycle.LABELS},
        "config": item(config),
    }
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > _ROLLBACK_MAX:
        raise ValueError("scheduler rollback state exceeds bound")
    return raw


def _decode_snapshot(raw: bytes, *, target: Path, home: Path,
                     uid: int) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    if len(raw) > _ROLLBACK_MAX:
        raise ValueError("scheduler rollback state exceeds bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("scheduler rollback state is invalid") from exc
    if (not isinstance(value, dict)
            or set(value) != {"schema", "target", "home", "uid", "jobs", "config"}
            or value.get("schema") != _ROLLBACK_SCHEMA
            or value.get("target") != str(target) or value.get("home") != str(home)
            or value.get("uid") != uid
            or not isinstance(value.get("jobs"), dict)
            or set(value["jobs"]) != set(scheduler_lifecycle.LABELS)):
        raise ValueError("scheduler rollback state is invalid")
    def decode(item: object, *, loaded: bool = False) -> dict[str, object]:
        keys = {"data", "mode", "mtime_ns", "sha256"} | ({"loaded"} if loaded else set())
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError("scheduler rollback state is invalid")
        data, mode, mtime, digest = (
            item["data"], item["mode"], item["mtime_ns"], item["sha256"],
        )
        if data is None:
            if mode is not None or mtime is not None or digest is not None:
                raise ValueError("scheduler rollback state is invalid")
            decoded = None
        else:
            if (not isinstance(data, str) or len(data) > 128 * 1024
                    or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or type(mode) is not int or mode not in {0o600, 0o644}
                    or type(mtime) is not int or mtime < 0):
                raise ValueError("scheduler rollback state is invalid")
            try:
                decoded = base64.b64decode(data, validate=True)
            except Exception as exc:
                raise ValueError("scheduler rollback state is invalid") from exc
            if len(decoded) > 64 * 1024:
                raise ValueError("scheduler rollback state exceeds bound")
            if hashlib.sha256(decoded).hexdigest() != digest:
                raise ValueError("scheduler rollback state digest mismatch")
        result = {"raw": decoded, "mode": mode, "mtime_ns": mtime}
        if loaded:
            if type(item["loaded"]) is not bool:
                raise ValueError("scheduler rollback state is invalid")
            result["loaded"] = item["loaded"]
        return result
    jobs = {label: decode(value["jobs"][label], loaded=True)
            for label in scheduler_lifecycle.LABELS}
    config = decode(value.get("config"))
    return jobs, config


def _atomic_owner_file(path: Path, raw: bytes, *, mode: int = 0o600,
                       mtime_ns: int | None = None,
                       expected_identity: tuple[int, int] | None | object = ...) -> None:
    parent_fd = scheduler_doctor._open_directory(path.parent)
    temporary = "." + path.name + "." + uuid.uuid4().hex + ".tmp"
    fd = -1
    try:
        try:
            before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if expected_identity is not ...:
            actual_identity = None if before is None else (before.st_dev, before.st_ino)
            if actual_identity != expected_identity:
                raise ValueError("scheduler rollback/config file changed concurrently")
        if before is not None and (not stat.S_ISREG(before.st_mode)
                                   or before.st_uid != os.getuid()
                                   or stat.S_IMODE(before.st_mode) not in {0o600, 0o644}):
            raise ValueError("scheduler rollback/config file is not owner-safe")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                     getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
        os.fchmod(fd, mode)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("scheduler state write made no progress")
            view = view[count:]
        os.fsync(fd); os.close(fd); fd = -1
        current = None
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        if ((before is None) != (current is None)
                or (before is not None and current is not None and
                    (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino))):
            raise ValueError("scheduler rollback/config file changed concurrently")
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        if mtime_ns is not None:
            os.utime(path.name, ns=(mtime_ns, mtime_ns), dir_fd=parent_fd,
                     follow_symlinks=False)
        os.fsync(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _restore_optional(path: Path, previous: Mapping[str, object],
                      expected_current: bytes,
                      expected_identity: tuple[int, int] | None | object = ...) -> None:
    current = _read_optional(path, limit=_ROLLBACK_MAX)
    if (current.get("raw") != expected_current
            or (expected_identity is not ...
                and current.get("identity") != expected_identity)):
        raise ValueError("scheduler rollback state changed concurrently")
    if previous.get("raw") is not None:
        _atomic_owner_file(path, previous["raw"], mode=previous["mode"],
                           mtime_ns=previous["mtime_ns"],
                           expected_identity=current.get("identity"))
        return
    parent_fd = scheduler_doctor._open_directory(path.parent)
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) not in {0o600, 0o644}
                or (expected_identity is not ...
                    and (before.st_dev, before.st_ino) != expected_identity)):
            raise ValueError("scheduler rollback state is not owner-safe")
        fd = os.open(
            path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(fd)
            if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.getuid()):
                raise ValueError("scheduler rollback state identity changed")
        finally:
            os.close(fd)
        if _lstat_identity(parent_fd, path.name) != (before.st_dev, before.st_ino):
            raise ValueError("scheduler rollback state identity changed")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _restore_publication(path: Path, previous: Mapping[str, object],
                         attempted: bytes) -> None:
    current = _read_optional(path, limit=_ROLLBACK_MAX)
    if current.get("raw") == previous.get("raw"):
        return
    _restore_optional(path, previous, attempted)


def _lstat_identity(parent_fd: int, name: str) -> tuple[int, int]:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    return info.st_dev, info.st_ino


def _compensate_live(
    home: Path, snapshot: Mapping[str, Mapping[str, object]], *,
    expected_current: Mapping[str, Mapping[str, object]],
    currently_loaded: Mapping[str, bool], runner: object, uid: int,
    launchctl: str, validator: object, plutil: str,
) -> None:
    observed = scheduler_lifecycle.capture_launch_agents(
        home, loaded=currently_loaded,
    )
    for label in scheduler_lifecycle.LABELS:
        if any(observed[label].get(key) != expected_current[label].get(key)
               for key in ("raw", "mode", "mtime_ns", "identity")):
            raise scheduler_lifecycle.LifecycleError(
                "scheduler live plist changed before compensation",
            )
    present = [snapshot[label].get("raw") is not None
               for label in scheduler_lifecycle.LABELS]
    if all(present):
        generated = {
            label: snapshot[label]["raw"]
            for label in scheduler_lifecycle.LABELS
        }
        desired = {
            label: snapshot[label]["loaded"]
            for label in scheduler_lifecycle.LABELS
        }
        metadata = {
            label: {
                "mode": snapshot[label]["mode"],
                "mtime_ns": snapshot[label]["mtime_ns"],
            }
            for label in scheduler_lifecycle.LABELS
        }
        scheduler_lifecycle.apply_launch_agents(
            home, generated, runner, loaded=currently_loaded, uid=uid,
            launchctl=launchctl, intent="rollback", expected=generated,
            desired_loaded=desired, plist_validator=validator, plutil=plutil,
            desired_metadata=metadata,
            expected_current_identities={
                label: expected_current[label]["identity"]
                for label in scheduler_lifecycle.LABELS
            },
        )
        return
    if not any(present):
        scheduler_lifecycle.uninstall_launch_agents(
            home, runner, loaded=currently_loaded, uid=uid,
            launchctl=launchctl,
            expected_current_identities={
                label: expected_current[label]["identity"]
                for label in scheduler_lifecycle.LABELS
            },
        )
        return
    raise scheduler_lifecycle.LifecycleError(
        "scheduler compensation snapshot is incomplete",
    )


class SubprocessRunner:
    """Bounded exact-argv runner shared by launchctl, plutil, and doctor."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def run(self, argv: tuple[str, ...], *, shell: bool, input: bytes | None = None):
        if shell is not False or not argv or any(not isinstance(v, str) or "\x00" in v for v in argv):
            raise scheduler_lifecycle.LifecycleError("scheduler command shape is invalid")
        try:
            result = subprocess.run(
                list(argv), shell=False, check=False, input=input,
                capture_output=True, timeout=self.timeout,
            )
            if (len(result.stdout or b"") > 64 * 1024
                    or len(result.stderr or b"") > 64 * 1024):
                raise scheduler_lifecycle.LifecycleError("scheduler command output exceeds bound")
            return result
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise scheduler_lifecycle.LifecycleError("scheduler command failed") from exc


def validate_user_target(target: str | Path, home: str | Path) -> tuple[Path, Path]:
    """Require one explicit user-level installation rooted at its selected home."""
    target_path, home_path = Path(target), Path(home)
    if (not target_path.is_absolute() or not home_path.is_absolute()
            or "\x00" in str(target_path) or "\x00" in str(home_path)
            or os.path.abspath(target_path) != os.path.abspath(home_path)):
        raise ValueError("scheduler target must be the explicitly selected user home")
    descriptor = os.open(home_path.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    try:
        for component in home_path.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                              | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise ValueError("scheduler home is not an owner-safe user directory")
    except OSError as exc:
        raise ValueError("scheduler home must not traverse symbolic links") from exc
    finally:
        os.close(descriptor)
    return target_path, home_path


def run_lifecycle(
    action: str, *, target: str | Path, home: str | Path, yes: bool,
    runner: object | None = None, plist_validator: object | None = None,
    launchctl: str = "/bin/launchctl", plutil: str = "/usr/bin/plutil",
    uid: int | None = None, loaded: Mapping[str, bool] | None = None,
) -> dict[str, object]:
    """Apply one explicitly confirmed scheduler action."""
    if yes is not True:
        raise ValueError("scheduler lifecycle requires explicit --yes")
    if action not in {"install", "upgrade", "rollback", "uninstall"}:
        raise ValueError("scheduler action must be install, upgrade, rollback, or uninstall")
    target_path, home_path = validate_user_target(target, home)
    if not Path(launchctl).is_absolute() or not Path(plutil).is_absolute():
        raise ValueError("scheduler tools must use absolute paths")
    command_runner = runner or SubprocessRunner()
    validator = plist_validator or command_runner
    effective_uid = os.getuid() if uid is None else uid
    if loaded is None:
        loaded = scheduler_doctor.observe_loaded_state(
            command_runner, launchctl=launchctl, uid=effective_uid,
        )
    if action == "uninstall":
        _status, _lines, doctor_evidence = collect_doctor(
            target=target_path, home=home_path, runner=command_runner,
            launchctl=launchctl, uid=effective_uid,
            now=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )
        shim = home_path / "Library" / "Scripts" / "agentic_stack_review_notify.py"
        scheduler_lifecycle.uninstall_launch_agents(
            home_path, command_runner, loaded=loaded, uid=effective_uid, launchctl=launchctl,
            compatibility_shim=shim, doctor_evidence=doctor_evidence,
        )
        return {"action": action, "home": str(home_path), "jobs": {}}
    document = state.load(target_path)
    if not isinstance(document, dict):
        raise ValueError("scheduler lifecycle requires valid install state")
    agent_root = target_path / ".agent"
    rollback_path = agent_root / "runtime" / "scheduler-rollback.json"
    active_path = agent_root / "runtime" / "scheduler-active.json"
    config_path = agent_root / "memory" / "orchestration" / "scheduled-local.json"
    desired_loaded = None
    if action == "rollback":
        current_jobs = scheduler_lifecycle.capture_launch_agents(
            home_path, loaded=loaded,
        )
        prior_active = _read_optional(active_path, limit=_ROLLBACK_MAX)
        rollback_raw, _ = scheduler_doctor._read_bounded(
            rollback_path, limit=_ROLLBACK_MAX,
        )
        jobs, prior_config = _decode_snapshot(
            rollback_raw, target=target_path, home=home_path, uid=effective_uid,
        )
        if any(jobs[label]["raw"] is None for label in scheduler_lifecycle.LABELS):
            raise ValueError("scheduler rollback state predates a complete managed installation")
        generated = {
            label: jobs[label]["raw"] for label in scheduler_lifecycle.LABELS
        }
        desired_loaded = {
            label: jobs[label]["loaded"] for label in scheduler_lifecycle.LABELS
        }
        current_config = _read_optional(config_path)
        published_config = None
        publication_attempted = False
        lifecycle_succeeded = False
        try:
            if prior_config["raw"] is None:
                raise ValueError("scheduler rollback config snapshot is missing")
            _atomic_owner_file(
                config_path, prior_config["raw"], mode=prior_config["mode"],
                mtime_ns=prior_config["mtime_ns"],
            )
            published_config = _read_optional(config_path)
            paths = scheduler_lifecycle.apply_launch_agents(
                home_path, generated, command_runner, loaded=loaded,
                uid=effective_uid, launchctl=launchctl, intent=action,
                expected=generated, desired_loaded=desired_loaded,
                desired_metadata={
                    label: {
                        "mode": jobs[label]["mode"],
                        "mtime_ns": jobs[label]["mtime_ns"],
                    }
                    for label in scheduler_lifecycle.LABELS
                },
                plist_validator=validator, plutil=plutil,
            )
            lifecycle_succeeded = True
            published_jobs = scheduler_lifecycle.capture_launch_agents(
                home_path, loaded=desired_loaded,
            )
            publication_attempted = True
            _atomic_owner_file(active_path, rollback_raw)
        except BaseException:
            if not lifecycle_succeeded:
                if published_config is None:
                    raise
                try:
                    _restore_optional(
                        config_path, current_config, prior_config["raw"],
                        expected_identity=published_config["identity"],
                    )
                except BaseException as compensation:
                    raise scheduler_lifecycle.LifecycleError(
                        "scheduler rollback failed and config compensation was incomplete",
                    ) from compensation
                raise
            compensation_errors: list[BaseException] = []
            config_safe = True
            try:
                observed_config = _read_optional(config_path)
                if any(observed_config.get(key) != published_config.get(key)
                       for key in ("raw", "identity")):
                    raise ValueError("scheduler config changed before compensation")
            except BaseException as exc:
                config_safe = False
                compensation_errors.append(exc)
            try:
                if config_safe:
                    _compensate_live(
                        home_path, current_jobs, expected_current=published_jobs,
                        currently_loaded=desired_loaded,
                        runner=command_runner, uid=effective_uid,
                        launchctl=launchctl, validator=validator, plutil=plutil,
                    )
            except BaseException as exc:
                compensation_errors.append(exc)
            try:
                if config_safe and current_config["raw"] is not None:
                    _atomic_owner_file(
                        config_path, current_config["raw"], mode=current_config["mode"],
                        mtime_ns=current_config["mtime_ns"],
                        expected_identity=published_config["identity"],
                    )
            except BaseException as exc:
                compensation_errors.append(exc)
            if publication_attempted:
                try:
                    _restore_publication(active_path, prior_active, rollback_raw)
                except BaseException as exc:
                    compensation_errors.append(exc)
            if compensation_errors:
                raise scheduler_lifecycle.LifecycleError(
                    "scheduler rollback failed and compensation was incomplete",
                )
            raise
        return {
            "action": action, "home": str(home_path),
            "jobs": {label: str(path) for label, path in paths.items()},
        }
    generated = scheduled_launchers.build_launch_agents_from_state(document, agent_root)
    captured = scheduler_lifecycle.capture_launch_agents(home_path, loaded=loaded)
    config = _read_optional(config_path)
    if config["raw"] is None:
        raise ValueError("scheduler lifecycle requires a persisted local schedule config")
    prior_rollback = _read_optional(rollback_path, limit=_ROLLBACK_MAX)
    prior_active = _read_optional(active_path, limit=_ROLLBACK_MAX)
    if prior_active["raw"] is not None:
        _decode_snapshot(
            prior_active["raw"], target=target_path, home=home_path,
            uid=effective_uid,
        )
        snapshot_raw = prior_active["raw"]
    else:
        snapshot_raw = _encode_snapshot(
            captured, config, target=target_path, home=home_path,
            uid=effective_uid,
        )
    _atomic_owner_file(rollback_path, snapshot_raw)
    try:
        paths = scheduler_lifecycle.apply_launch_agents(
            home_path, generated, command_runner, loaded=loaded, uid=effective_uid,
            launchctl=launchctl, intent=action, expected=generated,
            plist_validator=validator, plutil=plutil,
        )
    except BaseException:
        _restore_optional(rollback_path, prior_rollback, snapshot_raw)
        raise
    installed = scheduler_lifecycle.capture_launch_agents(
        home_path, loaded={label: True for label in scheduler_lifecycle.LABELS},
    )
    active_raw = _encode_snapshot(
        installed, config, target=target_path, home=home_path, uid=effective_uid,
    )
    try:
        _atomic_owner_file(active_path, active_raw)
    except BaseException as original:
        compensation_errors: list[BaseException] = []
        config_safe = True
        try:
            current_config = _read_optional(config_path)
            if any(current_config.get(key) != config.get(key)
                   for key in ("raw", "identity")):
                raise ValueError("scheduler config changed during compensation")
        except BaseException as exc:
            config_safe = False
            compensation_errors.append(exc)
        try:
            if config_safe:
                _compensate_live(
                    home_path, captured, expected_current=installed,
                    currently_loaded={
                        label: True for label in scheduler_lifecycle.LABELS
                    },
                    runner=command_runner, uid=effective_uid,
                    launchctl=launchctl, validator=validator, plutil=plutil,
                )
        except BaseException as exc:
            compensation_errors.append(exc)
        try:
            _restore_publication(active_path, prior_active, active_raw)
        except BaseException as exc:
            compensation_errors.append(exc)
        try:
            _restore_publication(rollback_path, prior_rollback, snapshot_raw)
        except BaseException as exc:
            compensation_errors.append(exc)
        if compensation_errors:
            raise scheduler_lifecycle.LifecycleError(
                "scheduler state publication failed and compensation was incomplete",
            ) from original
        raise
    return {
        "action": action, "home": str(home_path),
        "jobs": {label: str(path) for label, path in paths.items()},
    }


def collect_doctor(
    *, target: str | Path, home: str | Path, runner: object | None = None,
    launchctl: str = "/bin/launchctl", uid: int | None = None,
    now: str,
) -> tuple[int, list[str], dict[str, object]]:
    """Collect a read-only, structured scheduler attestation."""
    target_path, home_path = validate_user_target(target, home)
    document = state.load(target_path)
    if not isinstance(document, dict):
        raise ValueError("scheduler doctor requires valid install state")
    agent_root = target_path / ".agent"
    orchestration = document.get("orchestration")
    if not isinstance(orchestration, dict):
        raise ValueError("scheduler doctor requires valid orchestration state")
    selected = scheduled_runtime.validate_record_data(orchestration.get("scheduled_runtime"))
    expected = scheduled_launchers.build_launch_agents(selected.path, agent_root)
    shim_expected = scheduled_launchers.build_review_compatibility_shim(
        selected.path, agent_root,
    )
    return scheduler_doctor.collect_scheduler_attestation(
        target_root=target_path, home=home_path, expected_plists=expected,
        runtime=selected.record(), expected_shim=shim_expected, runner=runner or SubprocessRunner(),
        launchctl=launchctl, uid=os.getuid() if uid is None else uid, now=now,
    )
