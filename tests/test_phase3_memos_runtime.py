import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.memos_runtime import (
    MEMOS_PLUGIN_VERSION,
    bridge_command,
    build_memos_config,
    prepare_project_runtime,
    runtime_environment,
    runtime_paths,
    validate_project_id,
    write_config_atomic,
)
from harness_manager.memos_install import (
    LOCK_ASSET_DIR,
    MEMOS_PLUGIN_INTEGRITY,
    MEMOS_PLUGIN_SHASUM,
    install_verified_tarball,
    verify_tarball,
)


PROJECT_ID = "0123456789abcdef"


class MemosRuntimeTest(unittest.TestCase):
    def test_project_id_is_exactly_lowercase_hex(self):
        self.assertEqual(validate_project_id(PROJECT_ID), PROJECT_ID)
        for invalid in ("", "1234", "0123456789abcdeg", "0123456789ABCDEF", "x" * 16):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_project_id(invalid)

    def test_code_and_per_project_home_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = runtime_paths(root / "code", root / "state", PROJECT_ID)
            second = runtime_paths(root / "code", root / "state", "fedcba9876543210")
            self.assertEqual(first.plugin_dir, second.plugin_dir)
            self.assertNotEqual(first.memos_home, second.memos_home)
            self.assertNotEqual(first.home, second.home)
            self.assertEqual(
                first.memos_home,
                (root / "state" / PROJECT_ID / "profiles" / PROJECT_ID / "memos-plugin").resolve(),
            )
            self.assertFalse(first.home.is_relative_to(first.memos_home))
            self.assertFalse(first.memos_home.is_relative_to(first.plugin_dir))
            self.assertIn(MEMOS_PLUGIN_VERSION, first.plugin_dir.parts)

    def test_config_is_deterministic_private_and_has_no_secret_slots(self):
        config = build_memos_config(PROJECT_ID)
        self.assertEqual(config, build_memos_config(PROJECT_ID))
        self.assertFalse(config["telemetry"]["enabled"])
        self.assertFalse(config["hub"]["enabled"])
        self.assertNotIn("enabled", config["viewer"])
        self.assertFalse(config["viewer"]["openOnFirstTurn"])
        # Every generated key is part of MemOS 2.0.10's ConfigSchema. Viewer
        # shutdown itself is the bridge's --no-viewer launch flag.
        self.assertEqual(set(config["viewer"]), {"bindHost", "openOnFirstTurn"})
        self.assertEqual(set(config["bridge"]), {"mode"})
        self.assertEqual(config["embedding"]["provider"], "local")
        self.assertEqual(config["llm"], {
            "provider": "local_only", "fallbackToHost": False, "maxRetries": 0,
        })
        self.assertTrue(config["algorithm"]["lightweightMemory"]["enabled"])
        self.assertEqual(config["logging"]["file"]["retentionDays"], 30)
        self.assertFalse(config["logging"]["console"]["enabled"])
        self.assertFalse(config["logging"]["llmLog"]["enabled"])
        rendered = json.dumps(config, sort_keys=True).lower()
        self.assertNotRegex(rendered, r'"[^"\\]*(?:key|token|secret)[^"\\]*"\s*:')

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime" / "config.yaml"
            write_config_atomic(path, config)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), config)
            self.assertFalse(any(path.parent.glob(f".{path.name}.*.tmp")))

    def test_prepare_runtime_isolated_environment_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = prepare_project_runtime(root / "code", root / "state", PROJECT_ID)
            remembered = paths.memos_home / "data" / "remembered.txt"
            remembered.write_text("keep")
            paths = prepare_project_runtime(root / "code", root / "state", PROJECT_ID)
            env = runtime_environment(paths, {"PATH": "/bin", "HOME": "/leaky"})
            self.assertEqual(env["MEMOS_HOME"], str(paths.memos_home))
            self.assertEqual(env["MEMOS_CONFIG_FILE"], str(paths.config_file))
            self.assertEqual(env["HOME"], str(paths.home))
            self.assertEqual(env["PATH"], "/bin")
            self.assertEqual(remembered.read_text(), "keep")
            config = json.loads(paths.config_file.read_text())
            config["telemetry"]["enabled"] = True
            paths.config_file.write_text(json.dumps(config))
            with self.assertRaises(RuntimeError, msg="tampered config must fail closed"):
                prepare_project_runtime(root / "code", root / "state", PROJECT_ID)
            prepare_project_runtime(
                root / "code", root / "state", PROJECT_ID,
                preserve_existing_config=False,
            )
            command = bridge_command(paths)
            self.assertIn("--no-viewer", command)
            self.assertIn(f"--home={paths.memos_home}", command)
            self.assertTrue(command[1].endswith("/dist/bridge.cjs"))
            for directory in (paths.project_root, paths.memos_home, paths.home):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)


