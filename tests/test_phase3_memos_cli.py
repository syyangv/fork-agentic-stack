import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / ".agent"
sys.path.insert(0, str(AGENT / "memory"))
from orchestration.contracts import EventEnvelope
from orchestration.identity import derive_project_identity


def _load_cli_module():
    path = AGENT / "tools" / "memory_orchestrate.py"
    spec = importlib.util.spec_from_file_location("phase3_memory_orchestrate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeExportProvider:
    def export_shadow(self, *, limit, max_bytes):
        payload = {
            "schema": "agentic.memory.behavioral-shadow.v1",
            "mode": "shadow",
            "traces": [{"id": str(index)} for index in range(limit)],
        }
        while len(json.dumps(payload).encode()) > max_bytes:
            payload["traces"].pop()
        return payload


class FakeSession:
    def __enter__(self):
        return FakeExportProvider()

    def __exit__(self, *_args):
        return None


class MemosCliTest(unittest.TestCase):
    def _environment(self, root: Path, mode="shadow"):
        project = root / "project"
        project.mkdir()
        config = root / "config.json"
        config.write_text(json.dumps({
            "schema": "agentic.memory.config.v1",
            "mode": mode,
            "total_token_budget": 12000,
            "lane_reserves": {"governance": 4800, "behavioral": 4200, "evidence": 3000},
            "project_aliases": {},
        }))
        environment = os.environ.copy()
        environment.update({
            "AGENTIC_PROJECT_ROOT": str(project),
            "AGENTIC_MEMORY_CONFIG": str(config),
            "AGENTIC_MEMOS_CODE_ROOT": str(root / "missing-code"),
            "AGENTIC_MEMOS_DATA_ROOT": str(root / "state"),
        })
        return project, environment

    def _run(self, environment, *arguments, stdin=None):
        return subprocess.run(
            [sys.executable, str(AGENT / "tools/memory_orchestrate.py"), *arguments],
            cwd=ROOT, env=environment, input=stdin, text=True,
            capture_output=True, check=False,
        )

    def test_unavailable_plugin_degrades_health_without_breaking_governance(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, environment = self._environment(Path(tmp))
            health = self._run(environment, "health")
            self.assertEqual(health.returncode, 0, health.stderr)
            payload = json.loads(health.stdout)
            self.assertEqual(payload["governance"]["status"], "healthy")
            self.assertEqual(payload["behavioral"]["status"], "degraded")
            self.assertIn("behavioral_unavailable", payload["behavioral"]["warnings"])

            recall = self._run(
                environment, "recall", "--intent", "permissions", "--format", "json",
            )
            self.assertEqual(recall.returncode, 0, recall.stderr)
            packet = json.loads(recall.stdout)["context_packet"]
            self.assertTrue(packet["routing"]["governance"])
            self.assertTrue(packet["routing"]["behavioral"])
            self.assertTrue(packet["sections"][0]["items"])
            self.assertEqual(packet["sections"][1]["items"], [])

    def test_external_event_is_validated_and_plaintext_secret_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, environment = self._environment(Path(tmp))
            identity = derive_project_identity(project)
            event = EventEnvelope.create(
                timestamp="2026-07-16T20:00:00Z", event_type="task.started",
                project_id=identity.project_id, repo_root=str(project), revision=None,
                harness="codex", run_id="run-1", session_id="session-1",
                actor="agent", intent="test shadow delivery", payload={},
                idempotency_key="idem-1",
            )
            valid = self._run(environment, "record", stdin=event.canonical_json())
            self.assertEqual(valid.returncode, 0, valid.stderr)
            result = json.loads(valid.stdout)
            self.assertEqual(result["status"], "recorded")
            self.assertEqual(result["event_ids"], [event.event_id])
            self.assertEqual(result["enqueued"], 2)
            self.assertEqual(result["health"]["status"], "degraded")

            unsafe = event.to_dict()
            unsafe["payload"] = {"api_key": "sk-plaintext-must-not-echo"}
            rejected = self._run(environment, "record", stdin=json.dumps(unsafe))
            self.assertEqual(rejected.returncode, 2)
            self.assertNotIn("sk-plaintext-must-not-echo", rejected.stderr)
            self.assertIn("ContractError", rejected.stderr)

    def test_export_command_preserves_limit_and_byte_bound(self):
        module = _load_cli_module()
        identity = derive_project_identity(ROOT)
        config = type("Config", (), {"mode": "shadow"})()
        with mock.patch.object(module, "_runtime_context", return_value=(identity, config)), \
             mock.patch.object(module, "_provider_session", return_value=FakeSession()):
            result = module.export_command(limit=100, max_bytes=256)
        self.assertLessEqual(len(result["traces"]), 100)
        self.assertLessEqual(len(json.dumps(result).encode()), 256)

    def test_recall_run_id_rejects_sensitive_paths_and_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, environment = self._environment(Path(tmp), mode="off")
            for run_id in ("sk-abcdefghijklmnopqrstuvwxyz", "/tmp/credentials",
                           "line\nbreak", "../escape"):
                with self.subTest(run_id=run_id):
                    result = self._run(
                        environment, "recall", "--intent", "permissions",
                        "--run-id", run_id,
                    )
                    self.assertEqual(result.returncode, 2)
            valid = self._run(
                environment, "recall", "--intent", "permissions",
                "--run-id", "run_2026-07-18:abc.1",
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)

    def test_assist_mode_fails_closed_to_governance_until_quality_gate_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, environment = self._environment(Path(tmp), mode="assist")
            result = self._run(
                environment, "recall", "--intent", "permissions", "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(result.stdout)["context_packet"]
            self.assertTrue(packet["sections"][0]["items"])
            self.assertEqual(packet["sections"][1]["items"], [])
            self.assertIn("assist_quality_gate_blocked", packet["warnings"])

            health = json.loads(self._run(environment, "health").stdout)
            self.assertEqual(health["behavioral"]["effective_mode"], "shadow")
            self.assertFalse(health["behavioral"]["assist_gate"]["eligible"])

    def test_eligible_assist_still_preserves_governance_when_plugins_are_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, environment = self._environment(root, mode="assist")
            identity = derive_project_identity(project)
            metrics = root / "assist-quality.json"
            metrics.write_text(json.dumps({
                "schema": "agentic.memory.assist-quality.v1",
                "project_id": identity.project_id,
                "measured_at": "2026-07-18T06:00:00Z",
                "source": {"evaluation_set_sha256": "sha256:" + "c" * 64,
                           "evaluator": "phase6-cli-test"},
                "completed_episodes": 50, "task_categories": 5,
                "duplicate_rate": 0.01, "evaluation_queries": 30,
                "precision_at_5": 0.75, "cross_project_leaks": 0,
                "p95_recall_ms": 500,
            }))
            environment["AGENTIC_ASSIST_METRICS"] = str(metrics)
            result = self._run(
                environment, "recall", "--intent", "debug repository failure",
                "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            packet = payload["context_packet"]
            self.assertTrue(packet["sections"][0]["items"])
            self.assertEqual(packet["sections"][1]["items"], [])
            self.assertIn("behavioral_unavailable", packet["warnings"])
            self.assertEqual(
                payload["retrieval_preview"]["schema"],
                "agentic.memory.retrieval-preview.v1",
            )

    def test_assist_factory_and_entry_failures_preserve_governance(self):
        module = _load_cli_module()
        identity = derive_project_identity(ROOT)
        config = type("Config", (), {
            "mode": "assist", "total_token_budget": 12000,
            "lane_reserves": {"governance": 4800, "behavioral": 4200,
                              "evidence": 3000},
        })()
        gate = type("Gate", (), {"eligible": True})()

        class BrokenEntry:
            def __enter__(self):
                raise sqlite3.DatabaseError("corrupt journal")

            def __exit__(self, *_args):
                return None

        for failure in (
            OSError("runtime unavailable"), BrokenEntry(),
        ):
            provider_result = (
                mock.Mock(side_effect=failure)
                if isinstance(failure, BaseException) else mock.Mock(return_value=failure)
            )
            with self.subTest(failure=type(failure).__name__), \
                 mock.patch.object(module, "_runtime_context", return_value=(identity, config)), \
                 mock.patch.object(module, "_assist_gate", return_value=gate), \
                 mock.patch.object(module, "_provider_session", provider_result):
                payload = json.loads(module.recall_command(
                    "permissions", "json", False, 3,
                ))
            packet = payload["context_packet"]
            self.assertTrue(packet["sections"][0]["items"])
            self.assertIn("behavioral_unavailable", packet["warnings"])


if __name__ == "__main__":
    unittest.main()
