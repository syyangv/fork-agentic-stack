from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.memos_backup import (
    MemosBackupError, create_project_backup, restore_project_backup,
)
from orchestration.memos_runtime import build_memos_config
from orchestration.memos_journal import MemosDeliveryJournal, stable_project_lock_path
import orchestration.memos_backup as memos_backup_module


PROJECT_ID = "0123456789abcdef"


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "state" / PROJECT_ID
    profile = root / "profiles" / PROJECT_ID / "memos-plugin"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(json.dumps(build_memos_config(PROJECT_ID)))
    connection = sqlite3.connect(root / "delivery.sqlite3")
    connection.execute("pragma journal_mode=wal")
    connection.execute("create table events(value text)")
    connection.execute("insert into events values ('before')")
    connection.commit()
    connection.close()
    return root


class MemosBackupTest(unittest.TestCase):
    def test_whole_project_backup_and_atomic_restore_preserve_prior_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            project = _runtime(tmp_path)
            backup = create_project_backup(project, tmp_path / "backups", PROJECT_ID)
            manifest = json.loads((backup / "manifest.json").read_text())
            self.assertEqual(manifest["project_id"], PROJECT_ID)
            self.assertTrue(any(
                row["path"] == "delivery.sqlite3" for row in manifest["files"]
            ))
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((backup / "manifest.json").stat().st_mode), 0o600,
            )

            with sqlite3.connect(project / "delivery.sqlite3") as connection:
                connection.execute("insert into events values ('after')")
            rollback = restore_project_backup(backup, project, PROJECT_ID)
            self.assertIsNotNone(rollback)
            assert rollback is not None
            self.assertTrue(rollback.is_dir())
            with sqlite3.connect(project / "delivery.sqlite3") as connection:
                self.assertEqual(
                    connection.execute("select value from events").fetchall(),
                    [("before",)],
                )
            with sqlite3.connect(rollback / "delivery.sqlite3") as connection:
                self.assertEqual(
                    connection.execute("select value from events").fetchall(),
                    [("before",), ("after",)],
                )

    def test_backup_rejects_symlinks_and_restore_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            project = _runtime(tmp_path)
            os.symlink(project / "delivery.sqlite3", project / "linked.db")
            with self.assertRaisesRegex(MemosBackupError, "symlink"):
                create_project_backup(project, tmp_path / "backups", PROJECT_ID)
            (project / "linked.db").unlink()
            backup = create_project_backup(project, tmp_path / "backups", PROJECT_ID)
            (backup / "project" / "delivery.sqlite3").write_bytes(b"tampered")
            with self.assertRaisesRegex(MemosBackupError, "manifest"):
                restore_project_backup(backup, project, PROJECT_ID)

    def test_project_identity_is_bound_to_root_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            wrong = tmp_path / "state" / "fedcba9876543210"
            wrong.mkdir(parents=True)
            with self.assertRaisesRegex(MemosBackupError, "basename"):
                create_project_backup(wrong, tmp_path / "backups", PROJECT_ID)
            project = _runtime(tmp_path)
            backup = create_project_backup(project, tmp_path / "backups", PROJECT_ID)
            with self.assertRaisesRegex(MemosBackupError, "project ID"):
                restore_project_backup(backup, project, "fedcba9876543210")

    def test_restore_can_create_a_missing_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            project = _runtime(tmp_path)
            backup = create_project_backup(project, tmp_path / "backups", PROJECT_ID)
            missing = tmp_path / "new-state" / PROJECT_ID
            rollback = restore_project_backup(backup, missing, PROJECT_ID)
            self.assertIsNone(rollback)
            with sqlite3.connect(missing / "delivery.sqlite3") as connection:
                self.assertEqual(
                    connection.execute("select value from events").fetchall(),
                    [("before",)],
                )

    def test_backup_and_restore_reject_unmanaged_or_unhealthy_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            project = _runtime(tmp_path)
            config = project / "profiles" / PROJECT_ID / "memos-plugin" / "config.yaml"
            config.write_text(json.dumps({"telemetry": {"enabled": True}}))
            with self.assertRaisesRegex(MemosBackupError, "managed config"):
                create_project_backup(project, tmp_path / "backups", PROJECT_ID)
            config.write_text(json.dumps(build_memos_config(PROJECT_ID)))
            backup = create_project_backup(project, tmp_path / "backups", PROJECT_ID)
            database = backup / "project" / "delivery.sqlite3"
            database.write_bytes(b"not sqlite")
            manifest = json.loads((backup / "manifest.json").read_text())
            # Digest tampering is caught before database health. A separately
            # malformed but correctly manifested snapshot reaches quick_check.
            import hashlib
            for row in manifest["files"]:
                if row["path"] == "delivery.sqlite3":
                    row["bytes"] = len(b"not sqlite")
                    row["sha256"] = hashlib.sha256(b"not sqlite").hexdigest()
            (backup / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(MemosBackupError, "database health"):
                restore_project_backup(backup, project, PROJECT_ID)

    def test_backup_waits_for_active_provider_session_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            project = _runtime(tmp_path)
            journal = MemosDeliveryJournal(project / "delivery.sqlite3")
            lifecycle_lock = Path(stable_project_lock_path(project))
            self.assertEqual(lifecycle_lock.parent, project.parent)
            self.assertFalse(lifecycle_lock.is_relative_to(project))
            self.assertEqual(stat.S_IMODE(lifecycle_lock.stat().st_mode), 0o600)
            finished = threading.Event()

            def backup() -> None:
                create_project_backup(project, tmp_path / "backups", PROJECT_ID)
                finished.set()

            with journal.delivery_worker():
                worker = threading.Thread(target=backup)
                worker.start()
                time.sleep(0.05)
                self.assertFalse(finished.is_set())
            worker.join(timeout=2)
            self.assertTrue(finished.is_set())

    def test_provider_waiter_cannot_bypass_restore_after_atomic_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            project = _runtime(tmp_path)
            journal = MemosDeliveryJournal(project / "delivery.sqlite3")
            backup = create_project_backup(project, tmp_path / "backups", PROJECT_ID)
            restore_holds_lock = threading.Event()
            release_restore = threading.Event()
            provider_entered = threading.Event()
            original = memos_backup_module._validate_runtime_health

            def pause_staging(root: Path, project_id: str) -> None:
                original(root, project_id)
                if ".restore-" in root.name:
                    restore_holds_lock.set()
                    release_restore.wait(2)

            with patch.object(
                memos_backup_module, "_validate_runtime_health", side_effect=pause_staging,
            ):
                restore_thread = threading.Thread(
                    target=restore_project_backup,
                    args=(backup, project, PROJECT_ID),
                )
                restore_thread.start()
                self.assertTrue(restore_holds_lock.wait(1))

                def enter_provider() -> None:
                    with journal.delivery_worker():
                        provider_entered.set()

                provider_thread = threading.Thread(target=enter_provider)
                provider_thread.start()
                time.sleep(0.05)
                self.assertFalse(provider_entered.is_set())
                release_restore.set()
                restore_thread.join(2)
                provider_thread.join(2)
                self.assertTrue(provider_entered.is_set())


if __name__ == "__main__":
    unittest.main()
