from __future__ import annotations

import sys
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.evolution_eval import (
    build_skill_injection_record, build_task_protocol_record,
    evaluate_held_out_tasks,
)


REVISION = "a" * 40


def rows(count: int = 20, *, assisted_cost: float = 8.0):
    observations = []
    ledger = {}
    execution_ledger = {}
    protocol_ledger = {}
    for index in range(count):
        protocol = build_task_protocol_record(
            task_id=f"task-{index}", project_id="0123456789abcdef", repository_revision=REVISION,
            task_definition_digest="sha256:" + hashlib.sha256(
                f"task-{index}:definition".encode()
            ).hexdigest(),
            harness_digest="sha256:" + hashlib.sha256(b"paired-harness-v1").hexdigest(),
        )
        protocol_digest = protocol["protocol_digest"]
        protocol_ledger[protocol_digest] = protocol
        skill_digest = "sha256:" + hashlib.sha256(b"candidate-skill-v1").hexdigest()
        injection = build_skill_injection_record(
            task_id=f"task-{index}", project_id="0123456789abcdef", repository_revision=REVISION,
            protocol_digest=protocol_digest, skill_digest=skill_digest,
            context_packet_digest="sha256:" + hashlib.sha256(
                f"task-{index}:rendered-context".encode()
            ).hexdigest(),
            observed_at=f"2026-07-19T00:{index:02d}:00Z",
        )
        injection_id = injection["evidence_id"]
        ledger[injection_id] = injection
        evidence = {}
        for arm, cost in (("baseline", 10.0), ("assisted", assisted_cost)):
            command_digest = "sha256:" + f"{index * 2 + (arm == 'assisted'):064x}"
            completed_at = f"2026-07-19T00:{index:02d}:0{int(arm == 'assisted')}Z"
            locator = {
                "executed_test": True, "exit_code": 0,
                "test_ids": [f"phase8-eval:task-{index}:{arm}"],
                "command_digest": command_digest,
                "protocol_digest": protocol_digest,
                "completed_at": completed_at,
                "duration_ms": int(cost * 1000),
            }
            if arm == "assisted":
                locator["skill_injection_evidence_id"] = injection_id
                locator["injected_skill_digest"] = skill_digest
            seed = json.dumps({
                "kind": "test_run", "project_id": "0123456789abcdef",
                "revision": REVISION, "locator": locator,
            }, separators=(",", ":"), sort_keys=True)
            execution_id = "evi_" + hashlib.sha256(seed.encode()).hexdigest()
            execution_ledger[execution_id] = {
                "schema": "agentic.memory.evidence-ledger.v1",
                "evidence_id": execution_id,
                "summary": "Explicit test run passed: 1 test identifier(s)",
                "provenance": {
                    "kind": "test_run", "provider": "test-runner",
                    "source_id": execution_id, "repository_revision": REVISION,
                    "project_id": "0123456789abcdef", "source_hash": command_digest,
                    "observed_at": completed_at, "confidence": 1.0,
                    "freshness": "fresh", "locator": locator,
                },
                "verification": {
                    "repository_reconciled": True, "files_reconciled": False,
                    "symbols_reconciled": False, "executed_test": True,
                },
            }
            body = {
                "schema": "agentic.memory.evolution-observation.v1",
                "task_id": f"task-{index}", "arm": arm,
                "repository_revision": REVISION, "executable": True,
                "success": True, "cost": cost,
                "cost_metric": "completion_seconds",
                "skill_injected": arm == "assisted",
                "protocol_digest": protocol_digest,
                "execution_evidence_ids": [execution_id],
            }
            evidence_id = "evi_" + hashlib.sha256(json.dumps(
                body, separators=(",", ":"), sort_keys=True,
            ).encode()).hexdigest()
            evidence[arm] = evidence_id
            ledger[evidence_id] = {"evidence_id": evidence_id, **body}
        observations.append({
        "task_id": f"task-{index}",
        "baseline_success": True,
        "assisted_success": True,
        "baseline_cost": 10.0,
        "assisted_cost": assisted_cost,
        "cost_metric": "completion_seconds",
        "skill_injected": True,
        "protocol_digest": protocol_digest,
        "skill_digest": skill_digest,
        "injection_evidence_id": injection_id,
        "baseline_evidence_id": evidence["baseline"],
        "assisted_evidence_id": evidence["assisted"],
        })
    return observations, ledger, execution_ledger, protocol_ledger


