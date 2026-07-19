import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.host_evolution import (  # noqa: E402
    ClaudeOpusAdapter,
    CodexGPTAdapter,
    DailyQuotaStore,
    HostEvolutionError,
    audit_metadata,
    build_host_environment,
    load_pilot_config,
    quota_category,
    run_bounded_command,
    validate_sanitized_dto,
)


PROJECT = "0123456789abcdef"
REVISION = "a" * 40


def dto(**changes):
    value = {
        "schema": "agentic.memory.host-dto.v1",
        "project_id": PROJECT,
        "repository_revision": REVISION,
        "operation": "candidate.review",
        "summaries": ["Retry only after a verified transient failure."],
        "evidence_ids": ["evi_" + "b" * 64],
        "digests": ["sha256:" + "c" * 64],
        "outcome_class": "success",
    }
    value.update(changes)
    return value


class HostEvolutionTest(unittest.TestCase):
    def test_config_is_exact_owner_only_regular_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            path = Path(tmp) / "pilot.json"
            path.write_text(json.dumps({
                "schema": "agentic.memory.evolution-pilot.v1",
                "enabled": True, "project_id": PROJECT,
                "repo_root": str(root.resolve()), "gpt_model": "gpt-5.4",
                "opus_model": "opus", "timeout_seconds": 30,
                "daily_caps": {"policy": 5, "world_model": 2, "skill": 2, "other": 50},
                "min_distinct_episodes": 3,
            }))
            os.chmod(path, 0o600)
            config = load_pilot_config(path, expected_project_id=PROJECT, expected_repo_root=root)
            self.assertEqual(config.gpt_model, "gpt-5.4")
            os.chmod(path, 0o640)
            with self.assertRaisesRegex(HostEvolutionError, "pilot_config_invalid"):
                load_pilot_config(path, expected_project_id=PROJECT, expected_repo_root=root)

    def test_config_rejects_symlink_and_secret_slots(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            real = Path(tmp) / "real.json"
            body = {
                "schema": "agentic.memory.evolution-pilot.v1", "enabled": True,
                "project_id": PROJECT, "repo_root": str(root.resolve()),
                "gpt_model": "gpt", "opus_model": "opus", "timeout_seconds": 30,
                "daily_caps": {"policy": 1, "world_model": 1, "skill": 1, "other": 1},
                "min_distinct_episodes": 3,
                "api_key": "nope",
            }
            real.write_text(json.dumps(body)); os.chmod(real, 0o600)
            link = Path(tmp) / "link.json"; link.symlink_to(real)
            with self.assertRaisesRegex(HostEvolutionError, "pilot_config_invalid"):
                load_pilot_config(link, expected_project_id=PROJECT, expected_repo_root=root)
            with self.assertRaisesRegex(HostEvolutionError, "pilot_config_invalid"):
                load_pilot_config(real, expected_project_id=PROJECT, expected_repo_root=root)

    def test_dto_is_strict_bounded_and_rejects_sensitive_content(self):
        self.assertEqual(validate_sanitized_dto(dto())["project_id"], PROJECT)
        bad = [
            dto(raw_prompt="secret"),
            dto(summaries=["OPENAI_API_KEY=sk-" + "x" * 30]),
            dto(summaries=["AWS_SECRET_ACCESS_KEY=ordinary-looking-value"]),
            dto(summaries=["STRIPE_SECRET_KEY=ordinary-looking-value"]),
            dto(summaries=["read /Users/alice/private.txt"]),
            dto(summaries=["-----BEGIN PRIVATE KEY-----"]),
            dto(summaries=["x" * 2001]),
        ]
        for value in bad:
            with self.subTest(value=list(value)), self.assertRaises(HostEvolutionError):
                validate_sanitized_dto(value)

    def test_audit_contains_only_metadata(self):
        record = audit_metadata(dto(), provider="claude", model="opus", duration_ms=12,
                                outcome="ok", redaction_count=3)
        encoded = json.dumps(record)
        self.assertNotIn("Retry only", encoded)
        self.assertNotIn("summaries", record)
        clean = validate_sanitized_dto(dto())
        self.assertEqual(record["input_bytes"], len(json.dumps(clean, separators=(",", ":")).encode()))
        self.assertEqual(record["redaction_count"], 3)

    def test_environment_is_allowlisted(self):
        env = build_host_environment({
            "PATH": "/bin", "LANG": "en_US.UTF-8", "TERM": "xterm",
            "OPENAI_API_KEY": "secret", "ANTHROPIC_BASE_URL": "bad",
            "GH_TOKEN": "bad", "AWS_SECRET_ACCESS_KEY": "bad", "SSH_AUTH_SOCK": "bad",
            "HOME": "/private/home",
        }, home="/empty/home")
        self.assertEqual(env, {"PATH": "/bin", "LANG": "en_US.UTF-8", "TERM": "xterm",
                               "HOME": "/empty/home"})

    def test_quota_is_owner_only_transactional_and_cache_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DailyQuotaStore(Path(tmp) / "quota.sqlite3", {"policy": 1})
            self.assertIsNone(store.reserve_or_get("policy", "digest-1", day="2026-07-18"))
            with self.assertRaisesRegex(HostEvolutionError, "quota_request_in_progress"):
                store.reserve_or_get("policy", "digest-1", day="2026-07-18")
            store.complete("digest-1", {"text": "bounded"})
            self.assertEqual(store.reserve_or_get("policy", "digest-1", day="2026-07-18"),
                             {"text": "bounded"})
            with self.assertRaisesRegex(HostEvolutionError, "quota_exhausted"):
                store.reserve_or_get("policy", "digest-2", day="2026-07-18")
            self.assertEqual(stat.S_IMODE(os.stat(Path(tmp) / "quota.sqlite3").st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(tmp).st_mode), 0o700)

    def test_quota_category_is_exact_and_fail_closed(self):
        self.assertEqual(quota_category("l2.induction"), "policy")
        self.assertEqual(quota_category("l3.abstraction"), "world_model")
        self.assertEqual(quota_category("skill.crystallize"), "skill")
        self.assertEqual(quota_category("reward.score"), "other")
        with self.assertRaisesRegex(HostEvolutionError, "quota_operation_unknown"):
            quota_category("L2 induction")

    def test_runner_uses_stdin_timeout_process_group_and_output_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake.py"
            script.write_text("import sys; print(sys.stdin.read())")
            result = run_bounded_command((sys.executable, str(script)), stdin=b"payload",
                                         cwd=tmp, env={"PATH": os.environ.get("PATH", "")},
                                         timeout_seconds=2, max_output_bytes=100)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), b"payload")
            self.assertNotIn("payload", result.argv)
            script.write_text("print('x'*200)")
            with self.assertRaisesRegex(HostEvolutionError, "output_too_large"):
                run_bounded_command((sys.executable, str(script)), stdin=b"", cwd=tmp, env={},
                                    timeout_seconds=2, max_output_bytes=20)
            script.write_text("import time; time.sleep(10)")
            with self.assertRaisesRegex(HostEvolutionError, "timeout"):
                run_bounded_command((sys.executable, str(script)), stdin=b"", cwd=tmp, env={},
                                    timeout_seconds=.05, max_output_bytes=20)
            with self.assertRaisesRegex(HostEvolutionError, "invalid_limits"):
                run_bounded_command((sys.executable, str(script)), stdin=b"", cwd=tmp, env={},
                                    timeout_seconds=float("nan"), max_output_bytes=20)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process groups")
    def test_runner_kills_forked_term_ignoring_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "escaped"
            script = Path(tmp) / "fork.py"
            script.write_text(textwrap.dedent(f"""\
                import os, signal, time
                if os.fork() == 0:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    time.sleep(.4)
                    open({str(marker)!r}, 'w').write('escaped')
                    os._exit(0)
                os._exit(0)
            """))
            result = run_bounded_command((sys.executable, str(script)), stdin=b"", cwd=tmp,
                                         env={}, timeout_seconds=2, max_output_bytes=20)
            self.assertEqual(result.returncode, 0)
            import time
            time.sleep(.45)
            self.assertFalse(marker.exists())

    def test_claude_adapter_has_safe_exact_shape_and_requires_execution_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "capture.json"
            fake = Path(tmp) / "claude"
            fake.write_text(textwrap.dedent(f"""\
                #!{sys.executable}
                import json, pathlib, sys
                pathlib.Path({str(capture)!r}).write_text(json.dumps({{"argv":sys.argv[1:],"stdin":sys.stdin.read()}}))
                print(json.dumps({{"structured_output":{{"decision":"approve","rationale":"bounded"}},
                    "subtype":"success","is_error":False,"terminal_reason":"completed",
                    "permission_denials":[],"num_turns":1,"duration_api_ms":12}}))
            """)); fake.chmod(0o700)
            adapter = ClaudeOpusAdapter(executable=str(fake), model="opus", cwd=tmp)
            result = adapter.complete(dto())
            self.assertEqual(result["decision"], "approve")
            captured = json.loads(capture.read_text())
            self.assertNotIn("Retry only", " ".join(captured["argv"]))
            self.assertIn("Retry only", captured["stdin"])
            for flag in ("--safe-mode", "--disable-slash-commands", "--no-session-persistence",
                         "--strict-mcp-config", "--output-format", "--json-schema"):
                self.assertIn(flag, captured["argv"])
            self.assertEqual(captured["argv"][captured["argv"].index("--tools") + 1], "")
            self.assertEqual(captured["argv"][captured["argv"].index("--mcp-config") + 1],
                             '{"mcpServers":{}}')

    def test_claude_exit_zero_without_turns_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text(f"#!{sys.executable}\nimport json; print(json.dumps({{'structured_output':{{'decision':'approve','rationale':'x'}},'subtype':'success','is_error':False,'terminal_reason':'completed','permission_denials':[],'num_turns':0,'duration_api_ms':0}}))\n")
            fake.chmod(0o700)
            with self.assertRaisesRegex(HostEvolutionError, "claude_not_executed"):
                ClaudeOpusAdapter(executable=str(fake), cwd=tmp).complete(dto())

    def test_claude_invalid_mcp_style_exit_zero_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            envelope = {
                "structured_output": {"decision": "approve", "rationale": "x"},
                "subtype": "error", "is_error": True, "terminal_reason": "invalid_config",
                "permission_denials": ["invalid MCP configuration"],
                "num_turns": 1, "duration_api_ms": 10,
            }
            fake.write_text(f"#!{sys.executable}\nimport json; print(json.dumps({envelope!r}))\n")
            fake.chmod(0o700)
            with self.assertRaisesRegex(HostEvolutionError, "claude_not_executed"):
                ClaudeOpusAdapter(executable=str(fake), cwd=tmp).complete(dto())

    def test_codex_fails_closed_because_no_preventive_no_tools_mode_exists(self):
        with self.assertRaisesRegex(HostEvolutionError, "codex_no_tools_unavailable"):
            CodexGPTAdapter().complete(dto())


if __name__ == "__main__":
    unittest.main()
