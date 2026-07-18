import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".agent" / "harness" / "hooks"
HARNESS = ROOT / ".agent" / "harness"
MEMORY = ROOT / ".agent" / "memory"
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(MEMORY))

from orchestration_event import (  # noqa: E402
    CAPABILITIES,
    CaptureStatus,
    CorrelationStore,
    HookEventSpool,
    HookEventError,
    normalize_event,
)
from hooks.post_execution import log_execution  # noqa: E402


class HarnessCapabilityTest(unittest.TestCase):
    def test_manifest_declares_phase4_event_features(self):
        manifest = json.loads((ROOT / ".agent/infrastructure.json").read_text())
        self.assertEqual(manifest["orchestration_phase"], 4)
        self.assertTrue({
            "harness_event_normalization", "harness_event_correlation",
            "bounded_hook_delivery", "truthful_harness_capabilities",
        }.issubset(manifest["features"]))

    def test_every_shipped_adapter_has_an_explicit_truthful_matrix_row(self):
        adapters = {
            path.parent.name
            for path in (ROOT / "adapters").glob("*/adapter.json")
        }
        self.assertEqual(set(CAPABILITIES), adapters)
        self.assertEqual(
            set(next(iter(CAPABILITIES.values()))),
            {"user_prompt", "pre_tool", "post_tool", "feedback", "subagent_start", "finalize"},
        )
        self.assertTrue(CAPABILITIES["claude-code"]["user_prompt"])
        self.assertTrue(CAPABILITIES["claude-code"]["subagent_start"])
        self.assertFalse(CAPABILITIES["claude-code"]["feedback"])
        self.assertFalse(CAPABILITIES["codex"]["post_tool"])


class EventNormalizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.agent_root = self.root / ".agent"
        self.store = CorrelationStore(self.agent_root)
        self.now = "2026-07-18T04:10:00Z"

    def tearDown(self):
        self.temp.cleanup()

    def test_claude_prompt_and_post_tool_share_run_and_parent_event(self):
        started = normalize_event(
            "claude-code", "user_prompt",
            {"session_id": "session-1", "prompt": "Fix the parser"},
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store,
        )
        completed = normalize_event(
            "claude-code", "post_tool",
            {
                "session_id": "session-1", "tool_use_id": "tool-1",
                "tool_name": "Bash", "tool_input": {"command": "pytest -q"},
                "tool_response": {"exit_code": 0, "output": "12 passed"},
            },
            repo_root=self.root, agent_root=self.agent_root,
            timestamp="2026-07-18T04:10:01Z", store=self.store,
        )
        self.assertEqual(started.event_type, "task.started")
        self.assertEqual(completed.event_type, "tool.completed")
        self.assertEqual(completed.run_id, started.run_id)
        self.assertEqual(completed.parent_event_ids, (started.event_id,))
        self.assertEqual(completed.payload["tool_name"], "Bash")
        self.assertEqual(completed.payload["output_summary"], "12 passed")

    def test_redacts_secrets_and_drops_raw_payload_surfaces(self):
        normalize_event(
            "claude-code", "user_prompt",
            {"session_id": "s", "prompt": "Deploy safely"},
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store,
        )
        event = normalize_event(
            "claude-code", "post_tool",
            {
                "session_id": "s", "tool_use_id": "t",
                "tool_name": "Bash",
                "tool_input": {"command": "echo token=plain-github-token"},
                "tool_response": {
                    "output": "authorization: Bearer abcdefghijklmnop",
                    "raw_prompt": "must never cross the boundary",
                    "environment": {"SECRET": "value"},
                },
            },
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store,
        )
        rendered = event.canonical_json()
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("plain-github-token", rendered)
        self.assertNotIn("abcdefghijklmnop", rendered)
        self.assertNotIn("must never", rendered)
        self.assertNotIn('"environment"', rendered)

    def test_prompt_content_never_crosses_the_event_or_correlation_boundary(self):
        confidential = "Confidential merger plan alpha"
        event = normalize_event(
            "claude-code", "user_prompt",
            {"session_id": "private", "prompt": confidential},
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store,
        )
        correlation = self.store.current("claude-code", "private")
        self.assertEqual(event.intent, "user request received")
        self.assertEqual(correlation.intent, "user request received")
        self.assertNotIn(confidential, event.canonical_json())
        self.assertNotIn(confidential, self.store._path("claude-code", "private").read_text())

    def test_legacy_correlation_intent_is_rewritten_before_reuse(self):
        legacy_prompt = "LEGACY_RAW_PROMPT_SECRET"
        self.store.root.mkdir(parents=True)
        path = self.store._path("claude-code", "legacy")
        path.write_text(json.dumps({
            "run_id": "run_" + "a" * 24,
            "session_id": "legacy",
            "start_event_id": "evt_" + "b" * 64,
            "intent": legacy_prompt,
            "finalizing": False,
        }))
        event = normalize_event(
            "claude-code", "post_tool",
            {
                "session_id": "legacy", "tool_use_id": "tool-legacy",
                "tool_name": "Bash", "tool_input": {"command": "true"},
                "tool_response": {"output": "ok"},
            },
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store,
        )
        self.assertEqual(event.intent, "user request received")
        self.assertNotIn(legacy_prompt, event.canonical_json())
        self.assertNotIn(legacy_prompt, path.read_text())

    def test_malformed_and_unsupported_inputs_are_rejected(self):
        with self.assertRaises(HookEventError):
            normalize_event(
                "claude-code", "post_tool", [], repo_root=self.root,
                agent_root=self.agent_root, timestamp=self.now, store=self.store,
            )
        with self.assertRaises(HookEventError):
            normalize_event(
                "codex", "post_tool", {}, repo_root=self.root,
                agent_root=self.agent_root, timestamp=self.now, store=self.store,
            )

    def test_finalize_uses_active_run_and_clears_it(self):
        started = normalize_event(
            "gemini", "user_prompt",
            {"session_id": "gem-session", "prompt": "Repair build"},
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store,
        )
        final = normalize_event(
            "gemini", "finalize", {"session_id": "gem-session"},
            repo_root=self.root, agent_root=self.agent_root,
            timestamp="2026-07-18T04:11:00Z", store=self.store,
        )
        self.assertEqual(final.event_type, "task.completed")
        self.assertEqual(final.run_id, started.run_id)
        self.assertEqual(final.parent_event_ids, (started.event_id,))
        self.assertTrue(self.store.current("gemini", "gem-session").finalizing)

    def test_native_fixture_payloads_normalize_for_every_hooked_harness(self):
        fixtures = {
            "claude-code": (
                {"session_id": "claude-1", "prompt": "Run tests"},
                {"session_id": "claude-1", "tool_use_id": "c-tool", "tool_name": "Bash", "tool_input": {"command": "pytest"}, "tool_response": {"output": "ok"}},
            ),
            "gemini": (
                {"session_id": "gemini-1", "prompt": "Run tests"},
                {"session_id": "gemini-1", "event_id": "g-tool", "tool_name": "run_shell_command", "tool_input": {"command": "pytest"}, "tool_response": {"output": "ok"}},
            ),
            "copilot-cli": (
                {"sessionId": "copilot-1", "prompt": "Run tests"},
                {"sessionId": "copilot-1", "eventId": "co-tool", "toolName": "bash", "toolArgs": json.dumps({"command": "pytest"}), "toolResult": {"resultType": "success", "textResultForLlm": "ok"}},
            ),
            "pi": (
                {"session_id": "pi-1", "prompt": "Run tests"},
                {"session_id": "pi-1", "event_id": "pi-tool", "tool_name": "bash", "tool_input": {"command": "pytest"}, "tool_response": {"output": "ok"}},
            ),
        }
        for harness, (start_payload, tool_payload) in fixtures.items():
            with self.subTest(harness=harness):
                started = normalize_event(
                    harness, "user_prompt", start_payload, repo_root=self.root,
                    agent_root=self.agent_root, timestamp=self.now, store=self.store,
                )
                tool = normalize_event(
                    harness, "post_tool", tool_payload, repo_root=self.root,
                    agent_root=self.agent_root, timestamp=self.now, store=self.store,
                )
                self.assertEqual(tool.run_id, started.run_id)
                self.assertEqual(tool.parent_event_ids, (started.event_id,))

    def test_explicit_feedback_for_hookless_harness_is_not_fabricated(self):
        started = normalize_event(
            "codex", "user_prompt", {"session_id": "manual", "prompt": "Fix it"},
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store, explicit=True,
        )
        feedback = normalize_event(
            "codex", "feedback", {
                "session_id": "manual", "polarity": "positive",
                "magnitude": 1, "rationale": "verified by user",
            }, repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store, explicit=True,
        )
        self.assertEqual(feedback.event_type, "feedback.recorded")
        self.assertEqual(feedback.run_id, started.run_id)
        self.assertEqual(feedback.payload["channel"], "explicit")

    def test_claude_pretool_and_subagent_are_distinct_event_types(self):
        normalize_event(
            "claude-code", "user_prompt", {"session_id": "s", "prompt": "Delegate"},
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store,
        )
        pre = normalize_event(
            "claude-code", "pre_tool", {"session_id": "s", "tool_use_id": "t", "tool_name": "Task", "tool_input": {"description": "review"}},
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store,
        )
        subagent = normalize_event(
            "claude-code", "subagent_start", {"session_id": "s", "event_id": "a", "agent_type": "reviewer", "description": "review patch"},
            repo_root=self.root, agent_root=self.agent_root,
            timestamp=self.now, store=self.store,
        )
        self.assertEqual(pre.event_type, "tool.started")
        self.assertEqual(subagent.event_type, "subagent.started")


class DeliveryAndCorrelationTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable to Windows")
    def test_runtime_directories_and_files_are_owner_only_even_with_open_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            agent_root = root / ".agent"
            store = CorrelationStore(agent_root)
            spool = HookEventSpool(agent_root)
            previous = os.umask(0)
            try:
                event = normalize_event(
                    "claude-code", "user_prompt",
                    {"session_id": "private", "prompt": "work"},
                    repo_root=root, agent_root=agent_root,
                    timestamp="2026-07-18T04:10:00Z", store=store,
                )
                pending = spool.enqueue(event)
                spool.write_health("healthy", "queued", pending=1)
                with spool.worker_lock() as acquired:
                    self.assertTrue(acquired)
            finally:
                os.umask(previous)

            directories = [
                store.root.parent.parent, store.root.parent, store.root,
                spool.root, spool.pending_dir,
            ]
            files = [store._path("claude-code", "private"), pending, spool.health_file, spool.lock_file]
            for path in directories:
                with self.subTest(directory=path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for path in files:
                with self.subTest(file=path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            for path in directories:
                path.chmod(0o755)
            for path in files:
                path.chmod(0o644)
            self.assertIsNotNone(store.current("claude-code", "private"))
            self.assertEqual(spool.pending(), [pending])
            spool.write_health("healthy", "repaired", pending=1)
            with spool.worker_lock() as acquired:
                self.assertTrue(acquired)
            for path in directories:
                with self.subTest(repaired_directory=path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for path in files:
                with self.subTest(repaired_file=path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_worker_environment_preserves_remote_based_project_identity(self):
        from orchestration_event import _worker_environment

        environment = _worker_environment(ROOT)
        self.assertEqual(environment["AGENTIC_PROJECT_ROOT"], str(ROOT))
        self.assertEqual(
            environment["AGENTIC_GIT_REMOTE"],
            subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=ROOT, text=True, capture_output=True, check=True,
            ).stdout.strip(),
        )

    def test_default_capture_durably_queues_without_waiting_for_provider(self):
        from orchestration_event import capture_hook_event

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            store = CorrelationStore(root / ".agent")
            spool = HookEventSpool(root / ".agent")
            starts = []
            began = time.monotonic()
            event, status = capture_hook_event(
                "claude-code", "user_prompt", {"session_id": "s", "prompt": "work"},
                repo_root=root, store=store, spool=spool,
                worker_starter=lambda project: starts.append(project),
            )
            self.assertLess(time.monotonic() - began, 0.2)
            self.assertEqual(status, CaptureStatus("captured", "queued"))
            self.assertEqual(len(spool.pending()), 1)
            self.assertEqual(starts, [root])
            self.assertEqual(json.loads(spool.pending()[0].read_text())["event_id"], event.event_id)

    def test_spool_worker_batches_and_moves_only_accepted_events(self):
        from orchestration_event import drain_spool

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            store = CorrelationStore(root / ".agent")
            spool = HookEventSpool(root / ".agent")
            first = normalize_event(
                "claude-code", "user_prompt", {"session_id": "s", "prompt": "work"},
                repo_root=root, timestamp="2026-07-18T04:10:00Z", store=store,
            )
            second = normalize_event(
                "claude-code", "post_tool", {"session_id": "s", "tool_use_id": "t", "tool_name": "Bash", "tool_input": {"command": "pytest"}, "tool_response": {"output": "ok"}},
                repo_root=root, timestamp="2026-07-18T04:10:01Z", store=store,
            )
            spool.enqueue(first)
            spool.enqueue(second)
            batches = []
            count = drain_spool(
                spool, lambda events, _timeout: batches.append(events) or {"status": "recorded", "health": {"status": "healthy"}},
            )
            self.assertEqual(count, 2)
            self.assertEqual([item["event_id"] for item in batches[0]], [first.event_id, second.event_id])
            self.assertEqual(spool.pending(), [])
            self.assertEqual(len(list(spool.delivered_dir.glob("*.json"))), 2)

    def test_lock_contending_worker_waits_and_drains_event_enqueued_during_handoff(self):
        from orchestration_event import drain_spool

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            store = CorrelationStore(root / ".agent")
            spool = HookEventSpool(root / ".agent")
            event = normalize_event(
                "claude-code", "user_prompt", {"session_id": "s", "prompt": "work"},
                repo_root=root, timestamp="2026-07-18T04:10:00Z", store=store,
            )
            batches = []
            with spool.worker_lock() as acquired:
                self.assertTrue(acquired)
                worker = threading.Thread(target=lambda: drain_spool(
                    spool,
                    lambda events, _timeout: batches.append(events) or {"status": "recorded"},
                ))
                worker.start()
                time.sleep(0.05)
                self.assertTrue(worker.is_alive(), "contending worker exited instead of awaiting handoff")
                spool.enqueue(event)
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual([[item["event_id"] for item in batch] for batch in batches], [[event.event_id]])
            self.assertEqual(spool.pending(), [])

    def test_spool_worker_preserves_degraded_provider_health_after_drain(self):
        from orchestration_event import drain_spool

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            store = CorrelationStore(root / ".agent")
            spool = HookEventSpool(root / ".agent")
            spool.enqueue(normalize_event(
                "claude-code", "user_prompt", {"session_id": "s", "prompt": "work"},
                repo_root=root, timestamp="2026-07-18T04:10:00Z", store=store,
            ))
            drain_spool(
                spool,
                lambda _events, _timeout: {
                    "status": "recorded",
                    "health": {"status": "degraded", "warnings": ["behavioral_unavailable"]},
                },
            )
            health = json.loads(spool.health_file.read_text())
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(health["reason"], "behavioral_unavailable")
            self.assertEqual(health["pending"], 0)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable to Windows")
    def test_legacy_spool_events_are_sanitized_and_historical_modes_repaired(self):
        from orchestration.contracts import EventEnvelope

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool = HookEventSpool(root / ".agent")
            legacy_prompt = "LEGACY_RAW_PROMPT_SECRET"
            legacy = EventEnvelope.create(
                idempotency_key="legacy:stable", timestamp="2026-07-18T04:10:00Z",
                event_type="task.started", project_id="a" * 16, repo_root=str(root),
                revision=None, harness="claude-code", run_id="run_" + "b" * 24,
                session_id="legacy", actor="user", intent=legacy_prompt,
                payload={"source_signal": "user_prompt"},
            )
            for directory in (spool.pending_dir, spool.delivered_dir):
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(0o755)
                path = directory / f"20260718041000-{legacy.event_id}.json"
                path.write_text(legacy.canonical_json())
                path.chmod(0o644)
            malformed = spool.pending_dir / "malformed.json"
            malformed.write_text(legacy_prompt)
            malformed.chmod(0o644)

            pending = spool.pending()
            self.assertEqual(len(pending), 1)
            quarantined = list(spool.quarantine_dir.iterdir())
            self.assertEqual([path.name for path in quarantined], ["malformed.json"])
            self.assertEqual(stat.S_IMODE(spool.quarantine_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(quarantined[0].stat().st_mode), 0o600)
            for directory in (spool.pending_dir, spool.delivered_dir):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                files = list(directory.glob("*.json"))
                self.assertEqual(len(files), 1)
                self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
                rendered = files[0].read_text()
                self.assertNotIn(legacy_prompt, rendered)
                self.assertEqual(
                    EventEnvelope.from_external(json.loads(rendered)).intent,
                    "user request received",
                )

    def test_timeout_is_degraded_and_bounded(self):
        def slow(_event, _timeout):
            time.sleep(0.2)
            return {"status": "recorded"}

        from orchestration_event import deliver_with_timeout

        started = time.monotonic()
        result = deliver_with_timeout({"event_id": "evt"}, slow, timeout=0.02)
        elapsed = time.monotonic() - started
        self.assertEqual(result, CaptureStatus("degraded", "delivery_timeout"))
        self.assertLess(elapsed, 0.15)

    def test_correlation_lock_contention_is_bounded_on_the_hook_path(self):
        from orchestration_event import _ensure_private_tree, _exclusive_file_lock, capture_hook_event

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            store = CorrelationStore(root / ".agent")
            _ensure_private_tree(store.root)
            with _exclusive_file_lock(store._lock_path("claude-code", "s")) as acquired:
                self.assertTrue(acquired)
                started = time.monotonic()
                event, status = capture_hook_event(
                    "claude-code", "user_prompt", {"session_id": "s", "prompt": "work"},
                    repo_root=root, store=store, deliverer=lambda *_args: {"status": "recorded"},
                )
                elapsed = time.monotonic() - started
            self.assertIsNone(event)
            self.assertEqual(status, CaptureStatus("degraded", "normalization_error"))
            self.assertLess(elapsed, 0.75)

    def test_slow_git_enrichment_shares_one_subsecond_budget_before_enqueue(self):
        from orchestration_event import capture_hook_event

        def slow_git(*_args, timeout, **_kwargs):
            time.sleep(timeout + 0.01)
            raise subprocess.TimeoutExpired("git", timeout)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            store = CorrelationStore(root / ".agent")
            spool = HookEventSpool(root / ".agent")
            started = time.monotonic()
            with mock.patch("orchestration_event.subprocess.run", side_effect=slow_git):
                event, status = capture_hook_event(
                    "claude-code", "user_prompt", {"session_id": "s", "prompt": "work"},
                    repo_root=root, store=store, spool=spool, worker_starter=lambda _root: None,
                )
            elapsed = time.monotonic() - started
            self.assertEqual(status, CaptureStatus("captured", "queued"))
            self.assertEqual(len(spool.pending()), 1)
            self.assertEqual(spool.pending()[0].name.split("-", 1)[1], f"{event.event_id}.json")
            self.assertLess(elapsed, 1.0)

            started = time.monotonic()
            with mock.patch("orchestration_event.subprocess.run", side_effect=slow_git):
                final, final_status = capture_hook_event(
                    "claude-code", "finalize", {"session_id": "s"},
                    repo_root=root, store=store, spool=spool, worker_starter=lambda _root: None,
                )
            final_elapsed = time.monotonic() - started
            self.assertEqual(final_status, CaptureStatus("captured", "queued"))
            self.assertEqual(final.run_id, event.run_id)
            self.assertLess(final_elapsed, 1.0)

    def test_provider_degraded_health_is_visible_in_capture_status(self):
        from orchestration_event import deliver_with_timeout

        status = deliver_with_timeout(
            {"event_id": "evt"},
            lambda _event, _timeout: {
                "status": "recorded",
                "health": {"status": "degraded", "warnings": ["behavioral_unavailable"]},
            },
            timeout=0.1,
        )
        self.assertEqual(status, CaptureStatus("degraded", "provider:behavioral_unavailable"))

    def test_successful_finalization_clears_correlation_but_timeout_preserves_it(self):
        from orchestration_event import capture_hook_event

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            store = CorrelationStore(root / ".agent")
            normalize_event(
                "claude-code", "user_prompt", {"session_id": "s", "prompt": "work"},
                repo_root=root, agent_root=root / ".agent",
                timestamp="2026-07-18T04:10:00Z", store=store,
            )

            def timeout_delivery(_event, _timeout):
                time.sleep(0.1)
                return {"status": "recorded"}

            _, status = capture_hook_event(
                "claude-code", "finalize", {"session_id": "s"},
                timeout=0.01, repo_root=root, store=store,
                deliverer=timeout_delivery,
            )
            self.assertEqual(status.status, "degraded")
            self.assertTrue(store.current("claude-code", "s").finalizing)

            _, status = capture_hook_event(
                "claude-code", "finalize", {"session_id": "s"},
                timeout=0.1, repo_root=root, store=store,
                deliverer=lambda _event, _timeout: {"status": "recorded"},
            )
            self.assertEqual(status.status, "captured")
            self.assertIsNone(store.current("claude-code", "s"))

    def test_new_prompt_replaces_a_failed_finalization_without_orphaning_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            store = CorrelationStore(root / ".agent")
            first = normalize_event(
                "gemini", "user_prompt", {"session_id": "s", "prompt": "first"},
                repo_root=root, timestamp="2026-07-18T04:10:00Z", store=store,
            )
            normalize_event(
                "gemini", "finalize", {"session_id": "s"},
                repo_root=root, timestamp="2026-07-18T04:11:00Z", store=store,
            )
            second = normalize_event(
                "gemini", "user_prompt", {"session_id": "s", "prompt": "second"},
                repo_root=root, timestamp="2026-07-18T04:12:00Z", store=store,
            )
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertFalse(store.current("gemini", "s").finalizing)

    def test_old_finalizer_cannot_clear_a_new_run_started_during_worker_handoff(self):
        from orchestration_event import capture_hook_event

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            store = CorrelationStore(root / ".agent")
            spool = HookEventSpool(root / ".agent")
            first = normalize_event(
                "claude-code", "user_prompt", {"session_id": "s", "prompt": "first"},
                repo_root=root, timestamp="2026-07-18T04:10:00Z", store=store,
            )
            replacement = []

            def start_worker(_project):
                replacement.append(normalize_event(
                    "claude-code", "user_prompt", {"session_id": "s", "prompt": "second"},
                    repo_root=root, timestamp="2026-07-18T04:12:00Z", store=store,
                ))

            final, status = capture_hook_event(
                "claude-code", "finalize", {"session_id": "s"},
                repo_root=root, store=store, spool=spool, worker_starter=start_worker,
            )
            current = store.current("claude-code", "s")
            self.assertEqual(status, CaptureStatus("captured", "queued"))
            self.assertEqual(final.run_id, first.run_id)
            self.assertEqual(current.run_id, replacement[0].run_id)
            self.assertFalse(current.finalizing)

    def test_episodic_entry_carries_behavioral_event_and_run_ids(self):
        import hooks.post_execution as post

        with tempfile.TemporaryDirectory() as tmp:
            old = post.EPISODIC
            post.EPISODIC = str(Path(tmp) / "events.jsonl")
            try:
                entry = log_execution(
                    "claude-code", "bash: pytest", "ok", True,
                    orchestration_event_id="evt_" + "a" * 64,
                    orchestration_run_id="run-123",
                    orchestration_capture_status="captured:recorded",
                )
            finally:
                post.EPISODIC = old
        self.assertEqual(entry["orchestration_event_id"], "evt_" + "a" * 64)
        self.assertEqual(entry["orchestration_run_id"], "run-123")
        self.assertEqual(entry["orchestration_capture_status"], "captured:recorded")

    def test_cli_keeps_stdout_empty_for_stop_hooks(self):
        script = HOOKS / "orchestration_event.py"
        payload = json.dumps({"session_id": "missing-session"})
        run = subprocess.run(
            [sys.executable, str(script), "--harness", "claude-code", "--signal", "finalize", "--no-deliver"],
            input=payload, text=True, capture_output=True, cwd=ROOT,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "")

    def test_adapter_configs_keep_finalization_inside_three_seconds(self):
        claude = json.loads((ROOT / "adapters/claude-code/settings.json").read_text())
        gemini = json.loads((ROOT / "adapters/gemini/settings.json").read_text())
        copilot = json.loads((ROOT / "adapters/copilot-cli/hooks.json").read_text())
        self.assertEqual(claude["hooks"]["Stop"][0]["hooks"][0]["timeout"], 3)
        self.assertEqual(gemini["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 3000)
        self.assertEqual(copilot["hooks"]["sessionEnd"][0]["timeoutSec"], 3)
        for document in (claude, gemini, copilot):
            self.assertNotIn("feedback", json.dumps(document).lower())

    def test_capability_document_names_every_adapter(self):
        document = (ROOT / "docs/harness-event-capabilities.md").read_text().lower()
        for adapter in CAPABILITIES:
            self.assertIn(adapter.replace("-", " "), document)


if __name__ == "__main__":
    unittest.main()