def evaluate(values):
    observations, ledger, execution_ledger, protocol_ledger = values
    return evaluate_held_out_tasks(
        observations, evidence_ledger=ledger, execution_ledger=execution_ledger,
        protocol_ledger=protocol_ledger,
        project_id="0123456789abcdef", repository_revision=REVISION,
    )


class EvolutionEvalTest(unittest.TestCase):
    def test_accepts_twenty_nonregressing_tasks_with_ten_percent_gain(self):
        result = evaluate(rows())
        self.assertTrue(result.eligible)
        self.assertEqual(result.task_count, 20)
        self.assertAlmostEqual(result.relative_median_improvement, 0.2)

    def test_fails_closed_on_sample_regression_gain_or_missing_skill_pairs(self):
        too_few = evaluate(rows(19))
        self.assertIn("minimum_20_held_out_tasks", too_few.failures)
        slow = evaluate(rows(20, assisted_cost=9.5))
        self.assertIn("median_improvement_below_10_percent", slow.failures)
        regressed, ledger, execution, protocols = rows()
        regressed[0]["assisted_success"] = False
        assisted = ledger[regressed[0]["assisted_evidence_id"]]
        assisted["success"] = False
        execution_id = assisted["execution_evidence_ids"][0]
        execution_record = execution.pop(execution_id)
        execution_record["provenance"]["locator"]["exit_code"] = 1
        provenance = execution_record["provenance"]
        seed = json.dumps({
            "kind": "test_run", "project_id": provenance["project_id"],
            "revision": provenance["repository_revision"],
            "locator": provenance["locator"],
        }, separators=(",", ":"), sort_keys=True)
        execution_id = "evi_" + hashlib.sha256(seed.encode()).hexdigest()
        execution_record["evidence_id"] = execution_id
        provenance["source_id"] = execution_id
        execution[execution_id] = execution_record
        assisted["execution_evidence_ids"] = [execution_id]
        body = dict(assisted); body.pop("evidence_id")
        new_id = "evi_" + hashlib.sha256(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        assisted["evidence_id"] = new_id
        ledger[new_id] = assisted
        del ledger[regressed[0]["assisted_evidence_id"]]
        regressed[0]["assisted_evidence_id"] = new_id
        self.assertIn(
            "assisted_success_regressed", evaluate((regressed, ledger, execution, protocols)).failures,
        )
        no_skills, ledger, execution, protocols = rows()
        for row in no_skills:
            row["skill_injected"] = False
            record = ledger[row["assisted_evidence_id"]]
            record["skill_injected"] = False
            body = dict(record); body.pop("evidence_id")
            new_id = "evi_" + hashlib.sha256(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
            record["evidence_id"] = new_id; ledger[new_id] = record
            del ledger[row["assisted_evidence_id"]]; row["assisted_evidence_id"] = new_id
        with self.assertRaisesRegex(ValueError, "injection"):
            evaluate((no_skills, ledger, execution, protocols))

    def test_rejects_duplicate_tasks_unbound_evidence_and_nonfinite_cost(self):
        duplicate, ledger, execution, protocols = rows()
        duplicate[1]["task_id"] = duplicate[0]["task_id"]
        with self.assertRaises(ValueError):
            evaluate((duplicate, ledger, execution, protocols))
        invalid, ledger, execution, protocols = rows()
        invalid[0]["baseline_evidence_id"] = "not-evidence"
        with self.assertRaises(ValueError):
            evaluate((invalid, ledger, execution, protocols))
        invalid, ledger, execution, protocols = rows()
        invalid[0]["baseline_cost"] = float("nan")
        with self.assertRaises(ValueError):
            evaluate((invalid, ledger, execution, protocols))
        invalid, ledger, execution, protocols = rows()
        invalid[0]["baseline_evidence_id"] = "evi_" + "z" * 64
        with self.assertRaises(ValueError):
            evaluate((invalid, ledger, execution, protocols))
        invalid, ledger, execution, protocols = rows()
        invalid[0]["cost_metric"] = "model_judgment"
        with self.assertRaises(ValueError):
            evaluate((invalid, ledger, execution, protocols))

    def test_rejects_missing_fabricated_or_mismatched_ledger_observations(self):
        observations, ledger, execution, protocols = rows()
        target = observations[0]["baseline_evidence_id"]
        del ledger[target]
        with self.assertRaises(ValueError):
            evaluate((observations, ledger, execution, protocols))

    def test_rejects_noncanonical_execution_and_unproven_or_mismatched_injection(self):
        with self.assertRaisesRegex(ValueError, "digests"):
            build_skill_injection_record(
                task_id="task", project_id="0123456789abcdef",
                repository_revision=REVISION,
                protocol_digest="sha256:" + "a" * 64,
                skill_digest="sha256:" + "b" * 64,
                context_packet_digest="NOT-A-DIGEST",
                observed_at="2026-07-19T00:00:00Z",
            )
        with self.assertRaisesRegex(ValueError, "timestamp"):
            build_skill_injection_record(
                task_id="task", project_id="0123456789abcdef",
                repository_revision=REVISION,
                protocol_digest="sha256:" + "a" * 64,
                skill_digest="sha256:" + "b" * 64,
                context_packet_digest="sha256:" + "c" * 64,
                observed_at="NOT-A-TIMESTAMP",
            )
        observations, ledger, execution, protocols = rows()
        first_execution = next(iter(execution.values()))
        first_execution["provenance"]["locator"]["duration_ms"] += 1
        with self.assertRaisesRegex(ValueError, "canonical"):
            evaluate((observations, ledger, execution, protocols))

        observations, ledger, execution, protocols = rows()
        del ledger[observations[0]["injection_evidence_id"]]
        with self.assertRaisesRegex(ValueError, "injection"):
            evaluate((observations, ledger, execution, protocols))

        observations, ledger, execution, protocols = rows()
        observations[0]["protocol_digest"] = "sha256:" + "f" * 64
        with self.assertRaises(ValueError):
            evaluate((observations, ledger, execution, protocols))
        observations, ledger, execution, protocols = rows()
        with self.assertRaisesRegex(ValueError, "protocol|project"):
            evaluate_held_out_tasks(
                observations, evidence_ledger=ledger, execution_ledger=execution,
                protocol_ledger=protocols, project_id="fedcba9876543210",
                repository_revision=REVISION,
            )
        observations, ledger, execution, protocols = rows()
        ledger[observations[0]["baseline_evidence_id"]]["task_id"] = "other-task"
        with self.assertRaises(ValueError):
            evaluate((observations, ledger, execution, protocols))
        observations, ledger, execution, protocols = rows()
        del execution[next(iter(execution))]
        with self.assertRaises(ValueError):
            evaluate((observations, ledger, execution, protocols))
        observations, ledger, execution, protocols = rows()
        first = observations[0]["baseline_evidence_id"]
        second = observations[1]["baseline_evidence_id"]
        ledger[second]["execution_evidence_ids"] = ledger[first]["execution_evidence_ids"]
        body = dict(ledger[second]); body.pop("evidence_id")
        new_id = "evi_" + hashlib.sha256(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        ledger[new_id] = {"evidence_id": new_id, **body}
        del ledger[second]; observations[1]["baseline_evidence_id"] = new_id
        with self.assertRaises(ValueError):
            evaluate((observations, ledger, execution, protocols))


if __name__ == "__main__":
    unittest.main()
