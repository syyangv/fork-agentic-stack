import json
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration import host_evolution as host_module  # noqa: E402
from orchestration import memos_runtime as runtime_module  # noqa: E402
from orchestration.memos_factory import create_memos_provider
from orchestration.memos_journal import MemosDeliveryJournal
from orchestration.memos_runtime import (
    MEMOS_PLUGIN_INTEGRITY,
    MEMOS_PLUGIN_SHASUM,
    build_memos_config,
    load_evolution_pilot_config,
    prepare_project_runtime,
    validate_pinned_plugin,
)


PROJECT_ID = "0123456789abcdef"


def _pilot(repo_root: Path) -> dict:
    return {
        "schema": "agentic.memory.evolution-pilot.v2",
        "enabled": True,
        "project_id": PROJECT_ID,
        "repo_root": str(repo_root.resolve()),
        "provider": "claude_opus",
        "model": "opus",
        "daily_caps": {"policy": 5, "world_model": 2, "skill": 2, "other": 50},
        "min_distinct_episodes": 3,
        "timeout_seconds": 60,
    }


def _write_private(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


class EvolutionPilotConfigTest(unittest.TestCase):
    def test_pinned_plugin_requires_exact_immutable_installer_attestation(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp) / "memos-local-plugin/2.0.10"
            package = root / "node_modules/@memtensor/memos-local-plugin"
            (package / "dist").mkdir(parents=True)
            (package / "dist/bridge.cjs").write_text("// fixture\n")
            (package / "package.json").write_text(json.dumps({"version": "2.0.10"}))
            (root / "package-lock.json").write_text("{}")
            (root / ".agentic-stack-install.json").write_text(json.dumps({
                "artifact_sha1": MEMOS_PLUGIN_SHASUM,
                "integrity": MEMOS_PLUGIN_INTEGRITY,
                "package": "@memtensor/memos-local-plugin",
                "version": "2.0.10",
            }))
            manifest = runtime_module.build_plugin_file_manifest(root)
            manifest_path = root / ".agentic-stack-files.json"
            manifest_path.write_text(
                json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n"
            )
            marker = json.loads((root / ".agentic-stack-install.json").read_text())
            marker["files_manifest_sha256"] = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
            (root / ".agentic-stack-install.json").write_text(json.dumps(marker))
            fixture_hashes = {
                str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    package / "dist/bridge.cjs", package / "package.json",
                    root / "package-lock.json",
                )
            }
            for directory, directories, files in os.walk(root, topdown=False):
                for name in files:
                    (Path(directory) / name).chmod(0o444)
                for name in directories:
                    (Path(directory) / name).chmod(0o555)
            root.chmod(0o555)
            with patch.object(runtime_module, "MEMOS_PINNED_FILE_SHA256", fixture_hashes):
                self.assertEqual(validate_pinned_plugin(root), package / "dist/bridge.cjs")
                extra = package / "dist" / "unattested.js"
                extra.parent.chmod(0o755)
                extra.write_text("arbitrary executable code")
                extra.chmod(0o444)
                extra.parent.chmod(0o555)
                with self.assertRaisesRegex(RuntimeError, "inventory mismatch"):
                    validate_pinned_plugin(root)
                extra.parent.chmod(0o755)
                extra.unlink()
                extra.parent.chmod(0o555)
                (package / "dist/bridge.cjs").chmod(0o644)
                with self.assertRaisesRegex(RuntimeError, "digest mismatch|writable code"):
                    validate_pinned_plugin(root)
            root.chmod(0o755)
            (package / "dist/bridge.cjs").chmod(0o444)
            root.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "immutable"):
                validate_pinned_plugin(root)

    def test_default_profile_is_unchanged_and_pilot_is_host_backed(self):
        default = build_memos_config(PROJECT_ID)
        self.assertEqual(default["llm"], {
            "provider": "local_only", "fallbackToHost": False, "maxRetries": 0,
        })
        self.assertTrue(default["algorithm"]["lightweightMemory"]["enabled"])

        pilot = build_memos_config(
            PROJECT_ID, evolution_pilot=True, host_model="gpt-5.4",
        )
        self.assertEqual(pilot["llm"], {
            "provider": "host", "model": "gpt-5.4",
            "fallbackToHost": False, "maxRetries": 0,
        })
        self.assertEqual(pilot["algorithm"], {
            "lightweightMemory": {"enabled": False},
            "capture": {
                "alphaScoring": False, "synthReflections": False,
                "batchMode": "per_step",
            },
            "reward": {"llmScoring": False},
            "l2Induction": {"minEpisodesForInduction": 3},
            "l3Abstraction": {
                "minPolicies": 2, "minPolicySupport": 3,
                "traceEvidencePerPolicy": 0,
            },
            "skill": {"minSupport": 3, "candidateTrials": 3},
            "feedback": {"useLlm": False},
            "retrieval": {"llmFilterEnabled": False},
        })
        stricter = build_memos_config(
            PROJECT_ID, evolution_pilot=True, host_model="gpt-5.4",
            min_distinct_episodes=5,
        )
        self.assertEqual(
            stricter["algorithm"]["l2Induction"]["minEpisodesForInduction"], 5,
        )

    def test_loader_requires_private_exact_project_bound_schema(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            config = root / "pilot.json"
            _write_private(config, _pilot(repo))
            loaded = load_evolution_pilot_config(
                config, project_id=PROJECT_ID, repo_root=repo,
            )
            self.assertEqual(loaded.provider, "claude_opus")
            self.assertEqual(loaded.model, "opus")
            self.assertEqual(loaded.repo_root, str(repo.resolve()))
            self.assertEqual(loaded.daily_caps["policy"], 5)

            for field, value in (
                ("project_id", "fedcba9876543210"),
                ("repo_root", str(root / "another")),
            ):
                payload = _pilot(repo)
                payload[field] = value
                _write_private(config, payload)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    load_evolution_pilot_config(
                        config, project_id=PROJECT_ID, repo_root=repo,
                    )

            payload = _pilot(repo)
            payload["api_key"] = "must-never-be-a-config-slot"
            _write_private(config, payload)
            with self.assertRaises(ValueError):
                load_evolution_pilot_config(
                    config, project_id=PROJECT_ID, repo_root=repo,
                )

    @unittest.skipIf(os.name == "nt", "POSIX permission contract")
    def test_loader_rejects_group_readable_files_and_symlink_components(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            config = root / "pilot.json"
            _write_private(config, _pilot(repo))
            config.chmod(0o640)
            with self.assertRaises(PermissionError):
                load_evolution_pilot_config(
                    config, project_id=PROJECT_ID, repo_root=repo,
                )

            config.chmod(0o600)
            link = root / "linked"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                load_evolution_pilot_config(
                    link / "pilot.json", project_id=PROJECT_ID, repo_root=repo,
                )

    def test_profile_transition_rewrites_only_known_managed_configs(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            paths = prepare_project_runtime(root / "code", root / "data", PROJECT_ID)
            marker = paths.memos_home / "data" / "episode"
            marker.write_text("preserved", encoding="utf-8")
            paths = prepare_project_runtime(
                root / "code", root / "data", PROJECT_ID,
                evolution_pilot=True, host_model="gpt-5.4",
            )
            self.assertEqual(json.loads(paths.config_file.read_text())["llm"]["provider"], "host")
            self.assertEqual(marker.read_text(), "preserved")
            paths = prepare_project_runtime(root / "code", root / "data", PROJECT_ID)
            self.assertEqual(json.loads(paths.config_file.read_text())["llm"]["provider"], "local_only")
            self.assertEqual(marker.read_text(), "preserved")

    def test_profile_transition_waits_for_the_delivery_worker(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            paths = prepare_project_runtime(root / "code", root / "data", PROJECT_ID)
            journal = MemosDeliveryJournal(paths.project_root / "delivery.sqlite3")
            finished = threading.Event()

            def switch() -> None:
                prepare_project_runtime(
                    root / "code", root / "data", PROJECT_ID,
                    evolution_pilot=True, host_model="gpt-5.4",
                )
                finished.set()

            with journal.delivery_worker():
                worker = threading.Thread(target=switch)
                worker.start()
                time.sleep(0.05)
                self.assertFalse(finished.is_set())
            worker.join(timeout=2)
            self.assertTrue(finished.is_set())
            self.assertEqual(
                json.loads(paths.config_file.read_text())["llm"]["provider"], "host",
            )

    def test_factory_fails_before_state_changes_without_pinned_bridge(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            config = root / "pilot.json"
            _write_private(config, _pilot(repo))
            with patch.dict(os.environ, {
                "AGENTIC_EVOLUTION_PILOT_CONFIG": str(config),
            }, clear=False):
                with self.assertRaisesRegex(RuntimeError, "pinned MemOS"):
                    create_memos_provider(
                        root / "agent", PROJECT_ID, repo_root=repo,
                        code_root=root / "code", data_root=root / "data",
                    )
                self.assertFalse((root / "data" / PROJECT_ID).exists())
                with self.assertRaises(ValueError):
                    create_memos_provider(
                        root / "agent", "fedcba9876543210", repo_root=repo,
                        code_root=root / "code", data_root=root / "other-data",
                    )

    def test_factory_wires_only_the_concrete_handler_into_an_installed_bridge(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            config = root / "pilot.json"; _write_private(config, _pilot(repo))
            bridge = (
                root / "code" / "memos-local-plugin" / "2.0.10" / "node_modules" /
                "@memtensor" / "memos-local-plugin" / "dist" / "bridge.cjs"
            )
            bridge.parent.mkdir(parents=True)
            bridge.write_text("""
const readline = require('node:readline');
const rl = readline.createInterface({input: process.stdin});
rl.on('line', line => {
  const msg = JSON.parse(line);
  if (msg.method === 'core.health') {
    console.log(JSON.stringify({jsonrpc:'2.0', id:msg.id, result:{version:'2.0.10'}}));
  } else if (msg.method === 'core.shutdown') {
    console.log(JSON.stringify({jsonrpc:'2.0', id:msg.id, result:{ok:true}}));
    setImmediate(() => process.exit(0));
  }
});
""", encoding="utf-8")
            with patch.dict(os.environ, {
                "AGENTIC_EVOLUTION_PILOT_CONFIG": str(config),
                "AGENTIC_CLAUDE_COMMAND": "/nonexistent/claude-fixture",
            }, clear=False), patch(
                "orchestration.memos_factory.validate_pinned_plugin", return_value=bridge,
            ), patch(
                "orchestration.memos_factory._repository_revision", return_value="a" * 40,
            ):
                session = create_memos_provider(
                    root / "agent", PROJECT_ID, repo_root=repo,
                    code_root=root / "code", data_root=root / "data",
                )
                session2 = create_memos_provider(
                    root / "agent", PROJECT_ID, repo_root=repo,
                    code_root=root / "code", data_root=root / "data",
                )
            self.assertIsNotNone(session.client)
            handlers = session.client.config.request_handlers
            self.assertEqual(set(handlers or {}), {"host.llm.complete"})
            self.assertEqual(session.client.config.request_timeout, 65.0)
            self.assertIsInstance(
                handlers["host.llm.complete"], host_module.MemosOpusHostHandler,
            )
            self.assertEqual(
                handlers["host.llm.complete"].adapter.executable,
                "/nonexistent/claude-fixture",
            )
            self.assertEqual(handlers["host.llm.complete"].project_id, PROJECT_ID)
            self.assertEqual(handlers["host.llm.complete"].repository_revision, "a" * 40)
            entered_second = threading.Event()
            def enter_second():
                with session2:
                    entered_second.set()
            with session as provider:
                self.assertEqual(provider._validated_health["version"], "2.0.10")
                worker = threading.Thread(target=enter_second)
                worker.start()
                time.sleep(0.05)
                self.assertFalse(entered_second.is_set())
            worker.join(timeout=3)
            self.assertTrue(entered_second.is_set())
            session.close()

            with patch.dict(os.environ, {
                "AGENTIC_EVOLUTION_PILOT_CONFIG": str(config),
            }, clear=False), patch(
                "orchestration.memos_factory.validate_pinned_plugin", return_value=bridge,
            ), patch(
                "orchestration.memos_factory._repository_revision", return_value="a" * 40,
            ):
                owner = create_memos_provider(
                    root / "agent", PROJECT_ID, mode="shadow", repo_root=repo,
                    code_root=root / "code", data_root=root / "other-data",
                )
                contender = create_memos_provider(
                    root / "agent", PROJECT_ID, mode="assist", repo_root=repo,
                    code_root=root / "code", data_root=root / "other-data",
                    assist_deadline=time.monotonic() + 0.05,
                )
            with owner:
                contender_error = []
                def enter_contender():
                    try:
                        contender.__enter__()
                    except RuntimeError as exc:
                        contender_error.append(str(exc))
                worker = threading.Thread(target=enter_contender)
                worker.start()
                worker.join(timeout=3)
                self.assertEqual(
                    contender_error, ["evolution pilot lifecycle lock timeout"],
                )
                self.assertIsNone(contender.client._process)
            owner.close(); contender.close()


if __name__ == "__main__":
    unittest.main()
