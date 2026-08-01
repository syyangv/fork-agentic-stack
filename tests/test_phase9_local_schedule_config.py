from __future__ import annotations

import ast
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from harness_manager import profiles, upgrade


ROOT = Path(__file__).resolve().parents[1]


class LocalScheduleConfigTest(unittest.TestCase):
    def test_default_config_is_strict_data_only_and_has_safe_values(self) -> None:
        from harness_manager.local_schedule_config import (
            DEFAULT_LOCAL_CONFIG, LocalScheduleConfig, load_local_schedule_config,
        )

        self.assertEqual(DEFAULT_LOCAL_CONFIG.as_posix(), "memory/orchestration/scheduled-local.json")
        config = load_local_schedule_config(ROOT / ".agent" / DEFAULT_LOCAL_CONFIG)
        self.assertEqual(config.schema, "agentic.memory.scheduled-local.v1")
        self.assertEqual(config.obsidian_path, None)
        self.assertEqual(config.notification, "disabled")
        self.assertEqual(config.review_server_host, "127.0.0.1")
        self.assertGreaterEqual(config.review_server_port, 1024)
        self.assertLessEqual(config.review_server_port, 65535)
        self.assertEqual(config.maintenance_schedule, {"hour": 3, "minute": 0})
        self.assertEqual(config.review_schedule, {"hour": 9, "minute": 0})

        with self.assertRaises(ValueError):
            LocalScheduleConfig.from_external({"unknown": True})
        with self.assertRaises(ValueError):
            LocalScheduleConfig.from_external({"review_server_host": "0.0.0.0"})
        with self.assertRaises(ValueError):
            LocalScheduleConfig.from_external({"review_server_port": 80})
        with self.assertRaises(ValueError):
            LocalScheduleConfig.from_external({"obsidian_path": "relative/vault"})
        with self.assertRaises(ValueError):
            LocalScheduleConfig.from_external({"notification": "email"})
        with self.assertRaises(ValueError):
            LocalScheduleConfig.from_external({"review_schedule": {"hour": 25, "minute": 0}})

    def test_portable_backend_seeds_and_reads_without_dir_fd_support(self) -> None:
        from harness_manager import local_schedule_config as config_mod

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            source = root / "scheduled-local.default.json"
            source.write_bytes(
                (ROOT / ".agent/memory/orchestration/scheduled-local.default.json").read_bytes()
            )
            destination = root / "nested/scheduled-local.json"
            with mock.patch.object(
                config_mod, "_descriptor_relative_supported", return_value=False,
            ):
                config_mod.seed_local_schedule_config(source, destination)
                loaded = config_mod.load_local_schedule_config(destination)

            self.assertEqual(loaded.schema, config_mod.SCHEMA)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_portable_backend_rejects_symlinked_parent(self) -> None:
        from harness_manager import local_schedule_config as config_mod

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            redirected = root / "redirected"
            os.symlink(outside, redirected)
            destination = redirected / "new/scheduled-local.json"
            source = ROOT / ".agent/memory/orchestration/scheduled-local.default.json"
            before = sorted(path.name for path in outside.iterdir())

            with mock.patch.object(
                config_mod, "_descriptor_relative_supported", return_value=False,
            ), self.assertRaises((OSError, ValueError)):
                config_mod.seed_local_schedule_config(source, destination)

            self.assertEqual(sorted(path.name for path in outside.iterdir()), before)
            self.assertFalse((outside / "new").exists())

    @unittest.skipUnless(os.name == "nt", "requires native Windows junctions")
    def test_portable_backend_rejects_junction_before_parent_creation(self) -> None:
        from harness_manager import local_schedule_config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            redirected = root / "redirected"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(redirected), str(outside)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr or created.stdout)
            destination = redirected / "new/scheduled-local.json"
            source = ROOT / ".agent/memory/orchestration/scheduled-local.default.json"

            with self.assertRaises((OSError, ValueError)):
                config_mod.seed_local_schedule_config(source, destination)

            self.assertFalse((outside / "new").exists())

    def test_config_reader_rejects_a_symlink_instead_of_following_it(self) -> None:
        from harness_manager.local_schedule_config import load_local_schedule_config

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "scheduled-local.json"
            os.symlink(target, link)
            with self.assertRaises(ValueError):
                load_local_schedule_config(link)
            outside = root / "outside"
            outside.mkdir()
            (outside / "scheduled-local.json").write_text(
                (ROOT / ".agent/memory/orchestration/scheduled-local.default.json")
                .read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            redirected = root / "redirected"
            os.symlink(outside, redirected)
            with self.assertRaises(ValueError):
                load_local_schedule_config(redirected / "scheduled-local.json")

    def test_upgrade_rejects_invalid_or_symlinked_override_before_mutation(self) -> None:
        from harness_manager.local_schedule_config import DEFAULT_LOCAL_CONFIG
        from harness_manager.scheduled_runtime import select_runtime

        for make_invalid in ("malformed", "symlink"):
            with self.subTest(make_invalid=make_invalid), tempfile.TemporaryDirectory(dir=ROOT) as tmp:
                target = Path(tmp)
                agent = target / ".agent"
                profiles.copy_brain(ROOT / ".agent", agent, profile=profiles.STANDARD)
                state = {
                    "schema_version": 1,
                    "agentic_stack_version": "test",
                    "abs_target": str(target.resolve()),
                    "installed_at": "2026-07-29T00:00:00Z",
                    "adapters": {},
                    "orchestration": {
                        "profile": "standard",
                        "phase8_quality_gate": "blocked",
                        "scheduled_runtime": select_runtime().record(),
                    },
                }
                (agent / "install.json").write_text(json.dumps(state), encoding="utf-8")
                config_path = agent / DEFAULT_LOCAL_CONFIG
                if make_invalid == "malformed":
                    config_path.write_text('{"invalid": true}', encoding="utf-8")
                else:
                    config_path.unlink()
                    outside = target / "outside.json"
                    outside.write_text('{"sentinel": true}', encoding="utf-8")
                    os.symlink(outside, config_path)
                before = {
                    path.relative_to(agent).as_posix(): path.lstat().st_mtime_ns
                    for path in agent.rglob("*")
                }
                self.assertFalse((agent / "install.json.lock").exists())
                self.assertEqual(
                    upgrade.upgrade(target, ROOT, yes=True, log=lambda _line: None),
                    2,
                )
                after = {
                    path.relative_to(agent).as_posix(): path.lstat().st_mtime_ns
                    for path in agent.rglob("*")
                }
                self.assertEqual(after, before)
                self.assertFalse((agent / "install.json.lock").exists())

    def test_invalid_profile_state_without_lock_is_rejected_without_mutation(self) -> None:
        from harness_manager.scheduled_runtime import select_runtime

        for invalid in ("profile", "gate"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory(dir=ROOT) as tmp:
                target = Path(tmp)
                agent = target / ".agent"
                profiles.copy_brain(ROOT / ".agent", agent, profile=profiles.STANDARD)
                orchestration = {
                    "profile": "standard",
                    "phase8_quality_gate": "blocked",
                    "scheduled_runtime": select_runtime().record(),
                }
                orchestration["profile" if invalid == "profile" else "phase8_quality_gate"] = (
                    "unknown" if invalid == "profile" else "allowed"
                )
                state = {
                    "schema_version": 1,
                    "agentic_stack_version": "test",
                    "abs_target": str(target.resolve()),
                    "installed_at": "2026-07-29T00:00:00Z",
                    "adapters": {},
                    "orchestration": orchestration,
                }
                (agent / "install.json").write_text(json.dumps(state), encoding="utf-8")
                before = {
                    path.relative_to(agent).as_posix(): path.lstat().st_mtime_ns
                    for path in agent.rglob("*")
                }

                self.assertEqual(
                    upgrade.upgrade(target, ROOT, yes=True, log=lambda _line: None),
                    2,
                )
                after = {
                    path.relative_to(agent).as_posix(): path.lstat().st_mtime_ns
                    for path in agent.rglob("*")
                }
                self.assertEqual(after, before)
                self.assertFalse((agent / "install.json.lock").exists())

    def test_final_lock_recovers_journal_created_after_preflight(self) -> None:
        from harness_manager import install as install_mod, schema

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            target = Path(tmp)
            manifest = schema.validate(ROOT / "adapters/claude-code/adapter.json")
            install_mod.install(
                manifest=manifest,
                target_root=target,
                adapter_dir=ROOT / "adapters/claude-code",
                stack_root=ROOT,
                profile="standard",
                log=lambda _line: None,
            )
            agent = target / ".agent"
            deployed = agent / "infrastructure.json"
            before = deployed.read_bytes()
            real_lock = upgrade._upgrade_lock
            injected = False

            @contextmanager
            def inject_interrupted_transaction(root_fd, target_root):
                nonlocal injected
                if not injected:
                    injected = True
                    rollback = upgrade._UpgradeRollback.capture(
                        root_fd=root_fd,
                        dst_agent=agent,
                        portable_root_identity=None,
                        relatives=[Path("infrastructure.json")],
                    )
                    rollback.persist()
                    deployed.write_bytes(b"interrupted concurrent upgrade\n")
                with real_lock(root_fd, target_root):
                    yield

            with mock.patch.object(upgrade, "_upgrade_lock", inject_interrupted_transaction):
                self.assertEqual(
                    upgrade.upgrade(target, ROOT, yes=True, log=lambda _line: None),
                    0,
                )

            self.assertTrue(injected)
            self.assertEqual(deployed.read_bytes(), before)
            self.assertFalse((agent / ".upgrade-transaction.json").exists())

    def test_plan_is_revalidated_after_final_lock_acquisition(self) -> None:
        from harness_manager import install as install_mod, schema
        from harness_manager.local_schedule_config import DEFAULT_LOCAL_CONFIG

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            target = Path(tmp)
            manifest = schema.validate(ROOT / "adapters/claude-code/adapter.json")
            install_mod.install(
                manifest=manifest,
                target_root=target,
                adapter_dir=ROOT / "adapters/claude-code",
                stack_root=ROOT,
                profile="standard",
                log=lambda _line: None,
            )
            agent = target / ".agent"
            deployed = agent / "infrastructure.json"
            deployed.write_text("stale\n", encoding="utf-8")
            real_lock = upgrade._upgrade_lock
            mutated = False

            @contextmanager
            def mutate_before_lock(root_fd, target_root):
                nonlocal mutated
                if not mutated:
                    mutated = True
                    (agent / DEFAULT_LOCAL_CONFIG).write_text(
                        '{"invalid": true}', encoding="utf-8",
                    )
                with real_lock(root_fd, target_root):
                    yield

            with mock.patch.object(upgrade, "_upgrade_lock", mutate_before_lock):
                self.assertEqual(
                    upgrade.upgrade(target, ROOT, yes=True, log=lambda _line: None),
                    2,
                )

            self.assertTrue(mutated)
            self.assertEqual(deployed.read_text(encoding="utf-8"), "stale\n")
            self.assertFalse((agent / ".upgrade-transaction.json").exists())

    def test_fresh_profiles_get_defaults_and_user_overrides_survive_upgrade_byte_for_byte(self) -> None:
        from harness_manager.local_schedule_config import DEFAULT_LOCAL_CONFIG

        for profile in (profiles.STANDARD, profiles.MINIMAL):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                profiles.copy_brain(ROOT / ".agent", target / ".agent", profile=profile)
                config_path = target / ".agent" / DEFAULT_LOCAL_CONFIG
                self.assertTrue(config_path.is_file())
                self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
                default = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(default["notification"], "disabled")

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            target = Path(tmp)
            agent = target / ".agent"
            shutil.copytree(ROOT / ".agent", agent)
            config_path = agent / "memory/orchestration/scheduled-local.json"
            override = (
                b'{\n'
                b'  "schema": "agentic.memory.scheduled-local.v1",\n'
                b'  "obsidian_path": "/Users/example/Notes",\n'
                b'  "notification": "requested",\n'
                b'  "maintenance_schedule": {"hour": 4, "minute": 5},\n'
                b'  "review_schedule": {"hour": 10, "minute": 15},\n'
                b'  "review_server_host": "127.0.0.1",\n'
                b'  "review_server_port": 48999\n'
                b'}\n'
            )
            config_path.write_bytes(override)
            config_path.chmod(0o600)
            old_time = time.time_ns() - 5_000_000_000
            os.utime(config_path, ns=(old_time, old_time))
            from harness_manager.scheduled_runtime import select_runtime
            (agent / "install.json").write_text(json.dumps({
                "schema_version": 1,
                "agentic_stack_version": "test",
                "abs_target": str(target.resolve()),
                "installed_at": "2026-07-29T00:00:00Z",
                "adapters": {},
                "orchestration": {
                    "profile": "standard",
                    "phase8_quality_gate": "blocked",
                    "scheduled_runtime": select_runtime().record(),
                },
            }), encoding="utf-8")
            before = config_path.stat()
            self.assertEqual(
                upgrade.upgrade(target, ROOT, yes=True, log=lambda _line: None),
                0,
            )
            self.assertEqual(config_path.read_bytes(), override)
            after = config_path.stat()
            self.assertEqual(after.st_mode, before.st_mode)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_review_service_reuse_requires_all_ownership_attestation_fields(self) -> None:
        from harness_manager.review_service_lifecycle import (
            ReviewServiceExpectation, ReviewServiceObservation, assess_review_service,
            expectation_from_config,
        )
        from harness_manager.local_schedule_config import LocalScheduleConfig

        expected = ReviewServiceExpectation(
            project_id="0123456789abcdef", entrypoint="/opt/agent/tools/review_server.py",
            port=48999,
            command=("/usr/bin/python3", "/opt/agent/tools/review_server.py", "--host", "127.0.0.1", "--port", "48999"),
        )
        healthy = ReviewServiceObservation(
            host="127.0.0.1", port=48999, pid=4321, healthy=True,
            attestation={
                "schema": "agentic.memory.review-service.v1",
                "project_id": "0123456789abcdef",
                "entrypoint": "/opt/agent/tools/review_server.py",
                "host": "127.0.0.1",
                "port": 48999,
                "pid": 4321,
            },
            command=expected.command,
        )
        reused = assess_review_service(expected, healthy)
        self.assertEqual(reused.status, "reused")
        self.assertEqual(reused.authority, "no_auto_accept")

        for bad in (
            None,
            ReviewServiceObservation("127.0.0.1", 48999, 4321, False, healthy.attestation, expected.command),
            ReviewServiceObservation("127.0.0.1", 48999, 4321, True, {**healthy.attestation, "project_id": "other"}, expected.command),
            ReviewServiceObservation("127.0.0.1", 48999, 4321, True, healthy.attestation, ("different",)),
            ReviewServiceObservation("0.0.0.0", 48999, 4321, True, healthy.attestation, expected.command),
            ReviewServiceObservation("127.0.0.1", 49000, 4321, True, healthy.attestation, expected.command),
            ReviewServiceObservation("127.0.0.1", 48999, 9999, True, healthy.attestation, expected.command),
        ):
            with self.subTest(observation=bad):
                outcome = assess_review_service(expected, bad)
                self.assertIn(outcome.status, {"unavailable", "port_conflict"})
                self.assertEqual(outcome.authority, "no_auto_accept")
                self.assertNotIn("0123456789abcdef", repr(outcome))
                self.assertNotIn("review_server.py", repr(outcome))

        config = LocalScheduleConfig.from_external({
            "schema": "agentic.memory.scheduled-local.v1", "obsidian_path": None,
            "notification": "disabled", "maintenance_schedule": {"hour": 3, "minute": 0},
            "review_schedule": {"hour": 9, "minute": 0},
            "review_server_host": "127.0.0.1", "review_server_port": 48999,
        })
        self.assertEqual(
            expectation_from_config(config, "0123456789abcdef", expected.entrypoint, expected.command),
            expected,
        )
        with self.assertRaisesRegex(ValueError, "one host and port"):
            expectation_from_config(
                config,
                "0123456789abcdef",
                expected.entrypoint,
                expected.command + ("--host", "0.0.0.0"),
            )

    def test_launch_definitions_read_only_local_schedule_overrides(self) -> None:
        from harness_manager.local_schedule_config import DEFAULT_LOCAL_CONFIG
        from harness_manager.scheduled_launchers import build_launch_agents

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            agent = Path(tmp) / ".agent"
            profiles.copy_brain(ROOT / ".agent", agent, profile=profiles.STANDARD)
            config_path = agent / DEFAULT_LOCAL_CONFIG
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["maintenance_schedule"] = {"hour": 4, "minute": 5}
            config["review_schedule"] = {"hour": 10, "minute": 15}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            definitions = build_launch_agents("/usr/bin/python3", agent)
            self.assertEqual(
                plistlib.loads(definitions["com.syang.agentic-stack.auto-dream"])["StartCalendarInterval"],
                {"Hour": 4, "Minute": 5},
            )
            self.assertEqual(
                plistlib.loads(definitions["com.syang.agentic-stack.review-notify"])["StartCalendarInterval"],
                {"Hour": 10, "Minute": 15},
            )

    def test_lifecycle_is_pure_fixture_driven_and_has_no_signal_or_execution_surface(self) -> None:
        source = (ROOT / "harness_manager/review_service_lifecycle.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertTrue(imported.isdisjoint({"subprocess", "socket", "signal", "os"}))
        lowered = source.lower()
        for forbidden in ("kill", "lsof", "xargs", "launchctl", "popen", "system("):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
