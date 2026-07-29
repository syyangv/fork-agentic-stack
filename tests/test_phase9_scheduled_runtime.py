"""Gate 9 shared scheduled-Python selection and read-only diagnostics."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_manager import cli, doctor, install as install_mod, schema, upgrade as upgrade_mod
from harness_manager import scheduled_launchers, scheduled_runtime


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(sys.executable).resolve(strict=True)


class ScheduledRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = schema.validate(ROOT / "adapters/claude-code/adapter.json")

    def install(self, target: Path, *, profile: str = "standard", runtime: str | None = None) -> None:
        install_mod.install(
            manifest=self.manifest, target_root=target,
            adapter_dir=ROOT / "adapters/claude-code", stack_root=ROOT,
            profile=profile, scheduled_python=runtime, log=lambda _line: None,
        )

    @staticmethod
    def state(target: Path) -> dict:
        return json.loads((target / ".agent/install.json").read_text(encoding="utf-8"))

    @staticmethod
    def agent_files(target: Path) -> dict[str, bytes]:
        agent = target / ".agent"
        return {
            path.relative_to(agent).as_posix(): path.read_bytes()
            for path in agent.rglob("*")
            if path.is_file() and not path.name.endswith(".lock") and "__pycache__" not in path.parts
        }

    def test_runtime_validation_requires_real_absolute_executable_and_supported_python(self) -> None:
        selected = scheduled_runtime.select_runtime(RUNTIME)
        self.assertEqual(selected.path, str(RUNTIME))
        self.assertRegex(selected.version, r"^3\.(?:9|1[0-4])\.\d+$")
        if sys.platform == "darwin":
            system = scheduled_runtime.select_runtime("/usr/bin/python3")
            self.assertRegex(system.version, r"^3\.(?:9|1[0-4])\.\d+$")

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            regular = root / "not-executable"
            regular.write_text("not python", encoding="utf-8")
            regular.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "regular executable"):
                scheduled_runtime.select_runtime(regular)
            regular.chmod(0o700)
            with mock.patch.object(scheduled_runtime, "_python_version", return_value=(3, 8, 20)):
                with self.assertRaisesRegex(ValueError, "supported Python"):
                    scheduled_runtime.select_runtime(regular)
            with mock.patch.object(scheduled_runtime, "_python_version", return_value=(3, 9, 22)):
                self.assertEqual(
                    scheduled_runtime.select_runtime(regular).version, "3.9.22",
                )
            missing = root / "missing"
            with self.assertRaisesRegex(ValueError, "exist"):
                scheduled_runtime.select_runtime(missing)
            link = root / "python-link"
            os.symlink(regular, link)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                scheduled_runtime.select_runtime(link)
            linked_dir = root / "linked-dir"
            os.symlink(root, linked_dir)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                scheduled_runtime.select_runtime(linked_dir / "not-executable")
        with self.assertRaisesRegex(ValueError, "absolute"):
            scheduled_runtime.select_runtime("python3")

    def test_installer_persists_one_runtime_and_both_jobs_use_that_exact_value(self) -> None:
        for profile in ("standard", "minimal"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                self.install(target, profile=profile, runtime=str(RUNTIME))
                recorded = self.state(target)["orchestration"]["scheduled_runtime"]
                self.assertEqual(recorded, scheduled_runtime.select_runtime(RUNTIME).record())
                jobs = scheduled_launchers.build_launch_agents_from_state(
                    self.state(target), ROOT / ".agent",
                )
                for raw in jobs.values():
                    self.assertEqual(
                        __import__("plistlib").loads(raw)["ProgramArguments"][0],
                        recorded["path"],
                    )

    def test_cli_accepts_one_explicit_runtime_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            with mock.patch.object(cli, "_maybe_run_onboard", return_value=0):
                self.assertEqual(
                    cli.main(["claude-code", str(target), "--python", str(RUNTIME), "--yes"]),
                    0,
                )
            self.assertEqual(
                self.state(target)["orchestration"]["scheduled_runtime"]["path"], str(RUNTIME),
            )

    def test_install_and_upgrade_fail_before_mutation_for_malformed_or_unsupported_recorded_runtime(self) -> None:
        for record in (
            {"path": "python3", "version": "3.14.0"},
            {"path": str(RUNTIME), "version": "3.9.22"},
            {"path": str(RUNTIME), "version": "not-a-version"},
        ):
            with self.subTest(record=record), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                self.install(target)
                document = self.state(target)
                document["orchestration"]["scheduled_runtime"] = record
                state_path = target / ".agent/install.json"
                state_path.write_text(json.dumps(document), encoding="utf-8")
                before = self.agent_files(target)

                with self.assertRaisesRegex(ValueError, "scheduled Python runtime"):
                    self.install(target)
                self.assertEqual(self.agent_files(target), before)

                self.assertEqual(upgrade_mod.upgrade(target, ROOT, yes=True, log=lambda _line: None), 2)
                self.assertEqual(self.agent_files(target), before)

    def test_doctor_reports_runtime_state_and_drift_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target)
            before = self.agent_files(target)
            status, lines = doctor._audit_orchestration(target, stack_root=ROOT, environ={})
            self.assertNotEqual(status, doctor.RED, "\n".join(lines))
            self.assertIn("scheduled Python runtime", "\n".join(lines))
            self.assertEqual(self.agent_files(target), before)

            document = self.state(target)
            document["orchestration"]["scheduled_runtime"]["version"] = "3.10.0"
            (target / ".agent/install.json").write_text(json.dumps(document), encoding="utf-8")
            before = self.agent_files(target)
            status, lines = doctor._audit_orchestration(target, stack_root=ROOT, environ={})
            self.assertEqual(status, doctor.RED)
            self.assertIn("runtime version drift", "\n".join(lines))
            self.assertEqual(self.agent_files(target), before)

    def test_version_query_isolated_from_target_code(self) -> None:
        runtime = RUNTIME
        with mock.patch.object(scheduled_runtime.subprocess, "run", wraps=scheduled_runtime.subprocess.run) as run:
            scheduled_runtime.select_runtime(runtime)
        call = run.call_args
        self.assertEqual(call.args[0][:2], [str(runtime), "-I"])
        self.assertNotIn(str(ROOT / ".agent"), call.args[0])
        self.assertEqual(call.kwargs["cwd"], "/")
        self.assertEqual(
            call.kwargs["env"],
            {
                "PATH": os.defpath,
                "PYTHONNOUSERSITE": "1",
                "TMPDIR": tempfile.gettempdir(),
            },
        )

    def test_project_local_runtime_is_rejected_without_execution(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            target = Path(tmp)
            sentinel = target / "python"
            marker = target / "executed"
            sentinel.write_text(
                "#!/bin/sh\nprintf touched > \"$1\"\n",
                encoding="utf-8",
            )
            sentinel.chmod(0o700)
            before = self.agent_files(target)
            with self.assertRaisesRegex(ValueError, "outside project"):
                self.install(target, runtime=str(sentinel))
            self.assertEqual(self.agent_files(target), before)
            self.assertFalse(marker.exists())

            self.install(target)
            document = self.state(target)
            document["orchestration"]["scheduled_runtime"] = {
                "path": str(sentinel), "version": "3.9.0",
            }
            (target / ".agent/install.json").write_text(
                json.dumps(document), encoding="utf-8",
            )
            status, lines = doctor._audit_orchestration(
                target, stack_root=ROOT, environ={},
            )
            self.assertEqual(status, doctor.RED)
            self.assertIn("outside project", "\n".join(lines))
            self.assertFalse(marker.exists())

    def test_legacy_upgrade_selects_runtime_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target)
            document = self.state(target)
            document.pop("orchestration")
            state_path = target / ".agent/install.json"
            state_path.write_text(json.dumps(document), encoding="utf-8")
            deployed = target / ".agent/tools/memory_orchestrate.py"
            deployed.write_text("legacy\n", encoding="utf-8")
            before = self.agent_files(target)
            with mock.patch.object(
                scheduled_runtime, "select_runtime",
                side_effect=ValueError("scheduled Python runtime unavailable"),
            ):
                self.assertEqual(
                    upgrade_mod.upgrade(
                        target, ROOT, yes=True, log=lambda _line: None,
                    ),
                    2,
                )
            self.assertEqual(self.agent_files(target), before)

    def test_powershell_forwards_explicit_runtime_parameter(self) -> None:
        script = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$Python", script)
        self.assertIn("@('--python', $Python)", script)


if __name__ == "__main__":
    unittest.main()
