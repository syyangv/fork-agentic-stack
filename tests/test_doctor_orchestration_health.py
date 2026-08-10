"""Gate 2 read-only orchestration diagnostics."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_manager import doctor, install as install_mod, schema


ROOT = Path(__file__).resolve().parents[1]


class DoctorOrchestrationHealthTest(unittest.TestCase):
    def setUp(self):
        self.manifest = schema.validate(ROOT / "adapters/claude-code/adapter.json")

    def install(self, target: Path, profile: str) -> None:
        install_mod.install(
            manifest=self.manifest, target_root=target,
            adapter_dir=ROOT / "adapters/claude-code", stack_root=ROOT,
            profile=profile, log=lambda _line: None,
        )

    def audit(self, target: Path, **environment: str):
        return doctor._audit_orchestration(
            target, stack_root=ROOT, environ=environment,
        )

    def state(self, target: Path) -> dict:
        path = target / ".agent/install.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def write_state(self, target: Path, value: dict) -> None:
        (target / ".agent/install.json").write_text(json.dumps(value), encoding="utf-8")

    def test_standard_and_minimal_are_green_when_off_and_disabled(self):
        for profile in ("standard", "minimal"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                self.install(target, profile)
                before = {
                    p.relative_to(target).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in (target / ".agent").rglob("*") if p.is_file()
                }
                status, lines = self.audit(target)
                after = {
                    p.relative_to(target).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in (target / ".agent").rglob("*") if p.is_file()
                }
                self.assertNotEqual(status, doctor.RED, "\n".join(lines))
                self.assertEqual(before, after)
                self.assertIn(f"profile {profile}", "\n".join(lines))

    def test_profile_gate_config_and_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, "standard")
            doc = self.state(target)
            doc["orchestration"]["phase8_quality_gate"] = "passed"
            self.write_state(target, doc)
            status, lines = self.audit(target)
            self.assertEqual(status, doctor.RED)
            self.assertIn("Phase 8", "\n".join(lines))

            doc["orchestration"]["phase8_quality_gate"] = "blocked"
            doc["schema_version"] = 999
            self.write_state(target, doc)
            status, lines = self.audit(target)
            self.assertEqual(status, doctor.RED)
            self.assertIn("unsupported install-state schema", "\n".join(lines))

            config = target / ".agent/memory/orchestration/config.json"
            config.write_text(json.dumps({"schema": "unknown", "mode": "off"}))
            status, lines = self.audit(target)
            self.assertEqual(status, doctor.RED)
            self.assertIn("orchestration config invalid", "\n".join(lines))

    def test_active_mode_evolution_r7_and_minimal_capability_are_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, "minimal")
            config_path = target / ".agent/memory/orchestration/config.json"
            config = json.loads(config_path.read_text())
            config["mode"] = "assist"
            config_path.write_text(json.dumps(config))
            status, lines = self.audit(target)
            self.assertEqual(status, doctor.RED)
            self.assertIn("must remain off", "\n".join(lines))
            config["mode"] = "off"
            config_path.write_text(json.dumps(config))
            doc = self.state(target)
            doc["orchestration"]["evolution_enabled"] = True
            self.write_state(target, doc)
            status, lines = self.audit(target)
            self.assertEqual(status, doctor.RED)
            self.assertIn("evolution", "\n".join(lines))
            doc["orchestration"]["evolution_enabled"] = False
            doc["orchestration"]["r7_skill_promoted"] = True
            self.write_state(target, doc)
            status, lines = self.audit(target)
            self.assertEqual(status, doctor.RED)
            self.assertIn("R7", "\n".join(lines))
            doc["orchestration"]["r7_skill_promoted"] = False
            self.write_state(target, doc)
            (target / ".agent/memory/orchestration/memos_factory.py").write_text("# incompatible\n")
            status, lines = self.audit(target)
            self.assertEqual(status, doctor.RED)
            self.assertIn("profile-incompatible", "\n".join(lines))

    def test_memos_missing_invalid_and_running_are_diagnosed_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, "standard")
            absent = Path(tmp) / "absent-provider"
            status, lines = self.audit(target, AGENTIC_MEMOS_CODE_ROOT=str(absent))
            self.assertEqual(status, doctor.YELLOW)
            self.assertIn("artifact unavailable", "\n".join(lines))
            invalid = Path(tmp) / "invalid-provider"
            invalid.mkdir()
            status, lines = self.audit(target, AGENTIC_MEMOS_CODE_ROOT=str(invalid))
            self.assertEqual(status, doctor.YELLOW)
            self.assertIn("pinned artifact invalid", "\n".join(lines))
            # An arbitrary live PID is never sufficient evidence of MemOS.
            status, lines = self.audit(target, AGENTIC_MEMOS_PROCESS_PID=str(os.getpid()))
            self.assertNotEqual(status, doctor.RED)
            self.assertNotIn("unexpectedly running", "\n".join(lines))

    def test_memos_profile_identity_comes_from_attested_profile_topology(self):
        project = "0123456789abcdef"
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "memos"
            pilots = data / "pilot-configs"
            pilots.mkdir(parents=True)
            manifest = pilots / f"{project}.json"
            manifest.write_text(json.dumps({
                "schema": "agentic.memory.evolution-pilot.v2", "enabled": False,
                "project_id": project, "repo_root": tmp, "provider": "claude_opus",
                "model": "opus", "daily_caps": {}, "min_distinct_episodes": 3,
                "timeout_seconds": 60,
            }))
            manifest.chmod(0o600)
            active = data / project / "profiles" / project / "memos-plugin" / "config.yaml"
            rollback = (data / f".{project}.rollback-{'a' * 32}" / "profiles"
                        / project / "memos-plugin" / "config.yaml")
            for path in (active, rollback):
                path.parent.mkdir(parents=True)
                path.write_text("{}")
                path.chmod(0o600)
            self.assertEqual(
                doctor._owned_memos_profile_configs(data),
                ((active, project), (rollback, project)),
            )

    def test_memos_profile_ownership_rejects_foreign_malformed_and_symlinked_paths(self):
        project = "0123456789abcdef"
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "memos"
            pilots = data / "pilot-configs"
            pilots.mkdir(parents=True)
            manifest = pilots / f"{project}.json"
            manifest.write_text(json.dumps({
                "schema": "agentic.memory.evolution-pilot.v2", "enabled": False,
                "project_id": project, "repo_root": tmp, "provider": "claude_opus",
                "model": "opus", "daily_caps": {}, "min_distinct_episodes": 3,
                "timeout_seconds": 60,
            }))
            manifest.chmod(0o600)
            active = data / project / "profiles" / project / "memos-plugin" / "config.yaml"
            active.parent.mkdir(parents=True)
            active.write_text("{}")
            active.chmod(0o600)
            foreign = data / "fedcba9876543210" / "profiles" / "fedcba9876543210" / "memos-plugin" / "config.yaml"
            foreign.parent.mkdir(parents=True)
            foreign.write_text("{}")
            with self.assertRaisesRegex(ValueError, "ownership manifests"):
                doctor._owned_memos_profile_configs(data)
            __import__("shutil").rmtree(data / "fedcba9876543210")
            malformed = (data / ".malformed.rollback-root" / "profiles" / project
                         / "memos-plugin" / "config.yaml")
            malformed.parent.mkdir(parents=True)
            malformed.write_text("{}")
            with self.assertRaisesRegex(ValueError, "ownership manifests"):
                doctor._owned_memos_profile_configs(data)
            __import__("shutil").rmtree(data / ".malformed.rollback-root")
            target = Path(tmp) / "outside"
            target.mkdir()
            active.parents[3].rename(target / project)
            (data / project).symlink_to(target / project, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                doctor._owned_memos_profile_configs(data)

    def test_memos_process_requires_matching_bridge_and_project_attestation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "memos"
            project = data / "0123456789abcdef"
            project.mkdir(parents=True)
            bridge = root / "plugin" / "dist" / "bridge.cjs"
            bridge.parent.mkdir(parents=True)
            bridge.write_text("bridge", encoding="utf-8")
            (project / "bridge-process.json").write_text(json.dumps({
                "pid": 42, "bridge": str(bridge), "project_root": str(project),
            }), encoding="utf-8")
            messages: list[tuple[str, str]] = []
            with mock.patch.object(doctor, "_read_process_command", return_value="python " + str(bridge.resolve())):
                doctor._audit_owned_memos_processes(data, bridge, lambda *item: messages.append(item))
            self.assertEqual(messages[0][0], doctor.RED)
            self.assertIn("unexpectedly running", messages[0][1])
            messages.clear()
            (project / "bridge-process.json").write_text(json.dumps({
                "pid": os.getpid(), "bridge": str(root / "other"), "project_root": str(project),
            }), encoding="utf-8")
            doctor._audit_owned_memos_processes(data, bridge, lambda *item: messages.append(item))
            self.assertEqual(messages, [])

    def test_crg_healthy_stale_and_private_tmp_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, "standard")
            repo = target
            (repo / ".git").mkdir()
            (repo / ".git/HEAD").write_text("a" * 40 + "\n")
            data = Path(tmp) / "graph-data"
            data.mkdir()
            self._graph(data / "graph.db", "a" * 40)
            registry = Path(tmp) / "registry.json"
            registry.write_text(json.dumps({"repos": [{"path": str(repo), "data_dir": str(data)}]}))
            status, lines = self.audit(target, AGENTIC_CRG_REGISTRY=str(registry), AGENTIC_CRG_REVISION="a" * 40)
            self.assertIn("CRG healthy", "\n".join(lines))
            self.assertNotEqual(status, doctor.RED, "\n".join(lines))
            with contextlib.closing(sqlite3.connect(data / "graph.db")) as conn, conn:
                conn.execute("update metadata set value=? where key='git_head_sha'", ("b" * 40,))
            status, lines = self.audit(target, AGENTIC_CRG_REGISTRY=str(registry), AGENTIC_CRG_REVISION="a" * 40)
            self.assertEqual(status, doctor.YELLOW)
            self.assertIn("revision_mismatch", "\n".join(lines))
            with contextlib.closing(sqlite3.connect(data / "graph.db")) as conn, conn:
                conn.execute("update metadata set value='' where key='schema_version'")
            status, lines = self.audit(target, AGENTIC_CRG_REGISTRY=str(registry), AGENTIC_CRG_REVISION="a" * 40)
            self.assertEqual(status, doctor.RED)
            self.assertIn("missing_graph_schema_version", "\n".join(lines))
            with contextlib.closing(sqlite3.connect(data / "graph.db")) as conn, conn:
                conn.execute("update metadata set value='99' where key='schema_version'")
            status, lines = self.audit(target, AGENTIC_CRG_REGISTRY=str(registry), AGENTIC_CRG_REVISION="a" * 40)
            self.assertEqual(status, doctor.RED)
            self.assertIn("unsupported_graph_schema_version", "\n".join(lines))
            volatile = Path("/private/tmp") / f"gate2-crg-{os.getpid()}"
            volatile.mkdir(exist_ok=True)
            self.addCleanup(lambda: __import__("shutil").rmtree(volatile, ignore_errors=True))
            self._graph(volatile / "graph.db", "a" * 40)
            registry.write_text(json.dumps({"repos": [{"path": str(repo), "data_dir": str(volatile)}]}))
            status, lines = self.audit(target, AGENTIC_CRG_REGISTRY=str(registry), AGENTIC_CRG_REVISION="a" * 40)
            self.assertEqual(status, doctor.RED)
            self.assertIn("volatile_graph", "\n".join(lines))

    def test_drift_and_missing_source_are_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, "standard")
            hook = target / ".agent/harness/hooks/claude_code_post_tool.py"
            hook.write_text("changed\n")
            status, lines = self.audit(target)
            self.assertEqual(status, doctor.RED)
            self.assertIn("source/deployed drift", "\n".join(lines))
            status, lines = doctor._audit_orchestration(
                target, stack_root=Path(tmp) / "not-source", environ={},
            )
            self.assertEqual(status, doctor.YELLOW)
            self.assertIn("source infrastructure unavailable", "\n".join(lines))

    def test_source_unavailable_isolated_process_uses_deployed_schema_without_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, "minimal")
            runner = """