class MemosInstallTest(unittest.TestCase):
    def _artifact(self, root: Path, content: bytes = b"verified memos tarball") -> Path:
        artifact = root / "plugin.tgz"
        artifact.write_bytes(content)
        return artifact

    def test_verify_tarball_checks_sha512_integrity_and_sha1(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._artifact(Path(tmp))
            payload = artifact.read_bytes()
            integrity = "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()
            shasum = hashlib.sha1(payload).hexdigest()
            verified = verify_tarball(artifact, integrity=integrity, shasum=shasum)
            self.assertEqual(verified.sha1, shasum)
            with self.assertRaises(ValueError):
                verify_tarball(artifact, integrity=integrity, shasum="0" * 40)
            with self.assertRaises(ValueError):
                verify_tarball(artifact, integrity="sha512-AAAA", shasum=shasum)

    def test_pinned_metadata_matches_release(self):
        self.assertEqual(
            MEMOS_PLUGIN_INTEGRITY,
            "sha512-Rg2NIjGAObTC3zFQ4wOzB+hxR7qHvHWMVI5Nxc+7QEi5wpBUibkniz3SdHOPrbbCkqhatS0DjZ+aUexl/9Q+EA==",
        )
        self.assertEqual(MEMOS_PLUGIN_SHASUM, "d75850ce7340d56b8a255831969950b9fbf96995")
        lock = json.loads((LOCK_ASSET_DIR / "package-lock.json").read_text())
        locked = lock["packages"]["node_modules/@memtensor/memos-local-plugin"]
        self.assertEqual(locked["version"], MEMOS_PLUGIN_VERSION)
        self.assertEqual(locked["resolved"], "file:plugin.tgz")
        self.assertEqual(locked["integrity"], MEMOS_PLUGIN_INTEGRITY)

    def test_installer_requires_node_20_and_uses_local_tarball_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = self._artifact(root)
            payload = artifact.read_bytes()
            integrity = "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()
            shasum = hashlib.sha1(payload).hexdigest()
            lock_dir = root / "lock"
            lock_dir.mkdir()
            (lock_dir / "package.json").write_text(json.dumps({
                "dependencies": {"@memtensor/memos-local-plugin": "file:plugin.tgz"},
            }))
            (lock_dir / "package-lock.json").write_text(json.dumps({
                "packages": {"node_modules/@memtensor/memos-local-plugin": {
                    "version": MEMOS_PLUGIN_VERSION,
                    "resolved": "file:plugin.tgz",
                    "integrity": integrity,
                }},
            }))
            calls = []
            original_secret = os.environ.get("OPENAI_API_KEY")
            os.environ["OPENAI_API_KEY"] = "must-not-reach-install-scripts"
            self.addCleanup(
                lambda: os.environ.pop("OPENAI_API_KEY", None)
                if original_secret is None
                else os.environ.__setitem__("OPENAI_API_KEY", original_secret)
            )

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                prefix = Path(command[command.index("--prefix") + 1])
                package = prefix / "node_modules" / "@memtensor" / "memos-local-plugin"
                (package / "dist").mkdir(parents=True)
                (package / "package.json").write_text(json.dumps({"version": MEMOS_PLUGIN_VERSION}))
                (package / "dist" / "bridge.cjs").write_text("// built")
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with self.assertRaises(RuntimeError):
                install_verified_tarball(
                    artifact, root / "code", integrity=integrity, shasum=shasum,
                    node_version="v19.9.0", runner=fake_run, lock_asset_dir=lock_dir,
                )
            result = install_verified_tarball(
                artifact, root / "code", integrity=integrity, shasum=shasum,
                node_version="v20.11.1", npm_command=("offline-npm",), runner=fake_run,
                lock_asset_dir=lock_dir,
            )
            command = calls[0][0]
            self.assertEqual(command[0], "offline-npm")
            self.assertEqual(command[1], "ci")
            self.assertEqual(
                json.loads((result.plugin_dir / "package.json").read_text())["dependencies"],
                {"@memtensor/memos-local-plugin": "file:plugin.tgz"},
            )
            self.assertFalse((result.plugin_dir / "plugin.tgz").exists())
            install_env = calls[0][1]["env"]
            self.assertNotIn("OPENAI_API_KEY", install_env)
            self.assertNotEqual(install_env.get("HOME"), os.environ.get("HOME"))
            self.assertNotIn("@memtensor/memos-local-plugin@2.0.10", command)
            self.assertEqual(result.version, MEMOS_PLUGIN_VERSION)
            self.assertEqual(stat.S_IMODE(result.plugin_dir.stat().st_mode), 0o555)
            self.assertTrue((result.plugin_dir / ".agentic-stack-files.json").is_file())

            data = root / "state" / PROJECT_ID / "keep.db"
            data.parent.mkdir(parents=True)
            data.write_text("keep")
            again = install_verified_tarball(
                artifact, root / "code", integrity=integrity, shasum=shasum,
                node_version="20.0.0", npm_command=("offline-npm",), runner=fake_run,
                lock_asset_dir=lock_dir,
            )
            self.assertTrue(again.already_installed)
            self.assertEqual(len(calls), 1)
            self.assertEqual(data.read_text(), "keep")
            node_modules = result.plugin_dir / "node_modules"
            os.chmod(node_modules, 0o755)
            extra = node_modules / "unattested.js"
            extra.write_text("arbitrary executable code")
            extra.chmod(0o444)
            os.chmod(node_modules, 0o555)
            with self.assertRaisesRegex(RuntimeError, "inventory mismatch"):
                install_verified_tarball(
                    artifact, root / "code", integrity=integrity, shasum=shasum,
                    node_version="20.0.0", npm_command=("offline-npm",), runner=fake_run,
                    lock_asset_dir=lock_dir,
                )


if __name__ == "__main__":
    unittest.main()
