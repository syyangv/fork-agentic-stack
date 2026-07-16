import importlib.util
import json
import os
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

    def test_assist_mode_is_rejected_before_behavioral_injection_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, environment = self._environment(Path(tmp), mode="assist")
            result = self._run(environment, "recall", "--intent", "permissions")
            self.assertEqual(result.returncode, 2)
            self.assertIn("not supported before Phase 6", result.stderr)


if __name__ == "__main__":
    unittest.main()
