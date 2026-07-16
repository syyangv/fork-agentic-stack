import json
import hashlib
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / ".agent" / "memory"
sys.path.insert(0, str(MEMORY))

from orchestration.config import ConfigError, MemoryOrchestrationConfig, load_config
from orchestration.contracts import (
    ContractError,
    ContextPacket,
    EventEnvelope,
    IdempotencyRegistry,
    ProvenanceRef,
    RetrievalItem,
)
from orchestration.identity import ProjectIdentityResolver, derive_project_identity
from orchestration.router import LaneRequirement, allocate_lane_budgets, route_intent
from orchestration._core import canonical_json
from harness_manager.upgrade import upgrade


class EventEnvelopeTest(unittest.TestCase):
    def base_event(self, payload, **overrides):
        values = dict(
            idempotency_key="codex:run-1:tool-2",
            timestamp="2026-07-16T20:00:00Z",
            event_type="tool.completed",
            project_id="0123456789abcdef",
            repo_root="/repo",
            revision="a" * 40,
            harness="codex",
            run_id="run-1",
            session_id="session-1",
            actor="tool",
            intent="Inspect build output",
            payload=payload,
        )
        values.update(overrides)
        return EventEnvelope.create(**values)

    def test_stable_id_ignores_dictionary_key_order(self):
        first = self.base_event({"nested": {"b": 2, "a": 1}, "ok": True})
        second = self.base_event({"ok": True, "nested": {"a": 1, "b": 2}})
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.canonical_json(), second.canonical_json())

    def test_duplicate_idempotency_key_is_suppressed_in_memory(self):
        registry = IdempotencyRegistry()
        event = self.base_event({"ok": True})
        self.assertTrue(registry.accept(event))
        self.assertFalse(registry.accept(event))
        self.assertEqual(len(registry), 1)

    def test_secrets_and_credential_paths_are_redacted_before_construction(self):
        event = self.base_event(
            {
                "api_key": "sk-abcdefghijklmnopqrstuvwxyz123456",
                "command": "cat ~/.aws/credentials",
                "authorization": "Bearer top-secret-value",
            }
        )
        rendered = event.canonical_json()
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", rendered)
        self.assertNotIn(".aws/credentials", rendered)
        self.assertNotIn("top-secret-value", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertEqual(event.privacy, "sensitive-redacted")

    def test_forbidden_prompt_environment_and_nested_credentials_are_redacted(self):
        event = self.base_event(
            {
                "steps": [
                    {"client_secret": "plain-client-secret"},
                    {"refresh_token": "plain-refresh-token"},
                    {"raw_environment": {"HOME": "/Users/a", "PATH": "/bin"}},
                    {"full_prompt": "confidential user prompt"},
                    {"path": "~/.ssh/id_rsa"},
                ]
            }
        )
        rendered = event.canonical_json()
        for forbidden in (
            "plain-client-secret",
            "plain-refresh-token",
            '"HOME"',
            "confidential user prompt",
            ".ssh/id_rsa",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(event.privacy, "sensitive-redacted")

        external = self.base_event({"ok": True}).to_dict()
        external["payload"] = {"items": [{"raw_prompt": "do not persist"}]}
        with self.assertRaises(ContractError):
            EventEnvelope.from_external(external)

    def test_provider_prefixed_secrets_and_windows_credential_paths_are_redacted(self):
        payloads = (
            {"token": "plain-token-value"},
            {"github_token": "plain-github-token"},
            {"aws_session_token": "plain-session-token"},
            {"AWS_SECRET_ACCESS_KEY": "plain-aws-secret"},
            {"db_password": "plain-db-password"},
            {"env": {"API_TOKEN": "plain-value", "HOME": "/Users/a"}},
            {"path": r"C:\Users\Alice\.ssh\id_rsa"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                event = self.base_event(payload)
                rendered = event.canonical_json()
                self.assertEqual(event.privacy, "sensitive-redacted")
                self.assertIn("[REDACTED]", rendered)
                self.assertNotIn("plain-", rendered)
                self.assertNotIn(".ssh", rendered)

                external = self.base_event({"ok": True}).to_dict()
                external["payload"] = payload
                with self.assertRaises(ContractError):
                    EventEnvelope.from_external(external)

    def test_camel_case_secrets_and_temporary_aws_keys_are_redacted(self):
        payloads = (
            {"accessToken": "plain-access-token"},
            {"githubToken": "plain-github-token"},
            {"apiKey": "plain-api-key"},
            {"clientSecret": "plain-client-secret"},
            {"privateKey": "plain-private-key"},
            {"fullPrompt": "confidential prompt"},
            {"rawEnvironment": {"HOME": "/Users/a", "PATH": "/bin"}},
            {"AWS_ACCESS_KEY_ID": "ASIAABCDEFGHIJKLMNOP"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                event = self.base_event(payload)
                self.assertEqual(event.privacy, "sensitive-redacted")
                self.assertIn("[REDACTED]", event.canonical_json())

                external = self.base_event({"ok": True}).to_dict()
                external["payload"] = payload
                content = {
                    name: value for name, value in external.items() if name != "event_id"
                }
                external["event_id"] = "evt_" + hashlib.sha256(
                    canonical_json(content).encode("utf-8")
                ).hexdigest()
                with self.assertRaises(ContractError):
                    EventEnvelope.from_external(external)

    def test_non_sensitive_metadata_keys_are_not_false_positives(self):
        payload = {
            "token_estimate": 42,
            "max_tokens": 1200,
            "public_key": "node-identifier",
            "environment_name": "staging",
        }
        event = self.base_event(payload)
        self.assertEqual(event.privacy, "internal")
        self.assertEqual(dict(event.payload), payload)

    def test_direct_plaintext_secret_is_rejected(self):
        data = self.base_event({"ok": True}).to_dict()
        data["payload"] = {"token": "ghp_abcdefghijklmnopqrstuvwxyz1234567890"}
        with self.assertRaises(ContractError):
            EventEnvelope.from_external(data)

    def test_unicode_and_cjk_are_preserved(self):
        event = self.base_event({"message": "修复登录流程 — café"})
        self.assertIn("修复登录流程", event.canonical_json())
        self.assertEqual(event.payload["message"], "修复登录流程 — café")

    def test_oversize_string_and_payload_are_rejected(self):
        with self.assertRaises(ContractError):
            self.base_event({"message": "x" * 2001})
        with self.assertRaises(ContractError):
            self.base_event({f"field-{i}": "x" * 1000 for i in range(20)})

    def test_non_finite_and_non_json_payload_values_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.base_event({"metric": value})
        with self.assertRaises(ContractError):
            self.base_event({"binary": b"not-json"})

    def test_external_timestamp_must_be_utc(self):
        data = self.base_event({"ok": True}).to_dict()
        data["timestamp"] = "2026-07-16T16:00:00-04:00"
        with self.assertRaises(ContractError):
            EventEnvelope.from_external(data)
        for invalid in (
            "2026-99-99Tnot-a-timeZ",
            "2025-02-29T12:00:00Z",
            "2026-07-16T25:00:00Z",
        ):
            with self.subTest(timestamp=invalid), self.assertRaises(ContractError):
                self.base_event({}, timestamp=invalid)
        leap = self.base_event({}, timestamp="2024-02-29T12:00:00+00:00")
        self.assertEqual(leap.timestamp, "2024-02-29T12:00:00Z")

    def test_contract_records_are_immutable(self):
        event = self.base_event({"nested": {"answer": 42}})
        with self.assertRaises(FrozenInstanceError):
            event.intent = "changed"
        with self.assertRaises(TypeError):
            event.payload["new"] = "value"
        with self.assertRaises(TypeError):
            event.payload["nested"]["answer"] = 0


class ContractSchemaTest(unittest.TestCase):
    def test_manifest_declares_phase1_contract_features(self):
        manifest = json.loads((ROOT / ".agent" / "infrastructure.json").read_text())
        self.assertEqual(manifest["orchestration_phase"], 1)
        self.assertTrue(
            {
                "memory_contracts_v1",
                "memory_redaction",
                "stable_project_identity",
                "deterministic_memory_routing",
                "bounded_lane_budgets",
                "strict_orchestration_config",
            }.issubset(manifest["features"])
        )

    def test_four_contracts_round_trip_external_json(self):
        provenance = ProvenanceRef.from_external(
            {
                "kind": "lesson",
                "provider": "agentic-stack",
                "source_id": "lesson-1",
                "project_id": "0123456789abcdef",
                "repository_revision": None,
                "source_hash": "sha256:" + "a" * 64,
                "observed_at": "2026-07-16T20:00:00Z",
                "confidence": 0.8,
                "freshness": "fresh",
                "locator": {"path": "memory/semantic/LESSONS.md"},
            }
        )
        item = RetrievalItem.from_external(
            {
                "item_id": "lesson-1",
                "lane": "governance",
                "type": "lesson",
                "summary": "Never bypass review.",
                "scope": {"project_id": "0123456789abcdef", "harness": None},
                "status": "accepted",
                "provider_score": 0.9,
                "selection_reason": "accepted governance guidance",
                "provenance": [provenance.to_dict()],
                "token_estimate": 8,
                "expires_at": None,
            }
        )
        packet = ContextPacket.from_external(
            {
                "schema": "agentic.memory.context.v1",
                "intent": "review code",
                "project_id": "0123456789abcdef",
                "routing": {"governance": True, "behavioral": True, "evidence": True},
                "sections": [
                    {"lane": "governance", "items": [item.to_dict()]},
                    {"lane": "behavioral", "items": []},
                    {"lane": "evidence", "items": []},
                ],
                "warnings": [],
                "health": {"governance": "healthy"},
                "token_estimate": 8,
            }
        )
        self.assertEqual(packet.sections[0]["items"][0]["item_id"], "lesson-1")

    def test_unknown_contract_fields_are_rejected(self):
        event = EventEnvelopeTest().base_event({"ok": True}).to_dict()
        event["surprise"] = True
        with self.assertRaises(ContractError):
            EventEnvelope.from_external(event)


class ProjectIdentityTest(unittest.TestCase):
    def test_equivalent_github_remotes_share_an_identity(self):
        ssh = derive_project_identity("/ignored", "git@github.com:MemTensor/MemOS.git")
        https = derive_project_identity("/other", "https://github.com/MemTensor/MemOS")
        self.assertEqual(ssh.project_id, https.project_id)
        self.assertEqual(ssh.canonical_source, "github.com/memtensor/memos")
        lower = derive_project_identity("/third", "https://github.com/memtensor/memos.git")
        self.assertEqual(ssh.project_id, lower.project_id)

    def test_windows_paths_are_canonicalized_without_host_resolution(self):
        identity = derive_project_identity(r"C:\Users\Alice\Repo")
        self.assertEqual(identity.canonical_source, "c:/users/alice/repo")
        self.assertEqual(len(identity.project_id), 16)
        alternate = derive_project_identity(r"c:\users\alice\repo")
        self.assertEqual(identity.project_id, alternate.project_id)

    def test_alias_resolution_is_explicit(self):
        identity = derive_project_identity("/repo", "https://example.com/acme/repo.git")
        resolver = ProjectIdentityResolver({"legacy-project": identity.project_id})
        self.assertEqual(resolver.resolve("legacy-project"), identity.project_id)
        self.assertEqual(resolver.resolve(identity.project_id), identity.project_id)
        with self.assertRaises(KeyError):
            resolver.resolve("unknown")
        with self.assertRaises(ValueError):
            ProjectIdentityResolver({"broken": "not-a-project-id"})


class RoutingAndBudgetTest(unittest.TestCase):
    def test_routing_table(self):
        cases = [
            ("check permission and prior decision", "required", "default", "off"),
            ("debug repeated test failure in repository", "required", "required", "required"),
            ("find callers and refactor this symbol", "required", "default", "required"),
            ("draft non-code documentation", "required", "default", "off"),
            ("security review the authentication handler", "required", "required", "required"),
        ]
        for intent, governance, behavioral, evidence in cases:
            with self.subTest(intent=intent):
                route = route_intent(intent)
                self.assertEqual(route.governance.value, governance)
                self.assertEqual(route.behavioral.value, behavioral)
                self.assertEqual(route.evidence.value, evidence)

    def test_budgets_never_exceed_total_and_off_lanes_get_zero(self):
        routes = [
            route_intent("check a preference"),
            route_intent("find callers and debug a failure"),
            route_intent("write documentation"),
        ]
        for route in routes:
            with self.subTest(route=route):
                budget = allocate_lane_budgets(route)
                self.assertLessEqual(sum(budget.values()), 12_000)
                if route.evidence is LaneRequirement.OFF:
                    self.assertEqual(budget["evidence"], 0)
        with self.assertRaises(ValueError):
            allocate_lane_budgets(route_intent("review code"), total=12_001)


class ConfigurationTest(unittest.TestCase):
    def test_missing_config_defaults_to_off_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = load_config(path)
            self.assertEqual(config.mode, "off")
            self.assertFalse(path.exists())

    def test_unknown_config_fields_and_invalid_budget_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"mode": "off", "unknown": True}))
            with self.assertRaises(ConfigError):
                load_config(path)
            path.write_text(json.dumps({"mode": "off", "total_token_budget": 100}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_default_config_is_immutable_and_bounded(self):
        config = MemoryOrchestrationConfig()
        self.assertEqual(config.schema, "agentic.memory.config.v1")
        self.assertEqual(config.mode, "off")
        self.assertEqual(sum(config.lane_reserves.values()), config.total_token_budget)
        with self.assertRaises(FrozenInstanceError):
            config.mode = "assist"
        with self.assertRaises(TypeError):
            config.lane_reserves["governance"] = 1
        with self.assertRaises(ConfigError):
            MemoryOrchestrationConfig(mode="enforce")

    def test_upgrade_installs_code_and_schemas_but_preserves_local_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            agent = project / ".agent"
            agent.mkdir()
            local_config = agent / "memory" / "orchestration" / "config.json"
            local_config.parent.mkdir(parents=True)
            custom = {
                "schema": "agentic.memory.config.v1",
                "mode": "shadow",
                "total_token_budget": 12000,
                "lane_reserves": {
                    "governance": 5000,
                    "behavioral": 4000,
                    "evidence": 3000,
                },
                "project_aliases": {},
            }
            local_config.write_text(json.dumps(custom), encoding="utf-8")

            self.assertEqual(upgrade(project, ROOT, yes=True, log=lambda _msg: None), 0)

            self.assertEqual(json.loads(local_config.read_text(encoding="utf-8")), custom)
            self.assertTrue((agent / "memory" / "orchestration" / "contracts.py").is_file())
            self.assertTrue(
                (
                    agent
                    / "protocols"
                    / "tool_schemas"
                    / "memory"
                    / "event-envelope-v1.schema.json"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()
