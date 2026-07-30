from __future__ import annotations

import json
import os
import plistlib
import stat
import tempfile
import unittest
import datetime as dt
from unittest import mock
from pathlib import Path

from harness_manager import (
    cli, scheduled_launchers, scheduled_runtime, scheduler_control,
    scheduler_doctor, scheduler_lifecycle,
)


ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / ".agent" / "memory"))


class _Runner:
    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], bool]] = []
        self.fail_at = fail_at

    def run(self, argv: tuple[str, ...], *, shell: bool, input=None):
        self.calls.append((argv, shell))
        stdout = (b"3.9.6\n" if len(argv) >= 2 and argv[1] == "-I"
                  else b"state = running\nlast exit code = 0\n")
        return type("Result", (), {
            "returncode": 1 if self.fail_at == len(self.calls) else 0,
            "stdout": stdout,
        })()

class _Plutil:
    def __init__(self, fail_at: int | None = None) -> None:
        self.calls = []
        self.fail_at = fail_at

    def run(self, argv, *, shell, input):
        self.calls.append((argv, shell, input))
        return type("Result", (), {"returncode": 1 if self.fail_at == len(self.calls) else 0})()


class SchedulerGate12Test(unittest.TestCase):
    def _jobs(self) -> dict[str, bytes]:
        return {
            scheduler_lifecycle.AUTO_DREAM_LABEL: plistlib.dumps({
                "Label": scheduler_lifecycle.AUTO_DREAM_LABEL,
                "ProgramArguments": ["/usr/bin/python3", str(ROOT / ".agent/tools/memory_orchestrate.py"), "maintain", "--stage-candidates", "--scheduled"],
                "StartCalendarInterval": {"Hour": 3, "Minute": 0}, "RunAtLoad": False,
                "EnvironmentVariables": {"AGENTIC_SCHEDULER_RUN": "1"},
            }),
            scheduler_lifecycle.REVIEW_NOTIFY_LABEL: plistlib.dumps({
                "Label": scheduler_lifecycle.REVIEW_NOTIFY_LABEL,
                "ProgramArguments": ["/usr/bin/python3", str(ROOT / ".agent/tools/memory_orchestrate.py"), "review", "prepare", "--scheduled", "--notify"],
                "StartCalendarInterval": {"Hour": 9, "Minute": 0}, "RunAtLoad": False,
                "EnvironmentVariables": {"AGENTIC_SCHEDULER_RUN": "1"},
            }),
        }

    def _home(self, root: Path) -> Path:
        home = root / "home"
        home.mkdir(mode=0o700)
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, mode=0o700)
        (home / "Library").chmod(0o700)
        agents.chmod(0o700)
        return home

    def test_lifecycle_uses_injected_runner_and_rolls_back_bytes_mode_mtime_and_intent(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = self._home(Path(tmp)); agents = home / "Library" / "LaunchAgents"
            old = agents / f"{scheduler_lifecycle.AUTO_DREAM_LABEL}.plist"
            old.write_bytes(b"old plist"); old.chmod(0o600); old_time = 1_700_000_000_123_456_789
            os.utime(old, ns=(old_time, old_time))
            runner = _Runner(fail_at=3)  # bootstrap first replacement fails
            with self.assertRaises(scheduler_lifecycle.LifecycleError):
                scheduler_lifecycle.apply_launch_agents(
                    home, self._jobs(), runner, loaded={scheduler_lifecycle.AUTO_DREAM_LABEL: True}, uid=501,
                    expected=self._jobs(),
                )
            self.assertEqual(old.read_bytes(), b"old plist")
            self.assertEqual(stat.S_IMODE(old.stat().st_mode), 0o600)
            self.assertEqual(old.stat().st_mtime_ns, old_time)
            self.assertFalse((agents / f"{scheduler_lifecycle.REVIEW_NOTIFY_LABEL}.plist").exists())
            self.assertEqual(runner.calls[0], (("launchctl", "bootout", "gui/501/com.syang.agentic-stack.auto-dream"), False))
            self.assertEqual(runner.calls[1][1], False)
            self.assertEqual(runner.calls[2][1], False)
            self.assertEqual(runner.calls[-1], (("launchctl", "bootstrap", "gui/501", str(old)), False))

    def test_lifecycle_success_and_compatibility_shim_guard_are_fixture_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = self._home(Path(tmp)); runner = _Runner()
            validator = _Plutil()
            paths = scheduler_lifecycle.apply_launch_agents(
                home, self._jobs(), runner, uid=502, expected=self._jobs(),
                plist_validator=validator,
            )
            self.assertEqual(set(paths), set(self._jobs()))
            self.assertTrue(all(shell is False for _argv, shell in runner.calls))
            self.assertEqual([call[0] for call in validator.calls],
                             [("/usr/bin/plutil", "-lint", "-")] * 2)
            shim = home / "Library" / "Scripts" / "agentic_stack_review_notify.py"
            shim.parent.mkdir(mode=0o700); shim.parent.chmod(0o700)
            shim.write_text("shim", encoding="utf-8"); shim.chmod(0o600)
            self.assertFalse(scheduler_lifecycle.remove_compatibility_shim(shim, {"versioned_entrypoint_active": False}))
            self.assertTrue(shim.exists())
            evidence = {
                "schema": "agentic.scheduler-doctor.v1",
                "versioned_entrypoint_active": True,
                "home": str(home),
                "shim_path": str(shim),
                "runtime_path": "/usr/bin/python3",
                "entrypoint": str(ROOT / ".agent/tools/memory_orchestrate.py"),
                "shim_identity": [shim.stat().st_dev, shim.stat().st_ino],
                "jobs": {
                    label: {
                        "plist_valid": True, "loaded": True, "healthy": True,
                        "device": paths[label].stat().st_dev,
                        "inode": paths[label].stat().st_ino,
                    }
                    for label in scheduler_lifecycle.LABELS
                },
            }
            self.assertTrue(scheduler_lifecycle.remove_compatibility_shim(shim, evidence))
            self.assertFalse(shim.exists())

    def test_install_upgrade_rollback_and_uninstall_fixtures_preserve_governance_config_and_state(self) -> None:
        for intent in ("install", "upgrade", "rollback"):
            with self.subTest(intent=intent), tempfile.TemporaryDirectory(dir=ROOT) as tmp:
                root = Path(tmp); home = self._home(root); runner = _Runner()
                governance = root / "governance.json"; config = root / "scheduled-local.json"; state = root / "install.json"
                for path, raw in ((governance, b"accepted governance"), (config, b"user schedule"), (state, b"installer state")):
                    path.write_bytes(raw)
                expected = {path: path.read_bytes() for path in (governance, config, state)}
                scheduler_lifecycle.apply_launch_agents(
                    home, self._jobs(), runner, uid=503, intent=intent,
                    expected=self._jobs(),
                )
                self.assertEqual({path: path.read_bytes() for path in expected}, expected)
                if intent == "upgrade":
                    scheduler_lifecycle.uninstall_launch_agents(home, _Runner(), uid=503)
                    self.assertFalse(any((home / "Library" / "LaunchAgents").glob("*.plist")))
                    self.assertEqual({path: path.read_bytes() for path in expected}, expected)

    def test_doctor_fixture_validates_plists_health_and_never_needs_host_observation(self) -> None:
        health = {
            "schema": "agentic.scheduler-health.v1", "label": scheduler_lifecycle.AUTO_DREAM_LABEL,
            "status": "success", "started_at": "2026-07-29T03:00:00Z", "completed_at": "2026-07-29T03:00:01Z",
            "duration_ms": 1000, "tool_version": "memory_orchestrate.v1", "source_revision": "unknown",
            "candidate_count": 2, "rejection_count": 1, "notification": "not_requested",
            "run_token": "a" * 32,
        }
        fixture = {
            "runtime": {"path": "/usr/bin/python3", "version": "3.14.0"},
            "plists": self._jobs(), "expected_plists": self._jobs(),
            "observations": {
                scheduler_lifecycle.AUTO_DREAM_LABEL: {"loaded": True, "last_exit": 0, "mode": 0o600, "owner_uid": os.getuid(), "health": health},
                scheduler_lifecycle.REVIEW_NOTIFY_LABEL: {"loaded": True, "last_exit": 0, "mode": 0o600, "owner_uid": os.getuid(), "health": {**health, "label": scheduler_lifecycle.REVIEW_NOTIFY_LABEL, "notification": "deferred"}},
            },
        }
        status, lines = scheduler_doctor.audit_scheduler_fixture(fixture, now="2026-07-29T03:20:00Z")
        self.assertEqual(status, scheduler_doctor.GREEN, lines)
        joined = "\n".join(lines)
        self.assertIn("loaded", joined); self.assertIn("fresh", joined)
        bad = json.loads(json.dumps(fixture, default=lambda value: value.decode("latin1")))
        bad["plists"] = fixture["plists"]
        bad["observations"][scheduler_lifecycle.AUTO_DREAM_LABEL]["health"]["candidate_body"] = "never persist this"
        status, lines = scheduler_doctor.audit_scheduler_fixture(bad, now="2026-07-29T03:20:00Z")
        self.assertEqual(status, scheduler_doctor.RED)
        self.assertIn("health invalid", "\n".join(lines))

    def test_structured_health_store_is_atomic_bounded_and_privacy_safe(self) -> None:
        from orchestration.scheduler_health import SchedulerHealthStore
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp); root.chmod(0o700)
            store = SchedulerHealthStore(root)
            running = store.start(scheduler_lifecycle.AUTO_DREAM_LABEL, tool_version="memory_orchestrate.v1", source_revision="deadbeef")
            state = store.finish(
                scheduler_lifecycle.AUTO_DREAM_LABEL, run_token=running["run_token"],
                success=True, duration_ms=4,
                candidate_count=3, rejection_count=1, notification="not_requested",
            )
            self.assertEqual(set(state), scheduler_doctor._HEALTH_KEYS)
            self.assertNotIn("candidate_body", repr(state))
            health_file = root / "runtime" / "scheduler-health" / (scheduler_lifecycle.AUTO_DREAM_LABEL + ".json")
            self.assertLessEqual(health_file.stat().st_size, 4096)
            self.assertEqual(stat.S_IMODE(health_file.stat().st_mode), 0o600)
            health_file.unlink(); os.symlink(root / "outside", health_file)
            with self.assertRaises(ValueError):
                store.start(scheduler_lifecycle.AUTO_DREAM_LABEL, tool_version="v", source_revision="unknown")

    def test_preflight_rejects_drift_plutil_failure_and_non_boolean_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = self._home(Path(tmp)); agents = home / "Library" / "LaunchAgents"
            drift = self._jobs()
            parsed = plistlib.loads(drift[scheduler_lifecycle.AUTO_DREAM_LABEL])
            parsed["KeepAlive"] = True
            drift[scheduler_lifecycle.AUTO_DREAM_LABEL] = plistlib.dumps(parsed)
            with self.assertRaises(scheduler_lifecycle.LifecycleError):
                scheduler_lifecycle.apply_launch_agents(
                    home, drift, _Runner(), expected=self._jobs(),
                )
            self.assertEqual(list(agents.iterdir()), [])
            with self.assertRaises(scheduler_lifecycle.LifecycleError):
                scheduler_lifecycle.apply_launch_agents(
                    home, self._jobs(), _Runner(), expected=self._jobs(),
                    plist_validator=_Plutil(fail_at=2),
                )
            self.assertEqual(list(agents.iterdir()), [])
            with self.assertRaises(scheduler_lifecycle.LifecycleError):
                scheduler_lifecycle.apply_launch_agents(
                    home, self._jobs(), _Runner(), loaded={
                        scheduler_lifecycle.AUTO_DREAM_LABEL: 1,
                    }, expected=self._jobs(),
                )
            self.assertEqual(list(agents.iterdir()), [])

    @unittest.skipUnless(Path("/usr/bin/plutil").is_file(), "macOS plutil unavailable")
    def test_generated_definitions_pass_real_plutil_without_publication(self) -> None:
        runner = scheduler_control.SubprocessRunner()
        for raw in self._jobs().values():
            result = runner.run(
                ("/usr/bin/plutil", "-lint", "-"),
                shell=False,
                input=raw,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_health_token_prevents_stale_completion_and_doctor_flags_future(self) -> None:
        from orchestration.scheduler_health import SchedulerHealthStore
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp); root.chmod(0o700)
            store = SchedulerHealthStore(root)
            first = store.start(scheduler_lifecycle.AUTO_DREAM_LABEL,
                                tool_version="v1", source_revision="unknown")
            with self.assertRaises(ValueError):
                store.start(scheduler_lifecycle.AUTO_DREAM_LABEL,
                            tool_version="v1", source_revision="unknown")
            store.finish(scheduler_lifecycle.AUTO_DREAM_LABEL,
                         run_token=first["run_token"], success=True, duration_ms=1)
            second = store.start(scheduler_lifecycle.AUTO_DREAM_LABEL,
                                 tool_version="v1", source_revision="unknown")
            with self.assertRaises(ValueError):
                store.finish(scheduler_lifecycle.AUTO_DREAM_LABEL,
                             run_token=first["run_token"], success=True, duration_ms=1)
            store.finish(scheduler_lifecycle.AUTO_DREAM_LABEL,
                         run_token=second["run_token"], success=False, duration_ms=1)

    def test_health_stale_running_record_recovers_and_finish_requires_boolean(self) -> None:
        from orchestration.scheduler_health import SchedulerHealthStore
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp); root.chmod(0o700)
            store = SchedulerHealthStore(root)
            first = store.start(
                scheduler_lifecycle.AUTO_DREAM_LABEL,
                tool_version="v1", source_revision="unknown",
            )
            health_file = (
                root / "runtime" / "scheduler-health"
                / f"{scheduler_lifecycle.AUTO_DREAM_LABEL}.json"
            )
            stale = json.loads(health_file.read_text(encoding="utf-8"))
            stale["started_at"] = "2000-01-01T00:00:00Z"
            health_file.write_text(
                json.dumps(stale, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            second = store.start(
                scheduler_lifecycle.AUTO_DREAM_LABEL,
                tool_version="v1", source_revision="unknown",
            )
            self.assertNotEqual(first["run_token"], second["run_token"])
            with self.assertRaises(ValueError):
                store.finish(
                    scheduler_lifecycle.AUTO_DREAM_LABEL,
                    run_token=second["run_token"], success=1, duration_ms=1,
                )

    def test_rollback_preserves_a_concurrent_plist_replacement(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = self._home(Path(tmp))
            agents = home / "Library" / "LaunchAgents"
            target = agents / f"{scheduler_lifecycle.AUTO_DREAM_LABEL}.plist"
            target.write_bytes(b"prior")
            target.chmod(0o644)

            class ConcurrentRunner(_Runner):
                def run(self, argv: tuple[str, ...], *, shell: bool):
                    self.calls.append((argv, shell))
                    if len(self.calls) == 1:
                        replacement = target.with_suffix(".replacement")
                        replacement.write_bytes(b"concurrent-user-edit")
                        replacement.chmod(0o644)
                        os.replace(replacement, target)
                        return type("Result", (), {"returncode": 1})()
                    return type("Result", (), {"returncode": 0})()

            concurrent = ConcurrentRunner()
            with self.assertRaisesRegex(
                scheduler_lifecycle.LifecycleError, "rollback was incomplete",
            ):
                scheduler_lifecycle.apply_launch_agents(
                    home, self._jobs(), concurrent,
                    expected=self._jobs(),
                    loaded={scheduler_lifecycle.AUTO_DREAM_LABEL: True},
                )
            self.assertEqual(target.read_bytes(), b"concurrent-user-edit")
            self.assertFalse(any(call[0][1] == "bootstrap" for call in concurrent.calls))

    def test_doctor_exact_drift_future_failure_and_bounds_semantics(self) -> None:
        base_health = {
            "schema": "agentic.scheduler-health.v1",
            "status": "success",
            "started_at": "2026-07-29T03:00:00Z",
            "completed_at": "2026-07-29T03:00:01Z",
            "duration_ms": 1000,
            "tool_version": "v1",
            "source_revision": "unknown",
            "candidate_count": 0,
            "rejection_count": 0,
            "notification": "not_requested",
            "run_token": "b" * 32,
        }
        def fixture(health_override=None):
            jobs = self._jobs()
            return {
                "runtime": {"path": "/usr/bin/python3", "version": "3.9.6"},
                "plists": dict(jobs),
                "expected_plists": dict(jobs),
                "observations": {
                    label: {
                        "loaded": True, "last_exit": 0, "mode": 0o644,
                        "owner_uid": os.getuid(),
                        "health": {
                            **base_health, "label": label,
                            **(health_override or {}),
                        },
                    }
                    for label in scheduler_lifecycle.LABELS
                },
            }
        failed = fixture({"status": "failure"})
        status, _ = scheduler_doctor.audit_scheduler_fixture(
            failed, now="2026-07-29T03:20:00Z",
        )
        self.assertEqual(status, scheduler_doctor.YELLOW)
        future = fixture({
            "started_at": "2026-07-30T03:00:00Z",
            "completed_at": "2026-07-30T03:00:01Z",
        })
        self.assertEqual(
            scheduler_doctor.audit_scheduler_fixture(
                future, now="2026-07-29T03:20:00Z",
            )[0],
            scheduler_doctor.RED,
        )
        bounded = fixture({"duration_ms": 1_000_001})
        self.assertEqual(
            scheduler_doctor.audit_scheduler_fixture(
                bounded, now="2026-07-29T03:20:00Z",
            )[0],
            scheduler_doctor.RED,
        )
        drift = fixture()
        parsed = plistlib.loads(drift["plists"][scheduler_lifecycle.REVIEW_NOTIFY_LABEL])
        parsed["StartCalendarInterval"] = {"Hour": 10, "Minute": 0}
        drift["plists"][scheduler_lifecycle.REVIEW_NOTIFY_LABEL] = plistlib.dumps(parsed)
        self.assertEqual(
            scheduler_doctor.audit_scheduler_fixture(
                drift, now="2026-07-29T03:20:00Z",
            )[0],
            scheduler_doctor.RED,
        )

    def _installed_home(self, root: Path) -> tuple[Path, dict]:
        home = self._home(root)
        agent = home / ".agent"
        (agent / "tools").mkdir(parents=True)
        (agent / "memory" / "orchestration").mkdir(parents=True)
        (agent / "runtime" / "scheduler-health").mkdir(parents=True)
        (agent / "tools" / "memory_orchestrate.py").write_text("# versioned\n", encoding="utf-8")
        (agent / "memory" / "orchestration" / "scheduled-local.json").write_text(
            json.dumps({
                "schema": "agentic.memory.scheduled-local.v1",
                "obsidian_path": None,
                "notification": "disabled",
                "maintenance_schedule": {"hour": 3, "minute": 0},
                "review_schedule": {"hour": 9, "minute": 0},
                "review_server_host": "127.0.0.1",
                "review_server_port": 48999,
            }), encoding="utf-8",
        )
        record = {"path": "/usr/bin/python3", "version": "3.9.6"}
        document = {
            "schema_version": 1, "agentic_stack_version": "test",
            "abs_target": str(home), "installed_at": "2026-07-29T00:00:00Z",
            "adapters": {}, "orchestration": {"scheduled_runtime": record},
        }
        (agent / "install.json").write_text(json.dumps(document), encoding="utf-8")
        return home, document

    def test_public_control_requires_yes_and_same_explicit_home_and_uses_exact_commands(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runner = _Runner()
            selected = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            with mock.patch.object(
                scheduled_runtime, "runtime_from_record", return_value=selected,
            ):
                with self.assertRaisesRegex(ValueError, "explicit --yes"):
                    scheduler_control.run_lifecycle(
                        "install", target=home, home=home, yes=False, runner=runner,
                    )
                with self.assertRaisesRegex(ValueError, "selected user home"):
                    scheduler_control.run_lifecycle(
                        "install", target=home / "project", home=home, yes=True, runner=runner,
                    )
                result = scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True, runner=runner,
                    plist_validator=runner, uid=504,
                )
            self.assertEqual(result["action"], "install")
            calls = [call for call in runner.calls if call[0][1] != "-lint"]
            self.assertEqual(
                calls,
                [
                    (("/bin/launchctl", "print", "gui/504/com.syang.agentic-stack.auto-dream"), False),
                    (("/bin/launchctl", "print", "gui/504/com.syang.agentic-stack.review-notify"), False),
                    (("/bin/launchctl", "bootout", "gui/504/com.syang.agentic-stack.auto-dream"), False),
                    (("/bin/launchctl", "bootout", "gui/504/com.syang.agentic-stack.review-notify"), False),
                    (("/bin/launchctl", "bootstrap", "gui/504",
                      str(home / "Library/LaunchAgents/com.syang.agentic-stack.auto-dream.plist")), False),
                    (("/bin/launchctl", "bootstrap", "gui/504",
                      str(home / "Library/LaunchAgents/com.syang.agentic-stack.review-notify.plist")), False),
                ],
            )

    def test_cli_scheduler_surface_is_explicit_and_injectable(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            with mock.patch.object(scheduler_control, "run_lifecycle", return_value={}) as lifecycle:
                self.assertEqual(
                    cli.cmd_scheduler(["install", "--home", str(home)], yes=False),
                    2,
                )
                self.assertEqual(
                    cli.cmd_scheduler(["upgrade", str(home), "--home", str(home)],
                                      yes=True, runner=_Runner(), plist_validator=_Plutil()),
                    0,
                )
            lifecycle.assert_called_once()
            self.assertIs(lifecycle.call_args.kwargs["yes"], True)
            self.assertEqual(lifecycle.call_args.kwargs["target"], home)

    def test_read_only_collector_builds_shim_removal_attestation_and_detects_unsafe_shim(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            with mock.patch.object(scheduled_runtime, "runtime_from_record", return_value=runtime):
                from harness_manager import scheduled_launchers
                jobs = scheduled_launchers.build_launch_agents_from_state(
                    document, home / ".agent",
                )
            agents = home / "Library" / "LaunchAgents"
            for label, raw in jobs.items():
                path = agents / f"{label}.plist"; path.write_bytes(raw); path.chmod(0o600)
            health = {
                "schema": "agentic.scheduler-health.v1", "status": "success",
                "started_at": "2026-07-29T03:00:00Z", "completed_at": "2026-07-29T03:00:01Z",
                "duration_ms": 1000, "tool_version": "v1", "source_revision": "unknown",
                "candidate_count": 0, "rejection_count": 0,
                "notification": "not_requested", "run_token": "c" * 32,
            }
            for label in scheduler_lifecycle.LABELS:
                path = home / ".agent/runtime/scheduler-health" / f"{label}.json"
                path.write_text(json.dumps({**health, "label": label}), encoding="utf-8")
                path.chmod(0o600)
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in agents.glob("*.plist")
            }
            with mock.patch.object(scheduled_runtime, "runtime_from_record", return_value=runtime):
                status, _lines, evidence = scheduler_control.collect_doctor(
                    target=home, home=home, runner=_Runner(), uid=505,
                    now="2026-07-29T03:20:00Z",
                )
            self.assertEqual(status, scheduler_doctor.GREEN)
            self.assertTrue(evidence["versioned_entrypoint_active"])
            self.assertTrue(all(job["loaded"] for job in evidence["jobs"].values()))
            self.assertEqual(before, {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in agents.glob("*.plist")
            })
            shim = home / "Library/Scripts/agentic_stack_review_notify.py"
            shim.parent.mkdir(mode=0o700); shim.write_text("accept_everything()\n", encoding="utf-8")
            shim.chmod(0o600)
            with mock.patch.object(scheduled_runtime, "runtime_from_record", return_value=runtime):
                status, lines, evidence = scheduler_control.collect_doctor(
                    target=home, home=home, runner=_Runner(), uid=505,
                    now="2026-07-29T03:20:00Z",
                )
            self.assertEqual(status, scheduler_doctor.RED)
            self.assertFalse(evidence["versioned_entrypoint_active"])
            self.assertIn("unsafe legacy scheduler shim", "\n".join(lines))

    def test_public_control_rejects_unknown_launchctl_failure_before_mutation(self) -> None:
        class UnknownRunner(_Runner):
            def run(self, argv, *, shell, input=None):
                self.calls.append((argv, shell))
                return type("Result", (), {
                    "returncode": 5, "stdout": b"", "stderr": b"permission denied",
                })()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            before = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
            with self.assertRaises(ValueError):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=UnknownRunner(), plist_validator=_Plutil(), uid=506,
                )
            self.assertEqual(
                before, sorted(str(path.relative_to(home)) for path in home.rglob("*")),
            )
            self.assertFalse(
                (home / ".agent/runtime/scheduler-rollback.json").exists(),
            )

    def test_launchctl_not_found_must_attest_exact_label_and_gui_uid(self) -> None:
        class NotFound:
            def __init__(self, wrong=False):
                self.wrong = wrong
            def run(self, argv, *, shell):
                label = argv[-1].rsplit("/", 1)[-1]
                if self.wrong:
                    label = "com.example.other"
                return type("Result", (), {
                    "returncode": 113, "stdout": b"",
                    "stderr": (
                        f'Could not find service "{label}" in domain for user gui: 510'
                    ).encode(),
                })()
        self.assertEqual(
            scheduler_doctor.observe_loaded_state(
                NotFound(), launchctl="/bin/launchctl", uid=510,
            ),
            {label: False for label in scheduler_lifecycle.LABELS},
        )
        with self.assertRaises(ValueError):
            scheduler_doctor.observe_loaded_state(
                NotFound(wrong=True), launchctl="/bin/launchctl", uid=510,
            )

    def test_public_upgrade_then_rollback_restores_prior_successful_config_and_plists(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            config = home / ".agent/memory/orchestration/scheduled-local.json"
            with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                   return_value=runtime):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=507,
                )
                old_config = config.read_bytes()
                old_jobs = {
                    label: (home / "Library/LaunchAgents" / f"{label}.plist").read_bytes()
                    for label in scheduler_lifecycle.LABELS
                }
                changed = json.loads(config.read_text(encoding="utf-8"))
                changed["maintenance_schedule"] = {"hour": 4, "minute": 17}
                config.write_text(json.dumps(changed), encoding="utf-8")
                scheduler_control.run_lifecycle(
                    "upgrade", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=507,
                )
                self.assertNotEqual(
                    old_jobs[scheduler_lifecycle.AUTO_DREAM_LABEL],
                    (home / "Library/LaunchAgents" /
                     f"{scheduler_lifecycle.AUTO_DREAM_LABEL}.plist").read_bytes(),
                )
                scheduler_control.run_lifecycle(
                    "rollback", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=507,
                )
            self.assertEqual(config.read_bytes(), old_config)
            self.assertEqual(old_jobs, {
                label: (home / "Library/LaunchAgents" / f"{label}.plist").read_bytes()
                for label in scheduler_lifecycle.LABELS
            })

    def test_uninstall_failure_leaves_untouched_original_inode(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = self._home(Path(tmp)); runner = _Runner(fail_at=1)
            agents = home / "Library/LaunchAgents"
            lock = agents / ".agentic-stack-scheduler.lock"
            lock.write_bytes(b""); lock.chmod(0o600)
            paths = []
            for label, raw in self._jobs().items():
                path = agents / f"{label}.plist"
                path.write_bytes(raw); path.chmod(0o644); paths.append(path)
            identities = [(path.stat().st_dev, path.stat().st_ino) for path in paths]
            with self.assertRaisesRegex(
                scheduler_lifecycle.LifecycleError, "rollback completed",
            ):
                scheduler_lifecycle.uninstall_launch_agents(
                    home, runner,
                    loaded={label: True for label in scheduler_lifecycle.LABELS},
                    uid=508,
                )
            self.assertEqual(
                identities,
                [(path.stat().st_dev, path.stat().st_ino) for path in paths],
            )

    def test_uninstall_never_bootstraps_a_concurrent_replacement(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = self._home(Path(tmp)); agents = home / "Library/LaunchAgents"
            lock = agents / ".agentic-stack-scheduler.lock"
            lock.write_bytes(b""); lock.chmod(0o600)
            target = agents / f"{scheduler_lifecycle.AUTO_DREAM_LABEL}.plist"
            for label, raw in self._jobs().items():
                path = agents / f"{label}.plist"
                path.write_bytes(raw); path.chmod(0o644)
            class SubstituteOnBootout(_Runner):
                def run(self, argv, *, shell, input=None):
                    self.calls.append((argv, shell))
                    if len(self.calls) == 1:
                        replacement = target.with_suffix(".attacker")
                        replacement.write_bytes(b"attacker")
                        replacement.chmod(0o644)
                        os.replace(replacement, target)
                        return type("Result", (), {"returncode": 1})()
                    return type("Result", (), {"returncode": 0})()
            runner = SubstituteOnBootout()
            with self.assertRaisesRegex(
                scheduler_lifecycle.LifecycleError, "rollback was incomplete",
            ):
                scheduler_lifecycle.uninstall_launch_agents(
                    home, runner,
                    loaded={label: True for label in scheduler_lifecycle.LABELS},
                    uid=513,
                )
            self.assertEqual(target.read_bytes(), b"attacker")
            self.assertFalse(any(call[0][1] == "bootstrap" for call in runner.calls))

    def test_failed_public_upgrade_preserves_previous_rollback_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                   return_value=runtime):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=509,
                )
                rollback = home / ".agent/runtime/scheduler-rollback.json"
                before = (rollback.read_bytes(), rollback.stat().st_mtime_ns)
                # Two launchctl observations precede the first lifecycle
                # bootout; that ambiguous failure must restore the old record.
                with self.assertRaises(scheduler_lifecycle.LifecycleError):
                    scheduler_control.run_lifecycle(
                        "upgrade", target=home, home=home, yes=True,
                        runner=_Runner(fail_at=3), plist_validator=_Plutil(),
                        uid=509,
                    )
            self.assertEqual(
                before, (rollback.read_bytes(), rollback.stat().st_mtime_ns),
            )

    def test_upgrade_active_publication_failure_compensates_live_and_records(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                   return_value=runtime):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=511,
                )
                agents = home / "Library/LaunchAgents"
                config = home / ".agent/memory/orchestration/scheduled-local.json"
                active = home / ".agent/runtime/scheduler-active.json"
                rollback = home / ".agent/runtime/scheduler-rollback.json"
                paths = [
                    agents / f"{label}.plist" for label in scheduler_lifecycle.LABELS
                ]
                before = {
                    path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode),
                           path.stat().st_mtime_ns)
                    for path in [*paths, config, active, rollback]
                }
                changed = json.loads(config.read_text(encoding="utf-8"))
                changed["review_schedule"] = {"hour": 10, "minute": 23}
                config.write_text(json.dumps(changed), encoding="utf-8")
                before[config] = (
                    config.read_bytes(), stat.S_IMODE(config.stat().st_mode),
                    config.stat().st_mtime_ns,
                )
                original_write = scheduler_control._atomic_owner_file
                def fail_active(path, raw, **kwargs):
                    if Path(path) == active:
                        raise OSError("injected active publication failure")
                    return original_write(path, raw, **kwargs)
                with mock.patch.object(
                    scheduler_control, "_atomic_owner_file", side_effect=fail_active,
                ):
                    with self.assertRaises(OSError):
                        scheduler_control.run_lifecycle(
                            "upgrade", target=home, home=home, yes=True,
                            runner=_Runner(), plist_validator=_Plutil(), uid=511,
                        )
            self.assertEqual(before, {
                path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode),
                       path.stat().st_mtime_ns)
                for path in [*paths, config, active, rollback]
            })

    def test_rollback_active_publication_failure_restores_preoperation_state(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            config = home / ".agent/memory/orchestration/scheduled-local.json"
            active = home / ".agent/runtime/scheduler-active.json"
            rollback = home / ".agent/runtime/scheduler-rollback.json"
            agents = home / "Library/LaunchAgents"
            with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                   return_value=runtime):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=512,
                )
                changed = json.loads(config.read_text(encoding="utf-8"))
                changed["maintenance_schedule"] = {"hour": 5, "minute": 11}
                config.write_text(json.dumps(changed), encoding="utf-8")
                scheduler_control.run_lifecycle(
                    "upgrade", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=512,
                )
                paths = [
                    agents / f"{label}.plist" for label in scheduler_lifecycle.LABELS
                ]
                before = {
                    path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode),
                           path.stat().st_mtime_ns)
                    for path in [*paths, config, active, rollback]
                }
                original_write = scheduler_control._atomic_owner_file
                def fail_active(path, raw, **kwargs):
                    if Path(path) == active:
                        raise OSError("injected active publication failure")
                    return original_write(path, raw, **kwargs)
                with mock.patch.object(
                    scheduler_control, "_atomic_owner_file", side_effect=fail_active,
                ):
                    with self.assertRaises(OSError):
                        scheduler_control.run_lifecycle(
                            "rollback", target=home, home=home, yes=True,
                            runner=_Runner(), plist_validator=_Plutil(), uid=512,
                        )
            self.assertEqual(before, {
                path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode),
                       path.stat().st_mtime_ns)
                for path in [*paths, config, active, rollback]
            })

    def test_upgrade_publication_compensation_preserves_concurrent_plist_and_config(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            config = home / ".agent/memory/orchestration/scheduled-local.json"
            active = home / ".agent/runtime/scheduler-active.json"
            target = home / "Library/LaunchAgents" / (
                scheduler_lifecycle.AUTO_DREAM_LABEL + ".plist"
            )
            with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                   return_value=runtime):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=514,
                )
                original_write = scheduler_control._atomic_owner_file
                def substitute_then_fail(path, raw, **kwargs):
                    if Path(path) == active:
                        plist_replacement = target.with_suffix(".concurrent")
                        plist_replacement.write_bytes(b"concurrent plist")
                        plist_replacement.chmod(0o644)
                        os.replace(plist_replacement, target)
                        config_replacement = config.with_suffix(".concurrent")
                        config_replacement.write_bytes(b"concurrent config")
                        config_replacement.chmod(0o600)
                        os.replace(config_replacement, config)
                        raise OSError("publication failed after concurrent edits")
                    return original_write(path, raw, **kwargs)
                with mock.patch.object(
                    scheduler_control, "_atomic_owner_file",
                    side_effect=substitute_then_fail,
                ):
                    with self.assertRaisesRegex(
                        scheduler_lifecycle.LifecycleError,
                        "compensation was incomplete",
                    ):
                        scheduler_control.run_lifecycle(
                            "upgrade", target=home, home=home, yes=True,
                            runner=_Runner(), plist_validator=_Plutil(), uid=514,
                        )
            self.assertEqual(target.read_bytes(), b"concurrent plist")
            self.assertEqual(config.read_bytes(), b"concurrent config")

    def test_rollback_publication_compensation_preserves_concurrent_plist_and_config(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            config = home / ".agent/memory/orchestration/scheduled-local.json"
            active = home / ".agent/runtime/scheduler-active.json"
            target = home / "Library/LaunchAgents" / (
                scheduler_lifecycle.REVIEW_NOTIFY_LABEL + ".plist"
            )
            with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                   return_value=runtime):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=515,
                )
                changed = json.loads(config.read_text(encoding="utf-8"))
                changed["review_schedule"] = {"hour": 12, "minute": 7}
                config.write_text(json.dumps(changed), encoding="utf-8")
                scheduler_control.run_lifecycle(
                    "upgrade", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=515,
                )
                original_write = scheduler_control._atomic_owner_file
                def substitute_then_fail(path, raw, **kwargs):
                    if Path(path) == active:
                        plist_replacement = target.with_suffix(".concurrent")
                        plist_replacement.write_bytes(b"concurrent rollback plist")
                        plist_replacement.chmod(0o644)
                        os.replace(plist_replacement, target)
                        config_replacement = config.with_suffix(".concurrent")
                        config_replacement.write_bytes(b"concurrent rollback config")
                        config_replacement.chmod(0o600)
                        os.replace(config_replacement, config)
                        raise OSError("publication failed after concurrent edits")
                    return original_write(path, raw, **kwargs)
                with mock.patch.object(
                    scheduler_control, "_atomic_owner_file",
                    side_effect=substitute_then_fail,
                ):
                    with self.assertRaisesRegex(
                        scheduler_lifecycle.LifecycleError,
                        "compensation was incomplete",
                    ):
                        scheduler_control.run_lifecycle(
                            "rollback", target=home, home=home, yes=True,
                            runner=_Runner(), plist_validator=_Plutil(), uid=515,
                        )
            self.assertEqual(target.read_bytes(), b"concurrent rollback plist")
            self.assertEqual(config.read_bytes(), b"concurrent rollback config")

    def test_public_healthy_doctor_uninstall_removes_exact_shim_and_unsafe_retains(self) -> None:
        for case in ("healthy", "unsafe", "drift"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=ROOT) as tmp:
                home, _document = self._installed_home(Path(tmp))
                runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
                with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                       return_value=runtime):
                    scheduler_control.run_lifecycle(
                        "install", target=home, home=home, yes=True,
                        runner=_Runner(), plist_validator=_Plutil(), uid=516,
                    )
                    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
                    started = (now - dt.timedelta(seconds=2)).isoformat().replace(
                        "+00:00", "Z",
                    )
                    completed = (now - dt.timedelta(seconds=1)).isoformat().replace(
                        "+00:00", "Z",
                    )
                    health = {
                        "schema": "agentic.scheduler-health.v1",
                        "status": "success", "started_at": started,
                        "completed_at": completed, "duration_ms": 1000,
                        "tool_version": "v1", "source_revision": "unknown",
                        "candidate_count": 0, "rejection_count": 0,
                        "notification": "not_requested", "run_token": "d" * 32,
                    }
                    for label in scheduler_lifecycle.LABELS:
                        path = home / ".agent/runtime/scheduler-health" / f"{label}.json"
                        path.write_text(
                            json.dumps({**health, "label": label}),
                            encoding="utf-8",
                        )
                        path.chmod(0o600)
                    shim = home / "Library/Scripts/agentic_stack_review_notify.py"
                    shim.parent.mkdir(mode=0o755)
                    raw = (b"unsafe replacement" if case == "unsafe" else
                           scheduled_launchers.build_review_compatibility_shim(
                               runtime.path, home / ".agent",
                           ))
                    shim.write_bytes(raw); shim.chmod(0o644)
                    if case == "drift":
                        drift = home / "Library/LaunchAgents" / (
                            scheduler_lifecycle.AUTO_DREAM_LABEL + ".plist"
                        )
                        parsed = plistlib.loads(drift.read_bytes())
                        parsed["StartCalendarInterval"] = {"Hour": 17, "Minute": 1}
                        drift.write_bytes(plistlib.dumps(parsed)); drift.chmod(0o644)
                    scheduler_control.run_lifecycle(
                        "uninstall", target=home, home=home, yes=True,
                        runner=_Runner(), plist_validator=_Plutil(), uid=516,
                    )
                self.assertEqual(shim.exists(), case != "healthy")
                self.assertFalse(any(
                    (home / "Library/LaunchAgents").glob(
                        "com.syang.agentic-stack.*.plist",
                    ),
                ))

    def test_rollback_lifecycle_failure_preserves_concurrent_config_replacement(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            config = home / ".agent/memory/orchestration/scheduled-local.json"
            with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                   return_value=runtime):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=517,
                )
                changed = json.loads(config.read_text(encoding="utf-8"))
                changed["maintenance_schedule"] = {"hour": 7, "minute": 9}
                config.write_text(json.dumps(changed), encoding="utf-8")
                scheduler_control.run_lifecycle(
                    "upgrade", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=517,
                )
                class ReplaceConfigOnFailure(_Runner):
                    def run(self, argv, *, shell, input=None):
                        self.calls.append((argv, shell))
                        if argv[1] == "bootout":
                            replacement = config.with_suffix(".concurrent")
                            replacement.write_bytes(b"concurrent rollback config")
                            replacement.chmod(0o600)
                            os.replace(replacement, config)
                            return type("Result", (), {"returncode": 1})()
                        stdout = (
                            b"3.9.6\n" if len(argv) >= 2 and argv[1] == "-I"
                            else b"state = running\nlast exit code = 0\n"
                        )
                        return type("Result", (), {
                            "returncode": 0, "stdout": stdout, "stderr": b"",
                        })()
                with self.assertRaisesRegex(
                    scheduler_lifecycle.LifecycleError,
                    "config compensation was incomplete",
                ):
                    scheduler_control.run_lifecycle(
                        "rollback", target=home, home=home, yes=True,
                        runner=ReplaceConfigOnFailure(),
                        plist_validator=_Plutil(), uid=517,
                    )
            self.assertEqual(config.read_bytes(), b"concurrent rollback config")

    def test_rollback_lifecycle_failure_restores_initially_absent_config(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            config = home / ".agent/memory/orchestration/scheduled-local.json"
            with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                   return_value=runtime):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=518,
                )
                changed = json.loads(config.read_text(encoding="utf-8"))
                changed["review_schedule"] = {"hour": 14, "minute": 3}
                config.write_text(json.dumps(changed), encoding="utf-8")
                scheduler_control.run_lifecycle(
                    "upgrade", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=518,
                )
                config.unlink()
                with self.assertRaises(scheduler_lifecycle.LifecycleError):
                    scheduler_control.run_lifecycle(
                        "rollback", target=home, home=home, yes=True,
                        runner=_Runner(fail_at=3), plist_validator=_Plutil(),
                        uid=518,
                    )
            self.assertFalse(config.exists())

    def test_rollback_absence_restore_preserves_swap_at_final_config_delete(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home, _document = self._installed_home(Path(tmp))
            runtime = scheduled_runtime.ScheduledRuntime("/usr/bin/python3", "3.9.6")
            config = home / ".agent/memory/orchestration/scheduled-local.json"
            with mock.patch.object(scheduled_runtime, "runtime_from_record",
                                   return_value=runtime):
                scheduler_control.run_lifecycle(
                    "install", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=519,
                )
                changed = json.loads(config.read_text(encoding="utf-8"))
                changed["review_schedule"] = {"hour": 15, "minute": 4}
                config.write_text(json.dumps(changed), encoding="utf-8")
                scheduler_control.run_lifecycle(
                    "upgrade", target=home, home=home, yes=True,
                    runner=_Runner(), plist_validator=_Plutil(), uid=519,
                )
                config.unlink()
                original_identity = scheduler_control._lstat_identity
                swapped = False
                def swap_before_final_lstat(parent_fd, name):
                    nonlocal swapped
                    if name == config.name and not swapped:
                        swapped = True
                        replacement = config.with_suffix(".final-swap")
                        replacement.write_bytes(b"concurrent final replacement")
                        replacement.chmod(0o600)
                        os.replace(replacement, config)
                    return original_identity(parent_fd, name)
                with mock.patch.object(
                    scheduler_control, "_lstat_identity",
                    side_effect=swap_before_final_lstat,
                ):
                    with self.assertRaisesRegex(
                        scheduler_lifecycle.LifecycleError,
                        "config compensation was incomplete",
                    ):
                        scheduler_control.run_lifecycle(
                            "rollback", target=home, home=home, yes=True,
                            runner=_Runner(fail_at=3),
                            plist_validator=_Plutil(), uid=519,
                        )
            self.assertTrue(swapped)
            self.assertEqual(config.read_bytes(), b"concurrent final replacement")
