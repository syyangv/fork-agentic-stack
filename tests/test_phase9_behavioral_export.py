from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_manager import transfer_plan
from harness_manager.behavioral_export import (
    BehavioralExportError,
    export_behavioral_artifact,
)
from harness_manager.memos_install import (
    MEMOS_PLUGIN_INTEGRITY, MEMOS_PLUGIN_NAME, MEMOS_PLUGIN_SHASUM,
    MEMOS_PLUGIN_VERSION, _build_file_manifest, validate_installed_plugin,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


PROJECT_ID = "0123456789abcdef"
ROOT = Path(__file__).resolve().parents[1]


def snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
    return {
        path.relative_to(root).as_posix(): (
            stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns, path.read_bytes()
        )
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def make_project(base: Path, project_id: str = PROJECT_ID) -> Path:
    project = base / project_id
    plugin = project / "profiles" / project_id / "memos-plugin"
    (plugin / "data").mkdir(parents=True)
    (plugin / "skills" / "nested").mkdir(parents=True)
    (plugin / "config.yaml").write_text(json.dumps({
        "version": 1,
        "telemetry": {"enabled": False},
        "bridge": {"mode": "stdio"},
        "viewer": {"bindHost": "127.0.0.1", "openOnFirstTurn": False},
        "embedding": {"provider": "local", "model": "Xenova/all-MiniLM-L6-v2", "cache": {"enabled": True}},
        "llm": {"provider": "local_only", "fallbackToHost": False},
        "hub": {"enabled": False},
        "logging": {"llmLog": {"enabled": False, "redactPrompts": True, "redactCompletions": True}},
        "algorithm": {"lightweightMemory": {"enabled": True}},
    }), encoding="utf-8")
    (plugin / "skills" / "nested" / "skill.md").write_text(
        "# bounded behavioral skill\n", encoding="utf-8"
    )
    connection = sqlite3.connect(plugin / "data" / "memos.db")
    connection.execute("create table records (value text, blob_value blob)")
    connection.execute("insert into records values (?, ?)", ("safe", b"safe-blob"))
    connection.commit()
    connection.close()
    runtime = base.parent if base.name == "memos" else base
    _make_pinned_plugin(runtime / "providers")
    # A managed project owns one stable lifecycle lock for its full lifetime.
    # Export may acquire it, but never creates, replaces, or removes it.
    lock = project.parent / f".{project_id}.memos-lifecycle.lock"
    lock.touch(mode=0o600, exist_ok=True)
    os.chmod(lock, 0o600)
    return project


def _make_pinned_plugin(code_root: Path, *, version: str = MEMOS_PLUGIN_VERSION) -> Path:
    plugin = code_root / "memos-local-plugin" / MEMOS_PLUGIN_VERSION
    if plugin.exists():
        return plugin
    package = plugin / "node_modules" / "@memtensor" / "memos-local-plugin"
    (package / "dist").mkdir(parents=True)
    (package / "dist" / "bridge.cjs").write_text("// bridge\n", encoding="utf-8")
    (package / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (package / "telemetry.credentials.json").write_text(
        json.dumps({"audited_public_bootstrap": True}), encoding="utf-8",
    )
    manifest = _build_file_manifest(plugin)
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (plugin / ".agentic-stack-files.json").write_bytes(manifest_bytes)
    (plugin / ".agentic-stack-install.json").write_text(json.dumps({
        "artifact_sha1": MEMOS_PLUGIN_SHASUM,
        "integrity": MEMOS_PLUGIN_INTEGRITY,
        "package": MEMOS_PLUGIN_NAME,
        "version": MEMOS_PLUGIN_VERSION,
        "files_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }), encoding="utf-8")
    for path in sorted(plugin.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chmod(plugin, 0o555)
    return plugin


class BehavioralExportTest(unittest.TestCase):
    def run_cli(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["AGENTIC_STACK_ROOT"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-m", "harness_manager.cli", *args], cwd=cwd,
            env=env, text=True, capture_output=True, check=False,
        )

    def test_default_plan_never_selects_behavioral_scope(self):
        self.assertNotIn("behavioral", transfer_plan.DEFAULT_SCOPES)
        self.assertNotIn("behavioral", transfer_plan.detect_scopes("export behavioral memos skills"))
        self.assertNotIn("behavioral", transfer_plan.normalize_scopes(["behavioral"]))

    def test_cli_requires_dedicated_behavioral_flag_and_explicit_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".agent").mkdir()
            project = make_project(root / ".agent" / "runtime" / "memos")
            generic = self.run_cli(root, "transfer", "export", "--scope", "behavioral")
            self.assertEqual(generic.returncode, 2)
            self.assertIn("behavioral", generic.stderr)
            missing = self.run_cli(root, "transfer", "export", "--behavioral-export")
            self.assertEqual(missing.returncode, 2)
            result = self.run_cli(
                root, "transfer", "export", "--behavioral-export",
                "--project-id", PROJECT_ID, "--project-provenance", "example/repo",
                "--behavioral-output", str(root / "artifact"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "artifact" / "manifest.json").is_file())
            self.assertTrue(project.is_dir())

    def test_export_is_project_bound_and_does_not_mutate_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            before = snapshot(project)
            artifact = export_behavioral_artifact(
                project, root / "exports", PROJECT_ID,
                provenance="example/repository",
            )
            manifest = json.loads((artifact / "manifest.json").read_text())
            self.assertEqual(manifest["project_id"], PROJECT_ID)
            self.assertEqual(manifest["provenance"], "example/repository")
            self.assertEqual(
                [row["path"] for row in manifest["artifacts"]],
                ["data/memos.db", "skills/nested/skill.md"],
            )
            self.assertTrue((artifact / "data" / "memos.db").is_file())
            self.assertEqual(snapshot(project), before)

    def test_project_identity_and_runtime_leakage_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            (project / "delivery.sqlite3").write_bytes(b"must not export")
            with self.assertRaisesRegex(BehavioralExportError, "project ID"):
                export_behavioral_artifact(project, root / "exports", "fedcba9876543210", provenance="repo")
            artifact = export_behavioral_artifact(project, root / "exports", PROJECT_ID, provenance="repo")
            self.assertFalse(any("delivery" in path.name for path in artifact.rglob("*")))

    def test_only_selected_project_db_and_skills_are_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            other = make_project(root, "fedcba9876543210")
            (other / "profiles" / "fedcba9876543210" / "memos-plugin" / "skills" / "other.md").write_text("other", encoding="utf-8")
            artifact = export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            exported = "\n".join(path.relative_to(artifact).as_posix() for path in artifact.rglob("*"))
            self.assertNotIn("other.md", exported)
            self.assertNotIn("fedcba", exported)
            self.assertNotIn("config.yaml", exported)
            self.assertNotIn("bridge-process", exported)
            self.assertNotIn("telemetry.credentials.json", exported)

    def test_empty_skills_export_has_deterministic_database_only_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            skills = project / "profiles" / PROJECT_ID / "memos-plugin" / "skills"
            for child in skills.rglob("*"):
                if child.is_file():
                    child.unlink()
            for child in sorted(skills.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if child.is_dir():
                    child.rmdir()
            artifact = export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            manifest = json.loads((artifact / "manifest.json").read_text())
            self.assertEqual([item["path"] for item in manifest["artifacts"]], ["data/memos.db"])
            self.assertFalse((artifact / "skills").exists())

    def test_invalid_utf8_corrupt_db_and_unsafe_permissions_fail_before_output(self):
        for mutation, message in (
            ("utf8", "UTF-8"), ("corrupt", "SQLite"), ("permissions", "permissions"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                project = make_project(root)
                plugin = project / "profiles" / PROJECT_ID / "memos-plugin"
                if mutation == "utf8":
                    (plugin / "skills" / "nested" / "skill.md").write_bytes(b"\xff")
                elif mutation == "corrupt":
                    (plugin / "data" / "memos.db").write_bytes(b"not a sqlite database")
                else:
                    os.chmod(plugin / "data" / "memos.db", 0o666)
                with self.assertRaisesRegex(BehavioralExportError, message):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
                self.assertFalse((root / "artifact").exists())

    def test_secret_scan_covers_late_sqlite_text_blob_and_nested_skill_before_output(self):
        for place in ("late_text", "blob", "skill"):
            with self.subTest(place=place), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                project = make_project(root)
                database = project / "profiles" / PROJECT_ID / "memos-plugin" / "data" / "memos.db"
                if place == "late_text":
                    with sqlite3.connect(database) as connection:
                        connection.execute("insert into records values (?, ?)", ("sk-live-secret-value", b"safe"))
                elif place == "blob":
                    with sqlite3.connect(database) as connection:
                        connection.execute("insert into records values (?, ?)", ("safe", b"Authorization: Bearer secret"))
                else:
                    (project / "profiles" / PROJECT_ID / "memos-plugin" / "skills" / "nested" / "skill.md").write_text(
                        "token=super-secret", encoding="utf-8"
                    )
                output = root / "exports"
                with self.assertRaisesRegex(BehavioralExportError, "secret-like"):
                    export_behavioral_artifact(project, output, PROJECT_ID, provenance="repo")
                self.assertFalse(output.exists())

    def test_symlink_live_attestation_and_integrity_failure_are_rejected(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            plugin = project / "profiles" / PROJECT_ID / "memos-plugin"
            skill = plugin / "skills" / "nested" / "skill.md"
            outside = root / "outside.md"
            outside.write_text("safe", encoding="utf-8")
            skill.unlink()
            skill.symlink_to(outside)
            with self.assertRaisesRegex(BehavioralExportError, "symbolic"):
                export_behavioral_artifact(project, root / "exports", PROJECT_ID, provenance="repo")

            skill.unlink()
            skill.write_text("safe", encoding="utf-8")
            (project / "bridge-process.json").write_text(json.dumps({"pid": os.getpid()}))
            with self.assertRaisesRegex(BehavioralExportError, "live"):
                export_behavioral_artifact(project, root / "exports", PROJECT_ID, provenance="repo")

    def test_malformed_or_swapped_bridge_attestation_fails_closed(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            (project / "bridge-process.json").write_text(
                json.dumps({"pid": True}), encoding="utf-8",
            )
            with self.assertRaisesRegex(BehavioralExportError, "attestation is invalid"):
                export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertFalse((root / "artifact").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            attestation = project / "bridge-process.json"
            attestation.write_text(json.dumps({"pid": 999999}), encoding="utf-8")
            outside = root / "outside-attestation.json"
            outside.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            original_validate = __import__(
                "harness_manager.behavioral_export", fromlist=["_validate_regular_file"]
            )._validate_regular_file

            def swap(path: Path, label: str, maximum: int) -> None:
                original_validate(path, label, maximum)
                if label == "MemOS bridge attestation":
                    attestation.unlink()
                    attestation.symlink_to(outside)

            with patch(
                "harness_manager.behavioral_export._validate_regular_file",
                side_effect=swap,
            ):
                with self.assertRaisesRegex(BehavioralExportError, "changed during export"):
                    export_behavioral_artifact(
                        project, root / "artifact", PROJECT_ID, provenance="repo",
                    )
            self.assertFalse((root / "artifact").exists())

    def test_existing_lifecycle_lock_refuses_export_without_runtime_mutation(self):
        if fcntl is None:
            self.skipTest("POSIX lifecycle locks are unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            before = snapshot(project)
            lock = project.parent / f".{PROJECT_ID}.memos-lifecycle.lock"
            with lock.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                with self.assertRaisesRegex(BehavioralExportError, "lifecycle lock"):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertEqual(snapshot(project), before)
            self.assertFalse((root / "artifact").exists())

    def test_missing_managed_lifecycle_lock_refuses_without_creating_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            lock = project.parent / f".{PROJECT_ID}.memos-lifecycle.lock"
            lock.unlink()
            with self.assertRaisesRegex(BehavioralExportError, "lifecycle lock"):
                export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertFalse(lock.exists())
            self.assertFalse((root / "artifact").exists())

    def test_digest_is_stable_and_tampering_is_evident(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            first = export_behavioral_artifact(project, root / "first", PROJECT_ID, provenance="repo")
            second = export_behavioral_artifact(project, root / "second", PROJECT_ID, provenance="repo")
            first_manifest = json.loads((first / "manifest.json").read_text())
            second_manifest = json.loads((second / "manifest.json").read_text())
            self.assertEqual(first_manifest["content_digest"], second_manifest["content_digest"])
            (second / "skills" / "nested" / "skill.md").write_text("tampered", encoding="utf-8")
            self.assertNotEqual(
                __import__("hashlib").sha256((second / "skills" / "nested" / "skill.md").read_bytes()).hexdigest(),
                next(row["sha256"] for row in second_manifest["artifacts"] if row["path"].endswith("skill.md")),
            )

    def test_aggregate_skill_bound_and_schema_and_blob_signatures_fail_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            skills = project / "profiles" / PROJECT_ID / "memos-plugin" / "skills"
            for index in range(49):
                (skills / f"bulk-{index}.md").write_bytes(b"x" * (1024 * 1024))
            with self.assertRaisesRegex(BehavioralExportError, "total size bound"):
                export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertFalse((root / "artifact").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            database = project / "profiles" / PROJECT_ID / "memos-plugin" / "data" / "memos.db"
            with sqlite3.connect(database) as connection:
                connection.execute("create table schema_secret (value text default 'token=hidden')")
                connection.execute("insert into records values (?, ?)", ("safe", b"xx\x00Authorization: Bearer\x00secret"))
            with self.assertRaisesRegex(BehavioralExportError, "secret-like"):
                export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertFalse((root / "artifact").exists())

    def test_host_evolution_profile_and_fake_plugin_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            config = project / "profiles" / PROJECT_ID / "memos-plugin" / "config.yaml"
            value = json.loads(config.read_text())
            value["llm"]["provider"] = "host"
            value["llm"]["model"] = "not-allowed"
            config.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(BehavioralExportError, "local-only"):
                export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            config = project / "profiles" / PROJECT_ID / "memos-plugin" / "config.yaml"
            value = json.loads(config.read_text())
            value["algorithm"]["capture"] = {"alphaScoring": False}
            config.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(BehavioralExportError, "evolution fields"):
                export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            marker = root / "providers" / "memos-local-plugin" / MEMOS_PLUGIN_VERSION / ".agentic-stack-install.json"
            os.chmod(marker, 0o644)
            value = json.loads(marker.read_text())
            value["version"] = "9.9.9"
            marker.write_text(json.dumps(value), encoding="utf-8")
            os.chmod(marker, 0o444)
            with self.assertRaisesRegex(BehavioralExportError, "pinned MemOS plugin"):
                export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")

    def test_malformed_profile_nested_shapes_and_boolean_version_fail_closed(self):
        malformed_values = (
            (("version",), True),
            (("telemetry",), None),
            (("telemetry",), "SENTINEL_CONFIG_VALUE"),
            (("telemetry",), []),
            (("bridge",), None),
            (("bridge",), 1),
            (("viewer",), None),
            (("viewer",), []),
            (("embedding",), None),
            (("embedding",), []),
            (("embedding", "cache"), None),
            (("embedding", "cache"), "SENTINEL_CONFIG_VALUE"),
            (("hub",), None),
            (("hub",), []),
            (("logging",), None),
            (("logging",), 1),
            (("logging", "llmLog"), None),
            (("logging", "llmLog"), []),
            (("algorithm",), None),
            (("algorithm",), []),
            (("algorithm", "lightweightMemory"), None),
            (("algorithm", "lightweightMemory"), "SENTINEL_CONFIG_VALUE"),
        )
        for index, (parts, malformed) in enumerate(malformed_values):
            with self.subTest(parts=parts, malformed=type(malformed).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                project = make_project(root)
                config = project / "profiles" / PROJECT_ID / "memos-plugin" / "config.yaml"
                value = json.loads(config.read_text(encoding="utf-8"))
                container = value
                for part in parts[:-1]:
                    container = container[part]
                container[parts[-1]] = malformed
                config.write_text(json.dumps(value), encoding="utf-8")
                output = root / f"artifact-{index}"
                with self.assertRaises(BehavioralExportError) as raised:
                    export_behavioral_artifact(project, output, PROJECT_ID, provenance="repo")
                self.assertNotIn("SENTINEL_CONFIG_VALUE", str(raised.exception))
                self.assertFalse(output.exists())

    def test_cli_malformed_profile_is_content_free_policy_error_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".agent").mkdir()
            project = make_project(root / ".agent" / "runtime" / "memos")
            config = project / "profiles" / PROJECT_ID / "memos-plugin" / "config.yaml"
            value = json.loads(config.read_text(encoding="utf-8"))
            value["telemetry"] = "SENTINEL_CONFIG_VALUE"
            config.write_text(json.dumps(value), encoding="utf-8")
            output = root / "artifact"
            result = self.run_cli(
                root, "transfer", "export", "--behavioral-export",
                "--project-id", PROJECT_ID, "--project-provenance", "example/repo",
                "--behavioral-output", str(output),
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("SENTINEL_CONFIG_VALUE", result.stderr)
            self.assertFalse(output.exists())

    def test_malformed_pinned_plugin_metadata_fails_closed_without_output(self):
        for filename, malformed in (
            ("package.json", []),
            ("package.json", "SENTINEL_PLUGIN_METADATA"),
            (".agentic-stack-install.json", []),
            (".agentic-stack-install.json", "SENTINEL_PLUGIN_METADATA"),
        ):
            with self.subTest(filename=filename, malformed=type(malformed).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                project = make_project(root)
                plugin = root / "providers" / "memos-local-plugin" / MEMOS_PLUGIN_VERSION
                metadata = (
                    plugin / "node_modules" / "@memtensor" / "memos-local-plugin" / filename
                    if filename == "package.json" else plugin / filename
                )
                os.chmod(metadata, 0o644)
                metadata.write_text(json.dumps(malformed), encoding="utf-8")
                os.chmod(metadata, 0o444)
                with self.assertRaises(RuntimeError) as raised:
                    validate_installed_plugin(root / "providers")
                self.assertNotIn("SENTINEL_PLUGIN_METADATA", str(raised.exception))
                output = root / "artifact"
                with self.assertRaisesRegex(BehavioralExportError, "pinned MemOS plugin"):
                    export_behavioral_artifact(project, output, PROJECT_ID, provenance="repo")
                self.assertFalse(output.exists())

    def test_cli_malformed_pinned_plugin_metadata_is_content_free_policy_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".agent").mkdir()
            make_project(root / ".agent" / "runtime" / "memos")
            marker = root / ".agent" / "runtime" / "providers" / "memos-local-plugin" / MEMOS_PLUGIN_VERSION / ".agentic-stack-install.json"
            os.chmod(marker, 0o644)
            marker.write_text(json.dumps("SENTINEL_PLUGIN_METADATA"), encoding="utf-8")
            os.chmod(marker, 0o444)
            output = root / "artifact"
            result = self.run_cli(
                root, "transfer", "export", "--behavioral-export",
                "--project-id", PROJECT_ID, "--project-provenance", "example/repo",
                "--behavioral-output", str(output),
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("SENTINEL_PLUGIN_METADATA", result.stderr)
            self.assertFalse(output.exists())

    def test_config_and_database_swaps_are_refused_before_output(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        for label, relative in (
            ("managed MemOS config", Path("config.yaml")),
            ("behavioral database", Path("data/memos.db")),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                project = make_project(root)
                plugin = project / "profiles" / PROJECT_ID / "memos-plugin"
                original_path = plugin / relative
                outside = root / f"outside-{relative.name}"
                outside.write_bytes(original_path.read_bytes())
                original_validate = __import__(
                    "harness_manager.behavioral_export", fromlist=["_validate_regular_file"]
                )._validate_regular_file

                def swap(path: Path, observed_label: str, maximum: int) -> None:
                    original_validate(path, observed_label, maximum)
                    if observed_label == label:
                        original_path.unlink()
                        original_path.symlink_to(outside)

                with patch("harness_manager.behavioral_export._validate_regular_file", side_effect=swap):
                    with self.assertRaises(BehavioralExportError):
                        export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
                self.assertFalse((root / "artifact").exists())

    def test_replaced_project_namespace_is_refused_under_stable_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            original = __import__(
                "harness_manager.behavioral_export", fromlist=["_validate_managed_profile"]
            )._validate_managed_profile

            def replace_namespace(plugin: Path) -> None:
                original(plugin)
                project.rename(root / "displaced-project")
                project.mkdir()

            with patch("harness_manager.behavioral_export._validate_managed_profile", side_effect=replace_namespace):
                with self.assertRaisesRegex(BehavioralExportError, "namespace changed"):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertFalse((root / "artifact").exists())

    def test_staging_name_substitution_preserves_victim_and_refuses_export(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            original_read = __import__(
                "harness_manager.behavioral_export", fromlist=["_read_skill_exact"]
            )._read_skill_exact
            victim = root / "victim"

            def substitute(descriptor: int, entry: object) -> bytes:
                raw = original_read(descriptor, entry)
                temporary = next(path for path in root.iterdir() if path.name.startswith(f".{PROJECT_ID}.behavioral-"))
                temporary.rename(root / "displaced-staging")
                victim.mkdir()
                (victim / "sentinel").write_text("preserve", encoding="utf-8")
                victim.rename(temporary)
                return raw

            with patch("harness_manager.behavioral_export._read_skill_exact", side_effect=substitute):
                with self.assertRaises(BehavioralExportError):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertEqual((root / "displaced-staging").is_dir(), True)
            self.assertEqual((next(path for path in root.iterdir() if path.name.startswith(f".{PROJECT_ID}.behavioral-")) / "sentinel").read_text(), "preserve")
            self.assertFalse((root / "artifact").exists())

    def test_prebackup_staging_symlink_swap_never_writes_outside(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            outside = root / "outside"
            outside.mkdir()
            original = __import__(
                "harness_manager.behavioral_export", fromlist=["_sqlite_backup"]
            )._sqlite_backup

            def swap(source: Path) -> bytes:
                temporary = next(path for path in root.iterdir() if path.name.startswith(f".{PROJECT_ID}.behavioral-"))
                temporary.rename(root / "displaced-before-backup")
                temporary.symlink_to(outside, target_is_directory=True)
                return original(source)

            with patch("harness_manager.behavioral_export._sqlite_backup", side_effect=swap):
                with self.assertRaises(BehavioralExportError):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((root / "artifact").exists())

    def test_destination_reservation_never_clobbers_racing_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            output = root / "artifact"
            original_mkdir = os.mkdir

            def occupy(path: str | bytes, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
                if path == output.name and dir_fd is not None:
                    original_mkdir(path, mode, dir_fd=dir_fd)
                    output_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0), dir_fd=dir_fd)
                    descriptor = os.open("sentinel", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=output_fd)
                    os.close(descriptor)
                    os.close(output_fd)
                original_mkdir(path, mode, dir_fd=dir_fd)

            with patch("harness_manager.behavioral_export.os.mkdir", side_effect=occupy):
                with self.assertRaises(BehavioralExportError):
                    export_behavioral_artifact(project, output, PROJECT_ID, provenance="repo")
            self.assertEqual((output / "sentinel").read_text(), "")

    def test_staged_bytes_are_revalidated_after_manifest_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            original = __import__(
                "harness_manager.behavioral_export", fromlist=["_write_private_json"]
            )._write_private_json

            def mutate(path: Path, value: dict) -> None:
                original(path, value)
                temporary = next(item for item in root.iterdir() if item.name.startswith(f".{PROJECT_ID}.behavioral-"))
                (temporary / "data" / "memos.db").write_bytes(b"replacement-after-scan")

            with patch("harness_manager.behavioral_export._write_private_json", side_effect=mutate):
                with self.assertRaises(BehavioralExportError):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertFalse((root / "artifact").exists())

    def test_structured_secrets_and_stable_lifecycle_lock_fail_closed(self):
        for place in ("skill", "text", "blob", "schema"):
            with self.subTest(place=place), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                project = make_project(root)
                plugin = project / "profiles" / PROJECT_ID / "memos-plugin"
                database = plugin / "data" / "memos.db"
                credential = '"client_secret": "REDACTED"'
                if place == "skill":
                    (plugin / "skills" / "nested" / "skill.md").write_text(credential, encoding="utf-8")
                elif place == "text":
                    with sqlite3.connect(database) as connection:
                        connection.execute("insert into records values (?, ?)", (credential, b"safe"))
                elif place == "blob":
                    with sqlite3.connect(database) as connection:
                        connection.execute("insert into records values (?, ?)", ("safe", credential.encode("utf-16le")))
                else:
                    with sqlite3.connect(database) as connection:
                        connection.execute("create table credentials (value text default 'api_key=REDACTED')")
                with self.assertRaisesRegex(BehavioralExportError, "secret-like"):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
                self.assertFalse((root / "artifact").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            lock = project.parent / f".{PROJECT_ID}.memos-lifecycle.lock"
            before = (lock.stat().st_dev, lock.stat().st_ino, lock.stat().st_mtime_ns)
            export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            after = (lock.stat().st_dev, lock.stat().st_ino, lock.stat().st_mtime_ns)
            self.assertEqual(after, before)

    def test_raw_staged_database_secret_signature_refuses_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            original = __import__(
                "harness_manager.behavioral_export", fromlist=["_sqlite_backup"]
            )._sqlite_backup

            def inject(source: Path) -> bytes:
                return original(source) + b"\x00client_secret=REDACTED\x00"

            with patch("harness_manager.behavioral_export._sqlite_backup", side_effect=inject):
                with self.assertRaisesRegex(BehavioralExportError, "secret-like"):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertFalse((root / "artifact").exists())

    def test_relative_symlink_component_and_opened_parent_mode_are_rejected(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        module = __import__("harness_manager.behavioral_export", fromlist=["_reject_symlink_components"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "outside").mkdir()
            (root / "relative").symlink_to(root / "outside", target_is_directory=True)
            prior = Path.cwd()
            try:
                os.chdir(root)
                with self.assertRaises(BehavioralExportError):
                    module._reject_symlink_components(Path("relative") / "artifact")
            finally:
                os.chdir(prior)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            parent = root / "output"
            parent.mkdir(mode=0o777)
            os.chmod(parent, 0o777)
            with patch("harness_manager.behavioral_export._assert_secure_directory"):
                with self.assertRaisesRegex(BehavioralExportError, "destination parent.*unsafe"):
                    export_behavioral_artifact(project, parent / "artifact", PROJECT_ID, provenance="repo")

    def test_staged_snapshot_growth_and_post_copy_secret_scan_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)

            def oversized(_source: Path) -> bytes:
                return b"x" * (32 * 1024 * 1024 + 1)

            with patch("harness_manager.behavioral_export._sqlite_backup", side_effect=oversized):
                with self.assertRaisesRegex(BehavioralExportError, "snapshot.*size bound"):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertFalse((root / "artifact").exists())

    def test_destination_cannot_touch_runtime_or_new_parent_or_swapped_parent(self):
        if os.name == "nt":
            self.skipTest("symlink fixture is POSIX-specific")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            inside = project / "profiles" / PROJECT_ID / "memos-plugin" / "export"
            with self.assertRaisesRegex(BehavioralExportError, "inside behavioral runtime"):
                export_behavioral_artifact(project, inside, PROJECT_ID, provenance="repo")
            missing = root / "new-parent" / "artifact"
            with self.assertRaisesRegex(BehavioralExportError, "destination parent"):
                export_behavioral_artifact(project, missing, PROJECT_ID, provenance="repo")
            self.assertFalse(missing.parent.exists())

            parent = root / "output"
            outside = root / "outside"
            parent.mkdir()
            outside.mkdir()
            original = __import__("harness_manager.behavioral_export", fromlist=["_write_private_json"])._write_private_json

            def swap(path: Path, value: dict) -> None:
                original(path, value)
                parent.rename(root / "output-original")
                parent.symlink_to(outside, target_is_directory=True)

            with patch("harness_manager.behavioral_export._write_private_json", side_effect=swap):
                with self.assertRaisesRegex(BehavioralExportError, "destination parent changed"):
                    export_behavioral_artifact(project, parent / "artifact", PROJECT_ID, provenance="repo")
            self.assertEqual(list(outside.iterdir()), [])

    def test_real_linked_git_worktree_revision_is_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            repo = root / "repo"
            repo.mkdir()
            env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(root / "home")}
            for command in (
                ["git", "-C", str(repo), "init"],
                ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
                ["git", "-C", str(repo), "config", "user.name", "Test"],
            ):
                subprocess.run(command, env=env, check=True, capture_output=True, text=True)
            (repo / "tracked.txt").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], env=env, check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "test"], env=env, check=True, capture_output=True, text=True)
            worktree = root / "linked"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "linked", str(worktree)], env=env, check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "pack-refs", "--all", "--prune"], env=env, check=True, capture_output=True, text=True)
            revision = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"], env=env, check=True, capture_output=True, text=True).stdout.strip()
            artifact = export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo", repo_root=worktree)
            self.assertEqual(json.loads((artifact / "manifest.json").read_text())["repository_revision"], revision)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_project(root)
            with patch(
                "harness_manager.behavioral_export._read_skill_exact",
                return_value=b"tok" + b"en=changed-after-collection",
            ):
                with self.assertRaisesRegex(BehavioralExportError, "secret-like"):
                    export_behavioral_artifact(project, root / "artifact", PROJECT_ID, provenance="repo")
            self.assertFalse((root / "artifact").exists())


if __name__ == "__main__":
    unittest.main()
