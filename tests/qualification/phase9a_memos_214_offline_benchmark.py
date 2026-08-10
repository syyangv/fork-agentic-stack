#!/usr/bin/env python3
"""Reproducible synthetic-only MemOS 2.0.14 Phase 9A qualification replay."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.memos_bridge import BridgeConfig, MemOSBridgeClient
from orchestration.memos_runtime import build_memos_config, write_config_atomic


PROJECT_A = "0123456789abcdef"
PROJECT_B = "fedcba9876543210"
CATEGORIES = ("python", "testing", "database", "frontend", "operations")
CORPUS_ID = "phase9a-memos-2.0.14-synthetic-v2-five-relevant-per-query"
QUERY_COUNT = 30
RELEVANT_PER_QUERY = 5
EPISODE_COUNT = QUERY_COUNT * RELEVANT_PER_QUERY


def percentile(values: list[float], percentile_value: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile_value)))
    return round(ordered[index], 3)


def summary(values: list[float], unit: str = "ms") -> dict[str, float]:
    return {
        f"p50_{unit}": round(statistics.median(values), 3),
        f"p95_{unit}": percentile(values, 0.95),
    }


def timed(call, ledger: list[float]):
    started = time.monotonic_ns()
    result = call()
    ledger.append((time.monotonic_ns() - started) / 1_000_000)
    return result


def namespace(project_id: str, session_id: str) -> dict[str, str]:
    return {
        "agentKind": "hermes",
        "profileId": project_id,
        "workspaceId": project_id,
        "workspacePath": f"/synthetic/{project_id}",
        "sessionKey": session_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    system_tmp = Path(tempfile.gettempdir()).resolve()
    try:
        args.work_root.resolve().relative_to(system_tmp)
    except ValueError:
        raise SystemExit(
            f"--work-root must be below the system temp directory: {system_tmp}"
        )
    if args.work_root.exists():
        raise SystemExit("--work-root must not already exist")
    args.work_root.mkdir(parents=True)
    args.output.mkdir(parents=True, exist_ok=True)

    bridge = args.plugin_root / "node_modules/@memtensor/memos-local-plugin/dist/bridge.cjs"
    package = json.loads((bridge.parents[1] / "package.json").read_text("utf-8"))
    if package.get("version") != "2.0.14":
        raise SystemExit("plugin root is not MemOS 2.0.14")

    memos_home = args.work_root / "memos"
    home = args.work_root / "home"
    memos_home.mkdir()
    home.mkdir()
    config = memos_home / "config.yaml"
    write_config_atomic(config, build_memos_config(PROJECT_A))
    egress_log = args.output / "egress-attempts.jsonl"
    preload = args.work_root / "socket-audit.cjs"
    preload.write_text(
        """const fs=require('fs'); const log=process.env.PHASE9A_EGRESS_LOG;
