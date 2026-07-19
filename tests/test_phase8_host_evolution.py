import json
import os
import stat
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.host_evolution import (  # noqa: E402
    ClaudeOpusAdapter,
    ClaudeOpusNativeAdapter,
    CodexGPTAdapter,
    DailyQuotaStore,
    HostEvolutionError,
    MemosOpusHostHandler,
    NativeCompletion,
    audit_metadata,
    build_host_environment,
    load_pilot_config,
    quota_category,
    run_bounded_command,
    sanitize_native_memos_request,
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
    def setUp(self):
        self.native_fixtures = json.loads(
            (ROOT / "tests/fixtures/memos_2_0_10_host_requests.json").read_text()
        )

    def native_request(self, family: str = "l2", user: str | None = None):
        fixture = self.native_fixtures[family]
        return {
            "messages": [
                {"role": "system", "content": fixture["system"]},
                {"role": "user", "content": fixture["user"] if user is None else user},
            ],
            "model": "opus", "temperature": 0.1,
            "maxTokens": 1024, "timeoutMs": 30000,
        }

    def test_config_is_exact_owner_only_regular_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            path = Path(tmp) / "pilot.json"
            path.write_text(json.dumps({
                "schema": "agentic.memory.evolution-pilot.v2",
                "enabled": True, "project_id": PROJECT,
                "repo_root": str(root.resolve()), "provider": "claude_opus",
                "model": "opus", "timeout_seconds": 30,
                "daily_caps": {"policy": 5, "world_model": 2, "skill": 2, "other": 50},
                "min_distinct_episodes": 3,
            }))
            os.chmod(path, 0o600)
            config = load_pilot_config(path, expected_project_id=PROJECT, expected_repo_root=root)
            self.assertEqual(config.provider, "claude_opus")
            self.assertEqual(config.model, "opus")
            os.chmod(path, 0o640)
            with self.assertRaisesRegex(HostEvolutionError, "pilot_config_invalid"):
                load_pilot_config(path, expected_project_id=PROJECT, expected_repo_root=root)

    def test_config_rejects_symlink_and_secret_slots(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            real = Path(tmp) / "real.json"
            body = {
                "schema": "agentic.memory.evolution-pilot.v2", "enabled": True,
                "project_id": PROJECT, "repo_root": str(root.resolve()),
                "provider": "claude_opus", "model": "opus", "timeout_seconds": 30,
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
            "USER": "pilot", "LOGNAME": "pilot", "SHELL": "/bin/zsh",
            "OPENAI_API_KEY": "secret", "ANTHROPIC_BASE_URL": "bad",
            "GH_TOKEN": "bad", "AWS_SECRET_ACCESS_KEY": "bad", "SSH_AUTH_SOCK": "bad",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-fixture",
            "HOME": "/private/home",
        }, home="/empty/home")
        self.assertEqual(env, {
            "PATH": "/bin", "LANG": "en_US.UTF-8", "TERM": "xterm",
            "USER": "pilot", "LOGNAME": "pilot", "SHELL": "/bin/zsh",
            "HOME": "/empty/home",
        })
        authenticated = build_host_environment(
            {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-fixture", "ANTHROPIC_API_KEY": "blocked"},
            home="/empty/home", include_claude_oauth=True,
        )
        self.assertEqual(authenticated, {
            "HOME": "/empty/home", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-fixture",
        })

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

    def test_failed_quota_reservation_is_retryable_only_on_a_later_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DailyQuotaStore(Path(tmp) / "quota.sqlite3", {"policy": 1})
            self.assertIsNone(store.reserve_or_get("policy", "digest-1", day="2026-07-18"))
            store.fail("digest-1")
            with self.assertRaisesRegex(HostEvolutionError, "quota_request_failed"):
                store.reserve_or_get("policy", "digest-1", day="2026-07-18")
            self.assertIsNone(store.reserve_or_get("policy", "digest-1", day="2026-07-19"))
        with tempfile.TemporaryDirectory() as tmp:
            store = DailyQuotaStore(Path(tmp) / "quota.sqlite3", {"policy": 1})
            self.assertIsNone(store.reserve_or_get("policy", "old", day="2026-07-18"))
            store.fail("old")
            self.assertIsNone(store.reserve_or_get("policy", "today", day="2026-07-19"))
            store.complete("today", {"text": "bounded"})
            with self.assertRaisesRegex(HostEvolutionError, "quota_exhausted"):
                store.reserve_or_get("policy", "old", day="2026-07-19")

    def test_legacy_quota_migration_rolls_back_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quota.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("""
                    CREATE TABLE requests (
                        digest TEXT PRIMARY KEY, category TEXT NOT NULL, day TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('reserved','complete')),
                        response TEXT
                    )
                """)
            with patch.object(
                DailyQuotaStore, "_create_schema", side_effect=RuntimeError("injected"),
            ), self.assertRaisesRegex(RuntimeError, "injected"):
                DailyQuotaStore(path, {"policy": 1})
            with sqlite3.connect(path) as db:
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertIn("requests", tables)
            self.assertNotIn("requests_legacy", tables)

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

    def test_native_memos_request_is_pinned_bounded_and_sanitized(self):
        for family, expected in (
            ("l2", "l2.induction"),
            ("l3", "l3.abstraction"),
            ("skill", "skill.crystallize"),
        ):
            with self.subTest(family=family):
                operation, messages, redactions = sanitize_native_memos_request(
                    self.native_request(family), expected_model="opus",
                )
                self.assertEqual(operation, expected)
                sanitized = messages[-1]["content"]
                for canary in (
                    "RAW_USER", "RAW_AGENT", "RAW_STDOUT", "RAW_REFLECTION",
                    "RAW_POLICY", "RAW_SOURCE", "ordinary-value", "/Users/",
                    "PRIVATE_NAME", "RAW_PREF", "RAW_AVOID",
                ):
                    self.assertNotIn(canary, sanitized)
                body = json.loads(sanitized)
                if family == "l2":
                    self.assertIn("authentication", body["traces"][0]["state_classes"])
                    self.assertIn("source_code", body["traces"][0]["action_classes"])
                    self.assertEqual(body["traces"][0]["outcome_class"], "success")
                elif family == "l3":
                    self.assertIn("execute", body["policies"][0]["procedure_classes"])
                else:
                    self.assertIn("execute", body["policy"]["procedure_classes"])
                    self.assertEqual(body["evidence"][0]["tags"], ["python", "test"])
                self.assertGreaterEqual(redactions, 1)

    def test_only_exact_artifact_system_shape_is_accepted(self):
        request = self.native_request()
        for mutation in (
            lambda value: "X" + value[1:],
            lambda value: value.replace("Expected shape:", "Expected schema:", 1),
            lambda value: value + " ",
        ):
            bad = self.native_request()
            bad["messages"][0]["content"] = mutation(request["messages"][0]["content"])
            with self.subTest(), self.assertRaises(HostEvolutionError):
                sanitize_native_memos_request(bad, expected_model="opus")

    def test_native_request_rejects_bad_roles_models_limits_and_system_order(self):
        mutations = []
        request = self.native_request(); request["model"] = "gpt"
        mutations.append(request)
        request = self.native_request(); request["maxTokens"] = True
        mutations.append(request)
        request = self.native_request(); request["messages"][1]["role"] = "assistant"
        mutations.append(request)
        request = self.native_request(); request["messages"].append(
            {"role": "user", "content": "retry carrying prior output"},
        )
        mutations.append(request)
        request = self.native_request(); request["messages"] = request["messages"][:1]
        mutations.append(request)
        for bad in mutations:
            with self.subTest(), self.assertRaises(HostEvolutionError):
                sanitize_native_memos_request(bad, expected_model="opus")

    def test_skill_translation_preserves_only_allowlisted_metadata(self):
        _, messages, redactions = sanitize_native_memos_request(
            self.native_request("skill"), expected_model="opus",
        )
        body = json.loads(messages[-1]["content"])
        self.assertEqual(body["evidence_tool_classes"], ["shell"])
        self.assertEqual(body["policy"]["policy_id"], "po_001")
        self.assertEqual(
            set(body), {"schema", "policy", "evidence", "evidence_tool_classes"},
        )
        self.assertGreaterEqual(redactions, 1)

    def test_native_opus_adapter_uses_no_tools_and_validates_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "capture.json"
            fake = Path(tmp) / "claude"
            response_text = json.dumps({"title": "Use bounded retry"})
            fake.write_text(textwrap.dedent(f"""\
                #!{sys.executable}
                import json, pathlib, sys
                pathlib.Path({str(capture)!r}).write_text(json.dumps({{"argv":sys.argv[1:],"stdin":sys.stdin.read()}}))
                print(json.dumps({{"result":{response_text!r},"subtype":"success","is_error":False,
                    "terminal_reason":"completed","permission_denials":[],"num_turns":1,
                    "duration_api_ms":12,"usage":{{"input_tokens":4,"output_tokens":5}}}}))
            """)); fake.chmod(0o700)
            adapter = ClaudeOpusNativeAdapter(executable=str(fake), model="opus", cwd=tmp)
            completion = adapter.complete_messages([{"role": "user", "content": "bounded"}])
            self.assertEqual(completion.text, response_text)
            self.assertEqual(completion.usage["totalTokens"], 9)
            captured = json.loads(capture.read_text())
            self.assertEqual(captured["argv"][captured["argv"].index("--tools") + 1], "")
            self.assertNotIn("bounded", " ".join(captured["argv"]))
            self.assertIn("bounded", captured["stdin"])

    def test_native_handler_is_quota_cached_and_audits_metadata_only(self):
        class FakeAdapter:
            def __init__(self): self.calls = 0
            def complete_messages(self, _messages):
                self.calls += 1
                return NativeCompletion(
                    text='{"title":"bounded"}', model="opus",
                    usage={"promptTokens": 1, "completionTokens": 2, "totalTokens": 3},
                    duration_ms=7,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            handler = MemosOpusHostHandler(
                adapter=adapter,
                quota=DailyQuotaStore(root / "quota.sqlite3", {"policy": 1, "other": 1}),
                audit_file=root / "audit.jsonl", expected_model="opus",
                project_id=PROJECT, repository_revision=REVISION,
            )
            first = handler(self.native_request())
            second = handler(self.native_request())
            self.assertEqual(first, second)
            self.assertEqual(adapter.calls, 1)
            audit = (root / "audit.jsonl").read_text()
            self.assertNotIn("PATTERN_SIGNATURE", audit)
            self.assertIn('"outcome":"cached"', audit)
            another = self.native_request()
            another["messages"][1]["content"] = another["messages"][1]["content"].replace(
                "tr_001", "tr_002",
            )
            with self.assertRaisesRegex(HostEvolutionError, "quota_exhausted"):
                handler(another)
            rows = [json.loads(line) for line in audit.splitlines()]
            self.assertNotIn("failure_class", rows[0])
            self.assertEqual(rows[0]["project_id"], PROJECT)
            self.assertEqual(rows[0]["repository_revision"], REVISION)
            self.assertGreater(rows[0]["input_bytes"], 0)
            self.assertGreater(rows[0]["output_bytes"], 0)
            self.assertEqual(rows[0]["prompt_tokens"], 1)

    def test_native_cache_is_bound_to_repository_revision(self):
        class FakeAdapter:
            def __init__(self): self.calls = 0
            def complete_messages(self, _messages):
                self.calls += 1
                return NativeCompletion(
                    text='{"title":"bounded"}', model="opus",
                    usage={"promptTokens": 1, "completionTokens": 1, "totalTokens": 2},
                    duration_ms=1,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); adapter = FakeAdapter()
            quota = DailyQuotaStore(root / "quota.sqlite3", {"policy": 2})
            for revision in ("a" * 40, "b" * 40):
                MemosOpusHostHandler(
                    adapter=adapter, quota=quota, audit_file=root / "audit.jsonl",
                    expected_model="opus", project_id=PROJECT,
                    repository_revision=revision,
                )(self.native_request())
            self.assertEqual(adapter.calls, 2)

    def test_native_handler_audits_validation_quota_and_adapter_failures(self):
        class FailingAdapter:
            def __init__(self, code): self.code = code
            def complete_messages(self, _messages):
                raise HostEvolutionError(self.code)

        for code in ("claude_process_failed", "command_timeout"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                handler = MemosOpusHostHandler(
                    adapter=FailingAdapter(code),
                    quota=DailyQuotaStore(root / "quota.sqlite3", {"policy": 1}),
                    audit_file=root / "audit.jsonl", expected_model="opus",
                    project_id=PROJECT, repository_revision=REVISION,
                )
                with self.assertRaisesRegex(HostEvolutionError, code):
                    handler(self.native_request())
                row = json.loads((root / "audit.jsonl").read_text().splitlines()[-1])
                self.assertEqual(row["outcome"], "failed")
                self.assertEqual(row["failure_class"], code)
                self.assertNotIn("messages", row)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handler = MemosOpusHostHandler(
                adapter=FailingAdapter("unreachable"),
                quota=DailyQuotaStore(root / "quota.sqlite3", {"policy": 0}),
                audit_file=root / "audit.jsonl", expected_model="opus",
                project_id=PROJECT, repository_revision=REVISION,
            )
            bad = self.native_request()
            bad["messages"][0]["content"] = "unreviewed system"
            with self.assertRaises(HostEvolutionError):
                handler(bad)
            with self.assertRaisesRegex(HostEvolutionError, "quota_exhausted"):
                handler(self.native_request())
            audit = (root / "audit.jsonl").read_text()
            self.assertNotIn("ordinary-value", audit)
            rows = [json.loads(line) for line in audit.splitlines()]
            self.assertEqual(
                [row["failure_class"] for row in rows],
                ["native_request_unknown_prompt", "quota_exhausted"],
            )


if __name__ == "__main__":
    unittest.main()
