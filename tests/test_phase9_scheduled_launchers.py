from __future__ import annotations

import ast
import json
import plistlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock

from harness_manager.scheduled_launchers import (
    AUTO_DREAM_LABEL, REVIEW_NOTIFY_LABEL, build_launch_agents, write_launch_agents,
)


ROOT = Path(__file__).resolve().parents[1]


def fixture_home() -> tempfile.TemporaryDirectory[str]:
    # /tmp is a symlink on macOS.  These tests intentionally exercise a real
    # component-by-component fixture root instead.
    return tempfile.TemporaryDirectory(dir=ROOT)


def load_orchestrate():
    spec = spec_from_file_location("phase9_scheduled_orchestrate", ROOT / ".agent/tools/memory_orchestrate.py")
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def topology(root: Path) -> dict[str, tuple[int, int, int]]:
    """Names plus mode/mtime/size, without following links."""
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, info.st_size)
    return result


class ScheduledLaunchersTest(unittest.TestCase):
    def test_generated_launchers_are_thin_deterministic_and_safe(self) -> None:
        agents = build_launch_agents("/usr/bin/python3", ROOT / ".agent")
        self.assertEqual(tuple(agents), (AUTO_DREAM_LABEL, REVIEW_NOTIFY_LABEL))
        expected = ((AUTO_DREAM_LABEL, 3), (REVIEW_NOTIFY_LABEL, 9))
        for label, hour in expected:
            raw = agents[label]
            plist = plistlib.loads(raw)
            self.assertEqual(plist["Label"], label)
            self.assertEqual(plist["StartCalendarInterval"], {"Hour": hour, "Minute": 0})
            self.assertEqual(plist["ProgramArguments"][0], "/usr/bin/python3")
            entrypoint = str(ROOT / ".agent/tools/memory_orchestrate.py")
            expected_argv = (["/usr/bin/python3", entrypoint, "maintain", "--stage-candidates", "--scheduled"]
                             if label == AUTO_DREAM_LABEL else
                             ["/usr/bin/python3", entrypoint, "review", "prepare", "--scheduled", "--notify"])
            self.assertEqual(plist["ProgramArguments"], expected_argv)
            if label == REVIEW_NOTIFY_LABEL:
                self.assertEqual(plist["ProgramArguments"][-2:], ["--scheduled", "--notify"])
            text = raw.decode("utf-8").lower()
            for forbidden in ("graduate.py", "accept", "evolution", "kill", "|", "python -c"):
                self.assertNotIn(forbidden, text)

    def test_fixture_write_is_owner_safe_and_never_touches_real_home(self) -> None:
        with fixture_home() as tmp:
            home = Path(tmp)
            written = write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            self.assertEqual(set(written), {AUTO_DREAM_LABEL, REVIEW_NOTIFY_LABEL})
            for path in written.values():
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent, home / "Library" / "LaunchAgents")

    def test_writer_rejects_relative_paths_and_existing_destination(self) -> None:
        with self.assertRaises(ValueError):
            build_launch_agents("python3", ROOT / ".agent")
        with fixture_home() as tmp:
            home = Path(tmp)
            write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            with self.assertRaises(FileExistsError):
                write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")

    def test_symlinked_library_and_unsafe_home_are_rejected_without_outside_write(self) -> None:
        with fixture_home() as tmp:
            home = Path(tmp) / "home"; home.mkdir(mode=0o700)
            outside = Path(tmp) / "outside"; outside.mkdir()
            before = topology(outside)
            os.symlink(outside, home / "Library")
            with self.assertRaises((OSError, ValueError)):
                write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            self.assertEqual(topology(outside), before)

    def test_symlinked_launch_agents_and_destination_symlink_are_rejected(self) -> None:
        with fixture_home() as tmp:
            home = Path(tmp) / "home"; home.mkdir(mode=0o700)
            outside = Path(tmp) / "outside"; outside.mkdir()
            before = topology(outside)
            library = home / "Library"; library.mkdir(mode=0o700)
            os.symlink(outside, library / "LaunchAgents")
            with self.assertRaises((OSError, ValueError)):
                write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            self.assertEqual(topology(outside), before)
            (library / "LaunchAgents").unlink()
            agents = library / "LaunchAgents"; agents.mkdir(mode=0o700)
            os.symlink(outside / "missing", agents / f"{AUTO_DREAM_LABEL}.plist")
            with self.assertRaises(FileExistsError):
                write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            self.assertEqual(topology(outside), before)

    def test_writer_rejects_unsafe_directories_and_symlinked_entrypoint_inputs(self) -> None:
        with fixture_home() as tmp:
            home = Path(tmp) / "home"; home.mkdir(mode=0o700)
            library = home / "Library"; library.mkdir(mode=0o777)
            with self.assertRaises(ValueError):
                write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
        with fixture_home() as tmp:
            home = Path(tmp) / "home"; home.mkdir(mode=0o700)
            library = home / "Library"; library.mkdir(mode=0o700)
            library_inode = library.lstat().st_ino
            module = __import__("harness_manager.scheduled_launchers", fromlist=["_open_dir"])
            real_fstat = module.os.fstat
            def foreign_library(fd):
                value = real_fstat(fd)
                if value.st_ino == library_inode:
                    fields = list(value); fields[4] = value.st_uid + 1
                    return os.stat_result(fields)
                return value
            with mock.patch.object(module.os, "fstat", side_effect=foreign_library):
                with self.assertRaises(ValueError):
                    write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
        with fixture_home() as tmp:
            home = Path(tmp) / "home"; home.mkdir(mode=0o700)
            with mock.patch("harness_manager.scheduled_launchers.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaises(ValueError):
                    write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
        with fixture_home() as tmp:
            source = Path(tmp) / "source"; source.mkdir()
            link = Path(tmp) / "python"; os.symlink(source, link)
            with self.assertRaises(ValueError):
                build_launch_agents(link, ROOT / ".agent")
            agent_link = Path(tmp) / "agent"; os.symlink(ROOT / ".agent", agent_link)
            with self.assertRaises(ValueError):
                build_launch_agents("/usr/bin/python3", agent_link)

    def test_bad_inputs_fail_before_fixture_topology_creation(self) -> None:
        with fixture_home() as tmp:
            home = Path(tmp) / "home"; home.mkdir(mode=0o700)
            with self.assertRaises(ValueError):
                write_launch_agents(home, "relative-python", ROOT / ".agent")
            self.assertFalse((home / "Library").exists())
            with self.assertRaises(ValueError):
                build_launch_agents("/usr/bin/python3\x00bad", ROOT / ".agent")

    def test_short_writes_complete_and_write_failure_cleans_temps(self) -> None:
        with fixture_home() as tmp:
            home = Path(tmp)
            real_write = os.write
            def short_write(fd, data):
                return real_write(fd, data[: max(1, len(data) // 7)])
            with mock.patch("harness_manager.scheduled_launchers.os.write", side_effect=short_write):
                written = write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            expected = build_launch_agents("/usr/bin/python3", ROOT / ".agent")
            for label, output in written.items():
                self.assertEqual(output.read_bytes(), expected[label])
        with fixture_home() as tmp:
            home = Path(tmp)
            with mock.patch("harness_manager.scheduled_launchers.os.write", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            agents = home / "Library" / "LaunchAgents"
            # Gate 8 is a fixture writer, not the Gate 12 lifecycle
            # transaction: safe directories it created may remain.
            self.assertTrue(agents.is_dir())
            self.assertFalse(any(path.name.endswith(".tmp") for path in agents.iterdir()))
            self.assertFalse(any(path.name.endswith(".plist") for path in agents.iterdir()))

    def test_temp_cleanup_does_not_delete_substituted_victim(self) -> None:
        with fixture_home() as tmp:
            home = Path(tmp)
            victim = home / "victim"
            victim.write_text("do-not-delete", encoding="utf-8")
            observed: dict[str, str] = {}
            original = __import__("harness_manager.scheduled_launchers", fromlist=["_write_all"])._write_all
            def replace_temp(fd, raw):
                original(fd, raw)
                agents = home / "Library" / "LaunchAgents"
                temp = next(path for path in agents.iterdir() if path.name.endswith(".tmp"))
                observed["name"] = temp.name
                temp.unlink()
                os.link(victim, agents / temp.name)
                raise OSError("injected replacement")
            with mock.patch("harness_manager.scheduled_launchers._write_all", side_effect=replace_temp):
                with self.assertRaises(OSError):
                    write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            self.assertEqual(victim.read_text(encoding="utf-8"), "do-not-delete")
            self.assertTrue((home / "Library" / "LaunchAgents" / observed["name"]).exists())

    def test_atomic_no_clobber_preserves_racing_destination(self) -> None:
        with fixture_home() as tmp:
            home = Path(tmp)
            module = __import__("harness_manager.scheduled_launchers", fromlist=["_publish_temp"])
            original = module._publish_temp
            def race(agents_fd, temp, final, identity):
                fd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=agents_fd)
                try:
                    os.write(fd, b"sentinel")
                finally:
                    os.close(fd)
                return original(agents_fd, temp, final, identity)
            with mock.patch.object(module, "_publish_temp", side_effect=race):
                with self.assertRaises(FileExistsError):
                    write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            raced = home / "Library" / "LaunchAgents" / f"{AUTO_DREAM_LABEL}.plist"
            self.assertEqual(raced.read_bytes(), b"sentinel")

    def test_partial_publication_rollback_fsyncs_directory(self) -> None:
        with fixture_home() as tmp:
            home = Path(tmp)
            module = __import__("harness_manager.scheduled_launchers", fromlist=["_publish_temp"])
            original_publish = module._publish_temp
            original_fsync = module.os.fsync
            publication_count = 0
            directory_fsyncs = 0

            def fail_second(agents_fd, temp, final, identity):
                nonlocal publication_count
                publication_count += 1
                if publication_count == 2:
                    raise OSError("injected second publication failure")
                return original_publish(agents_fd, temp, final, identity)

            def observe_fsync(fd):
                nonlocal directory_fsyncs
                info = os.fstat(fd)
                if stat.S_ISDIR(info.st_mode):
                    directory_fsyncs += 1
                return original_fsync(fd)

            with mock.patch.object(module, "_publish_temp", side_effect=fail_second), \
                 mock.patch.object(module.os, "fsync", side_effect=observe_fsync):
                with self.assertRaisesRegex(OSError, "second publication"):
                    write_launch_agents(home, "/usr/bin/python3", ROOT / ".agent")
            agents = home / "Library" / "LaunchAgents"
            self.assertFalse(any(path.suffix == ".plist" for path in agents.iterdir()))
            self.assertGreaterEqual(directory_fsyncs, 3)


class ScheduledCommandsTest(unittest.TestCase):
    def test_parser_requires_every_scheduled_intent_flag(self) -> None:
        script = ROOT / ".agent/tools/memory_orchestrate.py"
        for argv in (("maintain",), ("maintain", "--scheduled"),
                     ("review", "prepare"), ("review", "prepare", "--scheduled")):
            result = subprocess.run([sys.executable, str(script), *argv], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)

    def test_scheduled_maintenance_uses_exact_bounded_subprocess_and_generic_errors(self) -> None:
        module = load_orchestrate()
        completed = mock.Mock(
            returncode=0,
            stdout=b'{"candidate_count":2,"pending_count":3,"rejection_count":1}',
        )
        with mock.patch.object(module.subprocess, "run", return_value=completed) as run:
            self.assertEqual(module.scheduled_maintain_command(), {
                "status": "staged", "authority": "no_auto_accept",
                "candidate_count": 2, "rejection_count": 1,
            })
        self.assertEqual(
            run.call_args.args[0],
            [sys.executable, str(module.AGENT_ROOT / "memory" / "auto_dream.py")],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], module.AGENT_ROOT)
        self.assertIs(run.call_args.kwargs["stdout"], module.subprocess.PIPE)
        self.assertIs(run.call_args.kwargs["stderr"], module.subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], 300)
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertEqual(
            run.call_args.kwargs["env"]["AGENTIC_SCHEDULER_RESULT"], "1",
        )
        for result in (mock.Mock(returncode=9), module.subprocess.TimeoutExpired(["x"], 300)):
            with mock.patch.object(module.subprocess, "run", side_effect=result if isinstance(result, BaseException) else None,
                            return_value=None if isinstance(result, BaseException) else result):
                with self.assertRaisesRegex(RuntimeError, "^scheduled maintenance failed$"):
                    module.scheduled_maintain_command()

    def test_main_returns_policy_error_not_traceback_for_scheduled_failure(self) -> None:
        import io
        module = load_orchestrate()
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["memory_orchestrate.py", "maintain", "--stage-candidates", "--scheduled"]), \
             mock.patch.object(module, "scheduled_maintain_command", side_effect=RuntimeError("scheduled maintenance failed")), \
             mock.patch.object(sys, "stderr", stderr):
            self.assertEqual(module.main(), 2)
        self.assertIn('"status": "error"', stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_review_queue_is_presence_only_and_refuses_symlink_or_special_file(self) -> None:
        module = load_orchestrate()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            agent = Path(tmp) / ".agent"; (agent / "memory/working").mkdir(parents=True)
            queue = agent / "memory/working/REVIEW_QUEUE.md"
            queue.write_text("private candidate", encoding="utf-8")
            with mock.patch.object(module, "AGENT_ROOT", agent):
                original_lstat = Path.lstat
                with mock.patch.object(Path, "lstat", autospec=True, side_effect=original_lstat) as lstat:
                    value = module.scheduled_review_prepare_command()
                self.assertEqual(lstat.call_count, 1)
                self.assertEqual(
                    [call.args for call in lstat.call_args_list],
                    [(queue,)],
                )
                self.assertEqual(value, {
                    "status": "maintenance_stale_or_failed", "queue_present": True,
                    "authority": "no_auto_accept",
                    "snapshot": {"intent": "bounded_metadata_only", "max_candidates": 10},
                    "notification": "disabled",
                })
                queue.unlink(); os.symlink(Path(tmp) / "outside", queue)
                self.assertFalse(module.scheduled_review_prepare_command()["queue_present"])
                queue.unlink(); os.mkfifo(queue)
                self.assertFalse(module.scheduled_review_prepare_command()["queue_present"])

    def test_review_policy_degrades_for_missing_failed_or_stale_maintenance_without_reading_queue(self) -> None:
        module = load_orchestrate()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            agent = Path(tmp) / ".agent"
            working = agent / "memory" / "working"
            working.mkdir(parents=True)
            queue = working / "REVIEW_QUEUE.md"
            secret = "candidate body must never reach scheduled status"
            queue.write_text(secret, encoding="utf-8")
            state = agent / "memory" / "dream-state.json"
            now = "2026-07-29T09:00:00+00:00"
            with mock.patch.object(module, "AGENT_ROOT", agent):
                with mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    missing = module.scheduled_review_prepare_command()
                self.assertEqual(missing["status"], "maintenance_stale_or_failed")
                self.assertTrue(missing["queue_present"])
                self.assertEqual(missing["snapshot"], {
                    "intent": "bounded_metadata_only", "max_candidates": 10,
                })
                self.assertNotIn(secret, repr(missing))

                state.write_text(json.dumps({
                    "schema_version": 1,
                    "last_status": "failure",
                    "last_success_at": "2026-07-29T03:00:00+00:00",
                }), encoding="utf-8")
                with mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    failed = module.scheduled_review_prepare_command()
                self.assertEqual(failed["status"], "maintenance_stale_or_failed")
                self.assertTrue(failed["queue_present"])

                state.write_text(json.dumps({
                    "schema_version": 1,
                    "last_status": "success",
                    "last_success_at": "2026-07-27T03:00:00+00:00",
                }), encoding="utf-8")
                with mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    stale = module.scheduled_review_prepare_command()
                self.assertEqual(stale["status"], "maintenance_stale_or_failed")
                self.assertTrue(stale["queue_present"])

                state.write_text(json.dumps({
                    "schema_version": 1,
                    "last_status": "success",
                    "last_success_at": "2026-07-29T03:00:00+00:00",
                }), encoding="utf-8")
                with mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    healthy = module.scheduled_review_prepare_command()
                self.assertEqual(healthy["status"], "review_ready")
                self.assertTrue(healthy["queue_present"])

                original_open = Path.open
                def refuse_queue_open(path, *args, **kwargs):
                    if path == queue:
                        raise AssertionError("scheduled policy must not read queue content")
                    return original_open(path, *args, **kwargs)
                with mock.patch.object(Path, "open", autospec=True, side_effect=refuse_queue_open), \
                     mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    self.assertEqual(module.scheduled_review_prepare_command()["status"], "review_ready")

                state.write_text(json.dumps({
                    "schema_version": 1,
                    "last_status": "success",
                    "last_success_at": "2026-07-30T03:00:00+00:00",
                }), encoding="utf-8")
                with mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    future = module.scheduled_review_prepare_command()
                self.assertEqual(future["status"], "maintenance_stale_or_failed")

                state.unlink()
                outside = Path(tmp) / "outside-state.json"
                outside.write_text(json.dumps({
                    "schema_version": 1,
                    "last_status": "success",
                    "last_success_at": "2026-07-29T03:00:00+00:00",
                }), encoding="utf-8")
                os.symlink(outside, state)
                with mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    linked = module.scheduled_review_prepare_command()
                self.assertEqual(linked["status"], "maintenance_stale_or_failed")

                state.unlink()
                state.write_text(json.dumps({
                    "schema_version": 1,
                    "last_status": "success",
                    "last_success_at": "2026-07-29T03:00:00+00:00",
                }), encoding="utf-8")
                queue.unlink()
                with mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    empty = module.scheduled_review_prepare_command()
                self.assertEqual(empty["status"], "no_review_queue")
                self.assertFalse(empty["queue_present"])

                local_config = agent / "memory/orchestration/scheduled-local.json"
                local_config.parent.mkdir(parents=True, exist_ok=True)
                source_config = json.loads(
                    (ROOT / ".agent/memory/orchestration/scheduled-local.default.json")
                    .read_text(encoding="utf-8")
                )
                source_config["notification"] = "requested"
                local_config.write_text(json.dumps(source_config), encoding="utf-8")
                with mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    requested = module.scheduled_review_prepare_command()
                self.assertEqual(requested["notification"], "requested_deferred")
                local_config.unlink()
                outside_config = Path(tmp) / "outside-config.json"
                outside_config.write_text(json.dumps(source_config), encoding="utf-8")
                os.symlink(outside_config, local_config)
                with mock.patch.object(module.scheduled_review_policy, "utc_now", return_value=now):
                    linked_config = module.scheduled_review_prepare_command()
                self.assertEqual(linked_config["notification"], "disabled")

    def test_generated_legacy_shim_execs_the_versioned_cli_with_exact_argv(self) -> None:
        module = __import__(
            "harness_manager.scheduled_launchers",
            fromlist=["build_review_compatibility_shim_from_state"],
        )
        runtime = __import__(
            "harness_manager.scheduled_runtime", fromlist=["select_runtime"],
        ).select_runtime()
        shim = module.build_review_compatibility_shim_from_state(
            {
                "orchestration": {
                    "scheduled_runtime": runtime.record(),
                },
            },
            ROOT / ".agent",
        )
        self.assertIsInstance(shim, bytes)
        tree = ast.parse(shim.decode("utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        exec_calls = [
            call for call in calls
            if isinstance(call.func, ast.Attribute) and call.func.attr == "execv"
        ]
        self.assertEqual(len(exec_calls), 1)
        call = exec_calls[0]
        self.assertIsInstance(call.func, ast.Attribute)
        self.assertEqual(call.func.attr, "execv")
        self.assertEqual(
            ast.literal_eval(call.args[1]),
            [
                runtime.path, str(ROOT / ".agent/tools/memory_orchestrate.py"),
                "review", "prepare", "--scheduled", "--notify",
            ],
        )
        source = shim.decode("utf-8").lower()
        self.assertNotIn("subprocess", source)
        self.assertNotIn("shell", source)
        self.assertNotIn("graduate", source)
        self.assertNotIn("accept", source)
        self.assertNotIn("kill", source)

    def test_scheduled_functions_have_no_acceptance_or_provider_call_surface(self) -> None:
        module = load_orchestrate()
        import builtins
        with mock.patch.object(
            module.subprocess, "run",
            return_value=mock.Mock(
                returncode=0,
                stdout=b'{"candidate_count":0,"pending_count":0,"rejection_count":0}',
            ),
        ) as run, \
             mock.patch.object(builtins, "__import__", wraps=builtins.__import__) as imports:
            module.scheduled_maintain_command()
        argv = run.call_args.args[0]
        self.assertEqual(argv, [sys.executable, str(module.AGENT_ROOT / "memory" / "auto_dream.py")])
        self.assertFalse(any(any(word in part.lower() for word in (
            "graduate", "accept", "evolution", "r7", "crg", "kill")) for part in argv))
        self.assertFalse(any("orchestration" in str(call.args[0]) for call in imports.call_args_list))
        value = module.scheduled_review_prepare_command()
        self.assertEqual(value["authority"], "no_auto_accept")

    def test_auto_dream_transitive_entrypoint_is_staging_only(self) -> None:
        """The scheduled child itself must have no graduation call/import."""
        source = (ROOT / ".agent/memory/auto_dream.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: dict[str, set[str]] = {}
        called_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.setdefault(node.module, set()).update(
                    alias.name for alias in node.names
                )
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)

        self.assertEqual(
            imported["promote"], {"cluster_and_extract", "write_candidates"}
        )
        self.assertNotIn("graduate", imported)
        self.assertTrue(
            {"write_candidates", "heuristic_check", "mark_rejected",
             "write_review_queue_summary"}.issubset(called_names)
        )
        self.assertTrue(
            called_names.isdisjoint({
                "graduate", "accept", "finalize_graduated",
                "finalize_provisional", "enable_evolution",
            })
        )


if __name__ == "__main__":
    unittest.main()
