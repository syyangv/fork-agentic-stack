import importlib.util
import json
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / ".agent"
sys.path.insert(0, str(AGENT / "memory"))
sys.path.insert(0, str(AGENT / "harness"))
from orchestration.orchestrator import build_governance_packet
from orchestration.providers.governance import GovernanceProvider
from text import word_set


def load_recall():
    spec = importlib.util.spec_from_file_location("phase2_recall", AGENT / "tools" / "recall.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GovernanceOrchestratorTest(unittest.TestCase):
    def test_manifest_declares_phase2_features(self):
        manifest = json.loads((AGENT / "infrastructure.json").read_text())
        self.assertEqual(manifest["orchestration_phase"], 2)
        self.assertTrue(
            {"governance_provider", "governance_orchestrator_cli", "legacy_recall_comparison"}
            .issubset(manifest["features"])
        )

    def fixture(self, root: Path) -> Path:
        agent = root / ".agent"
        (agent / "memory/semantic").mkdir(parents=True)
        (agent / "memory/personal").mkdir(parents=True)
        (agent / "memory/working").mkdir(parents=True)
        (agent / "protocols").mkdir(parents=True)
        rows = [
            {"id": "keep", "claim": "Retry deployment timeouts safely", "conditions": ["deploy", "timeout"], "status": "accepted"},
            {"id": "retracted", "claim": "Use the old deploy flag", "conditions": ["deploy"], "status": "accepted"},
            {"id": "provisional", "claim": "Unverified deploy shortcut", "conditions": ["deploy"], "status": "provisional"},
            {"id": "superseded", "claim": "Superseded deploy process", "conditions": ["deploy"], "status": "superseded"},
            {"id": "retracted", "claim": "Use the old deploy flag", "conditions": ["deploy"], "status": "retracted"},
        ]
        (agent / "memory/semantic/lessons.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        (agent / "memory/semantic/LESSONS.md").write_text(
            "# Lessons\n- Seed deploy safety\n- [PROVISIONAL] Maybe skip tests\n- ~~Old seed~~\n",
            encoding="utf-8",
        )
        (agent / "memory/personal/PREFERENCES.md").write_text("- Prefer local-first tools.\n")
        (agent / "memory/semantic/DECISIONS.md").write_text(
            "# Decisions\n## 2026-01-01: Review authority\n"
            "**Decision:** Human review is authoritative.\n"
            "**Rationale:** Derived memory is not doctrine.\n**Status:** active\n"
        )
        (agent / "memory/working/REVIEW_QUEUE.md").write_text("No pending candidates.\n")
        (agent / "protocols/permissions.md").write_text("# Permissions\nNever expose secrets.\n")
        return agent

    def provider(self, agent: Path):
        return GovernanceProvider(agent, "0123456789abcdef", word_set)

    def test_latest_state_and_nonaccepted_lessons_are_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.fixture(Path(tmp))
            items, health = self.provider(agent).retrieve("deploy timeout", top_k=10)
            claims = [item.summary for item in items if item.type == "lesson"]
            self.assertIn("Retry deployment timeouts safely", claims)
            self.assertIn("Seed deploy safety", claims)
            self.assertNotIn("Use the old deploy flag", claims)
            self.assertNotIn("Unverified deploy shortcut", claims)
            self.assertNotIn("Superseded deploy process", claims)
            self.assertEqual(health["status"], "healthy")

    def test_stale_markdown_cannot_resurrect_nonaccepted_structured_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.fixture(Path(tmp))
            claims = {
                "retracted": "Use obsolete deploy flag",
                "provisional": "Try unverified deploy flag",
                "superseded": "Use superseded deploy flag",
            }
            rows = []
            for status, claim in claims.items():
                rows.extend([
                    {"id": status, "claim": claim, "conditions": ["deploy"], "status": "accepted"},
                    {"id": status, "claim": claim, "conditions": ["deploy"], "status": status},
                ])
            (agent / "memory/semantic/lessons.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            (agent / "memory/semantic/LESSONS.md").write_text(
                "# Lessons\n" + "".join(f"- {claim}\n" for claim in claims.values())
            )
            items, _ = self.provider(agent).retrieve("deploy", top_k=10)
            returned = {item.summary for item in items if item.type == "lesson"}
            self.assertTrue(returned.isdisjoint(claims.values()))

    def test_permissions_are_always_present_and_missing_is_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.fixture(Path(tmp))
            packet = build_governance_packet(self.provider(agent), "unrelated words")
            items = packet.sections[0]["items"]
            self.assertTrue(any(item["type"] == "permission" for item in items))
            self.assertFalse(any(item["type"] == "lesson" for item in items))
            (agent / "protocols/permissions.md").unlink()
            degraded = build_governance_packet(self.provider(agent), "anything")
            self.assertIn("governance_permissions_missing", degraded.warnings)
            self.assertEqual(degraded.health["governance"]["status"], "degraded")

    def test_preferences_and_decisions_are_parsed_as_active_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.fixture(Path(tmp))
            (agent / "memory/personal/PREFERENCES.md").write_text(
                "# Preferences\n- _(e.g., placeholder)_\n- Keep responses concise.\n"
            )
            padding = "ignored superseded material " * 100
            (agent / "memory/semantic/DECISIONS.md").write_text(
                "# Decisions\n## 2025-01-01: Old\n**Decision:** Old choice.\n"
                f"**Rationale:** {padding}\n**Status:** superseded\n"
                "## 2026-01-01: Current\n**Decision:** Use current choice.\n"
                "**Rationale:** Current evidence.\n**Status:** active\n"
            )
            items, _ = self.provider(agent).retrieve("anything")
            preferences = [item.summary for item in items if item.type == "preference"]
            decisions = [item.summary for item in items if item.type == "decision"]
            self.assertEqual(preferences, ["Keep responses concise."])
            self.assertTrue(any("Use current choice" in value for value in decisions))
            self.assertFalse(any("Old choice" in value for value in decisions))

    def test_sensitive_governance_is_redacted_without_losing_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.fixture(Path(tmp))
            (agent / "memory/personal/PREFERENCES.md").write_text(
                "- Use ~/.aws/credentials only through environment injection.\n"
            )
            packet = build_governance_packet(self.provider(agent), "credentials")
            items = packet.sections[0]["items"]
            self.assertTrue(any(item["type"] == "permission" for item in items))
            rendered = json.dumps(packet.to_dict())
            self.assertNotIn(".aws/credentials", rendered)
            self.assertIn("governance_redacted:preference", packet.warnings)

    def test_budget_priority_keeps_authority_records_before_lessons(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.fixture(Path(tmp))
            rows = [
                {"id": f"large-{i}", "claim": "deploy " + ("x" * 1900),
                 "conditions": ["deploy"], "status": "accepted"}
                for i in range(30)
            ]
            (agent / "memory/semantic/lessons.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            packet = build_governance_packet(self.provider(agent), "deploy", top_k=30)
            types = {item["type"] for item in packet.sections[0]["items"]}
            self.assertTrue({"permission", "preference", "decision", "review_queue"}.issubset(types))
            self.assertTrue(any(warning.startswith("governance_budget_dropped:") for warning in packet.warnings))

    def test_legacy_golden_output_and_eligible_state_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.fixture(Path(tmp))
            recall = load_recall()
            recall.LESSONS_JSONL = str(agent / "memory/semantic/lessons.jsonl")
            recall.LESSONS_MD = str(agent / "memory/semantic/LESSONS.md")
            result, meta = recall.recall("deploy timeout", top_k=3)
            expected = (
                "Consulted lessons for intent: 'deploy timeout'\n"
                "  (2 accepted lessons available in corpus)\n"
                "  → returned 2: LESSONS.md:1, lessons.jsonl:1\n\n"
                "  [1] lexical_overlap=0.667  Retry deployment timeouts safely  [lessons.jsonl]\n"
                "      conditions: deploy, timeout\n"
                "  [2] lexical_overlap=0.167  Seed deploy safety  [LESSONS.md]"
            )
            self.assertEqual(recall.format_pretty("deploy timeout", result, meta), expected)
            provider_items, _ = self.provider(agent).retrieve("deploy timeout", top_k=3)
            self.assertEqual(
                {item.summary for item in provider_items if item.type == "lesson"},
                {item["claim"] for item in result},
            )

    def test_cjk_and_low_overlap_paraphrase_establish_no_match_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.fixture(Path(tmp))
            for query in ("部署超时重试", "recover from a stalled release operation"):
                with self.subTest(query=query):
                    items, _ = self.provider(agent).retrieve(query)
                    self.assertFalse(any(item.type == "lesson" for item in items))

    def test_governance_recall_p95_is_below_250ms(self):
        # SLO covers an in-process local provider call, not Python CLI startup.
        provider = self.provider(AGENT)
        samples = []
        for _ in range(40):
            started = time.perf_counter()
            build_governance_packet(provider, "deploy timeout")
            samples.append(time.perf_counter() - started)
        p95 = statistics.quantiles(samples, n=20)[18]
        self.assertLess(p95, 0.250, f"current-corpus p95={p95:.6f}s")

    def test_upgrade_installs_phase2_provider_and_cli(self):
        from harness_manager.upgrade import upgrade
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".agent").mkdir()
            self.assertEqual(upgrade(project, ROOT, yes=True, log=lambda _msg: None), 0)
            self.assertTrue((project / ".agent/tools/memory_orchestrate.py").is_file())
            self.assertTrue((project / ".agent/memory/orchestration/providers/governance.py").is_file())

    def test_cli_json_legacy_comparison(self):
        baseline_source = (
            AGENT / "memory/orchestration/legacy_recall_baseline.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("import governance_recall", baseline_source)
        result = subprocess.run(
            [sys.executable, str(AGENT / "tools/memory_orchestrate.py"), "recall",
             "--intent", "serialize timestamps UTC", "--format", "json", "--legacy"],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["context_packet"]["schema"], "agentic.memory.context.v1")
        self.assertIn("legacy", payload)
        legacy_claims = {item["claim"] for item in payload["legacy"]["result"]}
        new_claims = {
            item["summary"] for item in payload["context_packet"]["sections"][0]["items"]
            if item["type"] == "lesson"
        }
        self.assertEqual(legacy_claims, new_claims)


if __name__ == "__main__":
    unittest.main()