const deny=(api,args)=>{fs.appendFileSync(log,JSON.stringify({api,args:[...args].map(String)})+'\\n');throw new Error('phase9a offline network denied: '+api)};
for(const mod of ['net','tls','dgram','http','https']){const m=require(mod); for(const fn of ['connect','createConnection','request','get']) if(typeof m[fn]==='function')m[fn]=function(...a){return deny(mod+'.'+fn,a)}}
if(typeof globalThis.fetch==='function')globalThis.fetch=function(...a){return Promise.reject(deny('fetch',a))};
""",
        "utf-8",
    )
    egress_log.write_text("", "utf-8")
    environment = {
        "HOME": str(home),
        "MEMOS_HOME": str(memos_home),
        "MEMOS_CONFIG_FILE": str(config),
        "MEMOS_TELEMETRY_ENABLED": "0",
        "NODE_OPTIONS": f"--require={preload}",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PHASE9A_EGRESS_LOG": str(egress_log),
    }
    command = (
        "node", str(bridge), "--agent=hermes", "--no-viewer",
        f"--runtime-scope={PROJECT_A}", f"--home={memos_home}",
    )
    client = MemOSBridgeClient(BridgeConfig(
        command=command, env=environment, inherit_environment=False,
        call_timeout=20, shutdown_timeout=3, circuit_cooldown=0,
    ))
    latencies: dict[str, list[float]] = {
        key: [] for key in ("health", "session_open", "turn_start", "turn_end",
                            "episode_close", "session_close", "search", "total")
    }
    raw: list[dict] = []
    expected: dict[str, str] = {}
    episode_by_key: dict[str, set[str]] = {}
    errors: list[str] = []
    health = timed(client.health, latencies["health"])
    try:
        for index in range(EPISODE_COUNT):
            total_started = time.monotonic_ns()
            query_index = index // RELEVANT_PER_QUERY
            variant = index % RELEVANT_PER_QUERY
            category = CATEGORIES[query_index % len(CATEGORIES)]
            token = f"synthetic-{category}-case-{query_index:02d}-quartz"
            solution = f"synthetic-solution-{category}-{query_index:02d}-verified variant-{variant}"
            session_id = f"synthetic-session-{query_index:02d}-{variant}"
            turn_key = f"synthetic-turn-{query_index:02d}-{variant}"
            ns = namespace(PROJECT_A, session_id)
            common = {"agent": "hermes", "sessionId": session_id, "namespace": ns}
            timed(lambda: client.call("session.open", {**common, "meta": {}}, timeout=20),
                  latencies["session_open"])
            params = {
                **common, "userText": f"training example {category} {token}",
                "turnKey": turn_key, "contextHints": {"synthetic": True},
                "ts": int(time.time() * 1000),
            }
            first = timed(lambda: client.call("turn.start", params, timeout=20),
                          latencies["turn_start"])
            episode_id = first["query"]["episodeId"]
            episode_by_key.setdefault(turn_key, set()).add(episode_id)
            if index < 5:
                duplicate = timed(lambda: client.call("turn.start", params, timeout=20),
                                  latencies["turn_start"])
                episode_by_key[turn_key].add(duplicate["query"]["episodeId"])
            ended = timed(lambda: client.call("turn.end", {
                **common, "episodeId": episode_id, "agentText": solution,
                "toolCalls": [], "contextHints": {"synthetic": True},
                "ts": int(time.time() * 1000),
            }, timeout=20), latencies["turn_end"])
            expected[ended["traceId"]] = token
            timed(lambda: client.call("episode.close", {"episodeId": episode_id}, timeout=20),
                  latencies["episode_close"])
            timed(lambda: client.call("session.close", {"sessionId": session_id}, timeout=20),
                  latencies["session_close"])
            latencies["total"].append((time.monotonic_ns() - total_started) / 1_000_000)

        relevant_hits = 0
        returned_hits = 0
        useful_queries = 0
        context_bytes: list[int] = []
        for index in range(QUERY_COUNT):
            category = CATEGORIES[index % len(CATEGORIES)]
            token = f"synthetic-{category}-case-{index:02d}-quartz"
            result = timed(lambda token=token: client.call("memory.search", {
                "agent": "hermes", "namespace": namespace(PROJECT_A, "query"),
                "query": f"recall training solution for {token}",
                "topK": {"tier1": 5, "tier2": 5, "tier3": 5},
                "filters": {"reason": "synthetic_offline_qualification"},
            }, timeout=20, retryable=True), latencies["search"])
            hits = result.get("hits", [])[:5]
            relevance = [expected.get(hit.get("refId")) == token for hit in hits]
            relevant_hits += sum(relevance)
            returned_hits += len(hits)
            context = result.get("injectedContext", "")
            context_bytes.append(len(context.encode("utf-8")))
            useful = any(relevance) and f"synthetic-solution-{category}-{index:02d}-verified" in context
            useful_queries += int(useful)
            raw.append({"query_index": index, "token": token, "hit_ids": [h.get("refId") for h in hits],
                        "relevant": relevance, "useful": useful,
                        "context_bytes": context_bytes[-1],
                        "tier_latency_ms": result.get("tierLatencyMs")})

        leakage_hits = 0
        for index in range(10):
            result = timed(lambda index=index: client.call("memory.search", {
                "agent": "hermes", "namespace": namespace(PROJECT_B, "adversarial"),
                "query": f"synthetic-python-case-{index:02d}-quartz",
                "topK": {"tier1": 5, "tier2": 5, "tier3": 5}, "filters": {},
            }, timeout=20, retryable=True), latencies["search"])
            leakage_hits += len(result.get("hits", []))
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        shutdown_started = time.monotonic_ns()
        client.close()
        shutdown_ms = (time.monotonic_ns() - shutdown_started) / 1_000_000

    attempts = [json.loads(line) for line in egress_log.read_text("utf-8").splitlines() if line]
    attempt_apis = sorted({attempt["api"] for attempt in attempts})
    attempt_targets = sorted({
        argument for attempt in attempts for argument in attempt.get("args", [])
        if argument.startswith(("http://", "https://"))
    })
    duplicate_turns = sum(len(ids) > 1 for ids in episode_by_key.values())
    metrics = {
        "schema": "agentic.memory.phase9a-offline-qualification.v1",
        "corpus_id": CORPUS_ID,
        "corpus_policy": "generated synthetic/training only; no held-out material",
        "runtime_version": health["version"],
        "modes": {"deployed": "unchanged/off", "benchmark": "isolated shadow-only",
                  "assist": False, "evolution": False, "r8_run": False},
        "completed_episodes": EPISODE_COUNT, "task_categories": len(CATEGORIES),
        "evaluation_queries": QUERY_COUNT,
        "relevant_documents_per_query": RELEVANT_PER_QUERY,
        "precision_at_5_method": (
            "conventional fixed-denominator P@5: relevant hits in first five / "
            "(evaluation queries * 5); corpus preregisters five relevant traces per query"
        ),
        "precision_at_5": round(relevant_hits / (QUERY_COUNT * 5), 6),
        "precision_among_returned": round(relevant_hits / max(1, returned_hits), 6),
        "query_usefulness_rate": round(useful_queries / QUERY_COUNT, 6),
        "context_bytes": summary([float(value) for value in context_bytes], "bytes"),
        "latency": {name: summary(values) for name, values in latencies.items()},
        "shutdown_ms": round(shutdown_ms, 3),
        "duplicate_logical_turns": duplicate_turns,
        "duplicate_episode_rate": round(duplicate_turns / EPISODE_COUNT, 6),
        "degradation_errors": errors,
        "cross_project_leak_hits": leakage_hits,
        "instrumented_socket_attempt_apis": attempt_apis,
        "instrumented_socket_attempt_targets": attempt_targets,
        "observed_egress_attempts": len(attempts),
        "disabled_baseline": {"injected_context_bytes": 0, "usefulness_rate": 0,
                              "duplicate_episodes": 0},
    }
    metrics["gate_assessment"] = {
        "plausibly_clears_r8_preparation_bar": (
            metrics["precision_at_5"] >= 0.70
            and metrics["duplicate_episode_rate"] < 0.05
            and metrics["cross_project_leak_hits"] == 0
            and metrics["latency"]["search"]["p95_ms"] < 750
            and metrics["observed_egress_attempts"] == 0
            and not errors
        ),
        "stop_recommendation": None,
        "limitations": [
            "Synthetic replay can authorize R8 preparation only, never activation.",
            "NODE_OPTIONS socket hooks cover Node networking APIs but are not packet capture.",
            "Usefulness is deterministic expected-solution presence, not blinded human scoring.",
        ],
    }
    if not metrics["gate_assessment"]["plausibly_clears_r8_preparation_bar"]:
        metrics["gate_assessment"]["stop_recommendation"] = "STOP before R8; preliminary gate failed"
    raw_path = args.output / "raw-ledger.json"
    metrics_path = args.output / "summary.json"
    raw_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", "utf-8")
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", "utf-8")
    checksum_inputs = {
        "repo:tests/qualification/phase9a_memos_214_offline_benchmark.py": Path(__file__),
        "evidence:raw-ledger.json": raw_path,
        "evidence:summary.json": metrics_path,
        "evidence:egress-attempts.jsonl": egress_log,
        "runtime:dist/bridge.cjs": bridge,
        "runtime:package.json": bridge.parents[1] / "package.json",
    }
    checksums = {
        label: hashlib.sha256(path.read_bytes()).hexdigest()
        for label, path in checksum_inputs.items()
    }
    (args.output / "SHA256SUMS.json").write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n", "utf-8"
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
