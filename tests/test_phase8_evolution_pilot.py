import json
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

from orchestration.memos_factory import create_memos_provider
from orchestration.memos_journal import MemosDeliveryJournal
from orchestration.memos_runtime import (
    build_memos_config,
    load_evolution_pilot_config,
    prepare_project_runtime,
)


PROJECT_ID = "0123456789abcdef"


def _pilot(repo_root: Path) -> dict:
    return {
        "schema": "agentic.memory.evolution-pilot.v1",
        "enabled": True,
        "project_id": PROJECT_ID,
        "repo_root": str(repo_root.resolve()),
        "gpt_model": "gpt-5.4",
        "opus_model": "opus",
        "daily_caps": {"policy": 5, "world_model": 2, "skill": 2, "other": 50},
        "min_distinct_episodes": 3,
        "timeout_seconds": 60,
    }


def _write_private(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


class EvolutionPilotConfigTest(unittest.TestCase):
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
            "l2Induction": {"minEpisodesForInduction": 3},
            "l3Abstraction": {"minPolicies": 2, "minPolicySupport": 3},
            "skill": {"minSupport": 3, "candidateTrials": 3},
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
            self.assertEqual(loaded.gpt_model, "gpt-5.4")
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

    def test_factory_blocks_even_the_selected_project_before_state_changes(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            config = root / "pilot.json"
            _write_private(config, _pilot(repo))
            with patch.dict(os.environ, {
                "AGENTIC_EVOLUTION_PILOT_CONFIG": str(config),
            }, clear=False):
                with self.assertRaisesRegex(
                    RuntimeError, "evolution_pilot_host_handler_unavailable",
                ):
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


if __name__ == "__main__":
    unittest.main()