from pathlib import Path
from harness_manager import doctor
import sys
status, lines = doctor._audit_orchestration(Path(sys.argv[1]), stack_root=Path(sys.argv[2]), environ={})
print(status)
print('\\n'.join(lines))
"""
            result = subprocess.run(
                [sys.executable, "-c", runner, str(target), str(target / "missing-source")],
                text=True, capture_output=True, env={**os.environ, "PYTHONPATH": str(ROOT)}, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("orchestration config schema and lane budgets valid", result.stdout)
            self.assertIn("source infrastructure unavailable", result.stdout)
            self.assertNotIn("orchestration config invalid", result.stdout)

    def test_two_roots_are_order_independent_and_leave_no_orchestration_cache(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left, right = Path(first), Path(second)
            self.install(left, "minimal")
            self.install(right, "standard")
            for target, source in ((left, left / "missing"), (right, ROOT), (left, left / "missing")):
                _status, lines = doctor._audit_orchestration(target, stack_root=source, environ={})
                self.assertNotIn("orchestration config invalid", "\n".join(lines))
            self.assertFalse(any(name.startswith("_doctor_orchestration_") for name in sys.modules))

    def test_doctor_never_executes_deployed_orchestration_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, "standard")
            sentinel = target / "executed-by-doctor"
            payload = "from pathlib import Path; Path(" + repr(str(sentinel)) + ").write_text('executed')\n"
            for relative in (
                "memory/orchestration/__init__.py",
                "memory/orchestration/config.py",
                "memory/orchestration/memos_runtime.py",
                "memory/orchestration/providers/crg_evidence.py",
            ):
                (target / ".agent" / relative).write_text(payload, encoding="utf-8")
            status, lines = doctor._audit_orchestration(target, stack_root=ROOT, environ={})
            self.assertEqual(status, doctor.RED, "\n".join(lines))
            self.assertIn("source/deployed drift", "\n".join(lines))
            self.assertFalse(sentinel.exists())

    def _graph(self, db: Path, revision: str) -> None:
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("create table metadata (key text primary key, value text not null)")
            conn.executemany("insert into metadata values (?, ?)", [
                ("schema_version", "9"), ("last_updated", "2026-07-28T00:00:00Z"),
                ("git_head_sha", revision),
            ])
            conn.execute("create table nodes (id integer primary key, kind text)")
            conn.executemany("insert into nodes values (?, ?)", [(1, "File"), (2, "Function")])
