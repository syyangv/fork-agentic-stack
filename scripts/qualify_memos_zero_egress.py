#!/usr/bin/env python3
"""Run the pinned MemOS bridge under an application-level network tripwire."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "0123456789abcdef"
MODEL_INVENTORY = {
    "retrieval": {"mode": "lexical", "engine": "sqlite_fts5", "model": None},
    "embedding": {"enabled": False, "provider_credentials": False},
    "memos_llm": {"mode": "disabled", "provider_credentials": False},
    "host_evolution": {"mode": "disabled", "route": "approved_host_only"},
    "remote_fallback": False,
}
TRIPWIRE = r'''"use strict";
const fs = require("fs");
const dns = require("dns");
const net = require("net");
const tls = require("tls");
const http = require("http");
const https = require("https");
const dgram = require("dgram");
const evidence = process.env.MEMOS_EGRESS_EVIDENCE;
function record(kind, args) {
  const safe = Array.from(args).slice(0, 3).map((value) => {
    if (value === null || value === undefined) return value;
    if (typeof value === "string" || typeof value === "number") return value;
    if (typeof value === "object") return {host: value.host, hostname: value.hostname, port: value.port};
    return typeof value;
  });
  fs.appendFileSync(evidence, JSON.stringify({kind, args: safe}) + "\n", {mode: 0o600});
  const error = new Error("network disabled by MemOS qualification tripwire");
  error.code = "MEMOS_ZERO_EGRESS";
  return error;
}
for (const name of ["lookup", "resolve", "resolve4", "resolve6", "resolveAny", "reverse"]) {
  if (typeof dns[name] === "function") dns[name] = function(...args) {
    const error = record("dns." + name, args);
    const callback = args.findLast((value) => typeof value === "function");
    if (callback) return process.nextTick(callback, error);
    throw error;
  };
}
net.Socket.prototype.connect = function(...args) { throw record("net.Socket.connect", args); };
net.connect = net.createConnection = function(...args) { throw record("net.connect", args); };
tls.connect = function(...args) { throw record("tls.connect", args); };
for (const [name, module] of [["http", http], ["https", https]]) {
  module.request = function(...args) { throw record(name + ".request", args); };
  module.get = function(...args) { throw record(name + ".get", args); };
}
const originalCreateSocket = dgram.createSocket;
dgram.createSocket = function(...args) {
  const socket = originalCreateSocket.apply(this, args);
  socket.connect = function(...values) { throw record("dgram.connect", values); };
  socket.send = function(...values) { throw record("dgram.send", values); };
  return socket;
};
global.fetch = async function(...args) { throw record("fetch", args); };
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _config() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / ".agent" / "memory"))
    from orchestration.memos_runtime import build_memos_config
    return build_memos_config(PROJECT_ID)


def _legacy_2010_config() -> dict[str, Any]:
    """Build only the isolated 2.0.10 seed profile used for copy rehearsals."""
    value = _config()
    value["embedding"] = {
        "provider": "local", "model": "Xenova/all-MiniLM-L6-v2",
    }
    # 2.0.10 requires a schema-valid provider label, but its loader must not be
    # invoked while seeding an isolated copy/rollback store.
    value["algorithm"]["capture"]["embedTraces"] = False
    return value


def _run_bridge(
    bridge: Path, state: Path, hook: Path, label: str, *, strict_framing: bool = True,
    config_value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state.mkdir(parents=True, exist_ok=True)
    config = state / "config.yaml"
    config.write_text(
        json.dumps(config_value or _config(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    attempts = state / "egress-attempts.jsonl"
    attempts.write_text("", encoding="utf-8")
    os.chmod(attempts, 0o600)
    env = {
        "HOME": str(state / "home"),
        "MEMOS_HOME": str(state),
        "MEMOS_CONFIG_FILE": str(config),
        "MEMOS_EGRESS_EVIDENCE": str(attempts),
        "NODE_OPTIONS": f"--require={hook}",
        "PATH": os.environ.get("PATH", os.defpath),
    }
    process = subprocess.Popen(
        ["node", str(bridge), "--agent=hermes", "--no-viewer", f"--runtime-scope={PROJECT_ID}"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    lines: list[str] = []
    responses: dict[int, dict[str, Any]] = {}

    def call(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("id") == request_id:
                responses[request_id] = candidate
                return candidate
        raise RuntimeError(f"no response for {method}")

    health_response = call(1, "core.health", {})
    session_id = f"zero-egress-{label}"
    namespace = {
        "agentKind": "hermes", "profileId": PROJECT_ID,
        "workspaceId": PROJECT_ID, "workspacePath": f"/synthetic/{PROJECT_ID}",
        "sessionKey": session_id,
    }
    common = {"agent": "hermes", "sessionId": session_id, "namespace": namespace}
    now_ms = int(time.time() * 1000)
    token = "synthetic-python-case-00-quartz"
    call(2, "session.open", {**common, "meta": {"qualification": True}})
    turn = call(3, "turn.start", {
        **common, "turnKey": f"turn-{label}",
        "userText": f"training example python {token}",
        "contextHints": {"synthetic": True}, "ts": now_ms,
    })
    episode_id = (turn.get("result") or {}).get("query", {}).get("episodeId")
    if not episode_id:
        raise RuntimeError("turn.start did not return an episode ID")
    call(4, "turn.end", {
        **common, "episodeId": episode_id,
        "agentText": "synthetic-solution-python-00-verified", "toolCalls": [],
        "contextHints": {"synthetic": True}, "ts": now_ms + 1,
    })
    call(5, "episode.close", {"episodeId": episode_id})
    call(6, "session.close", {"sessionId": session_id})
    search = call(7, "memory.search", {
        "agent": "hermes", "namespace": namespace,
        "query": f"recall training solution for {token}",
        "topK": {"tier1": 5, "tier2": 5, "tier3": 5},
        "filters": {"reason": "zero_egress_qualification"},
    })
    call(8, "core.shutdown", {})
    deadline = time.monotonic() + 20
    tail, stderr = process.communicate(timeout=max(1, deadline - time.monotonic()))
    lines.extend(line for line in tail.splitlines() if line.strip())
    decoded: list[dict[str, Any]] = []
    invalid: list[str] = []
    for line in lines:
        try:
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                raise ValueError
            decoded.append(value)
        except (json.JSONDecodeError, ValueError):
            invalid.append(line[:200])
    attempts_rows = [json.loads(line) for line in attempts.read_text().splitlines() if line]
    raw_health = health_response.get("result")
    health = None if not isinstance(raw_health, dict) else {
        "ok": raw_health.get("ok"),
        "version": raw_health.get("version"),
        "agent": raw_health.get("agent"),
        "embedder_provider": (raw_health.get("embedder") or {}).get("provider"),
        "llm_provider": (raw_health.get("llm") or {}).get("provider"),
    }
    model_inventory = {
        **MODEL_INVENTORY,
        "observed": {
            "embedder_provider": None if health is None else health["embedder_provider"],
            "llm_provider": None if health is None else health["llm_provider"],
        },
    }
    framing = [
        {"type": "response", "id": row["id"]}
        if "id" in row else {"type": "notification", "method": row.get("method")}
        for row in decoded
    ]
    config_value = json.loads(config.read_text())
    credentials = sorted(
        path.relative_to(state).as_posix() for path in state.rglob("telemetry.credentials.json")
    )
    search_hits = (search.get("result") or {}).get("hits", [])
    passed = (
        process.returncode == 0 and (not invalid or not strict_framing) and not attempts_rows
        and isinstance(health, dict)
        and not any("error" in response for response in responses.values())
        and config_value.get("telemetry", {}).get("enabled") is False
        and config_value.get("hub", {}).get("enabled") is False
        and config_value.get("embedding") == {
            "enabled": False, "provider": "lexical", "engine": "sqlite_fts5",
        }
        and health.get("embedder_provider") in (None, "lexical")
        and health.get("llm_provider") == "local_only"
        and bool(search_hits)
        and not credentials
    )
    return {
        "label": label,
        "passed": passed,
        "returncode": process.returncode,
        "health": health,
        "model_inventory": model_inventory,
        "stdout": {"line_count": len(lines), "frames": framing, "invalid_lines": invalid},
        "strict_jsonrpc_framing_required": strict_framing,
        "stderr_bytes": len(stderr.encode()),
        "egress_attempts": attempts_rows,
        "workload": {
            "populated_turn": True,
            "retrieval": True,
            "search_hit_count": len(search_hits),
        },
        "controls": {
            "telemetry_enabled": config_value["telemetry"]["enabled"],
            "hub_enabled": config_value["hub"]["enabled"],
            "runtime_credential_files": credentials,
            "provider_credentials_present": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-2-0-14", type=Path, required=True)
    parser.add_argument("--bridge-2-0-10", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bridge_new = args.bridge_2_0_14.resolve(strict=True)
    bridge_old = args.bridge_2_0_10.resolve(strict=True)
    system_tmp = Path(tempfile.gettempdir()).resolve()
    try:
        args.output.resolve().relative_to(system_tmp)
    except ValueError:
        parser.error(f"qualification output must be below the system temp directory: {system_tmp}")
    with tempfile.TemporaryDirectory(prefix="memos-zero-egress-", dir=system_tmp) as tmp:
        work = Path(tmp)
        hook = work / "network-tripwire.cjs"
        hook.write_text(TRIPWIRE, encoding="utf-8")
        fresh = work / "fresh"
        fresh_result = _run_bridge(bridge_new, fresh, hook, "fresh-2.0.14")

        old_source = work / "source-2.0.10"
        old_result = _run_bridge(
            bridge_old, old_source, hook, "source-2.0.10", strict_framing=False,
            config_value=_legacy_2010_config(),
        )
        copied = work / "copied-2.0.10"
        shutil.copytree(old_source, copied, ignore=shutil.ignore_patterns("daemon", "egress-attempts.jsonl"))
        copied_result = _run_bridge(bridge_new, copied, hook, "copied-2.0.10-opened-by-2.0.14")

        pristine = work / "pristine-2.0.10"
        shutil.copytree(old_source, pristine, ignore=shutil.ignore_patterns("daemon", "egress-attempts.jsonl"))
        restored = work / "restored-2.0.10"
        shutil.copytree(pristine, restored)
        restored_result = _run_bridge(bridge_new, restored, hook, "restored-2.0.10-opened-by-2.0.14")

        artifact: dict[str, Any] = {
            "schema_version": 1,
            "qualification": "memos-2.0.14-zero-egress",
            "project_id": PROJECT_ID,
            "isolation_root": str(system_tmp),
            "observation": {
                "layer": "Node application APIs",
                "apis": ["dns", "net", "tls", "http", "https", "dgram", "fetch"],
                "policy": "record and fail every attempted operation",
                "native_syscall_observation": False,
            },
            "inputs": {
                "bridge_2_0_14_sha256": _sha256(bridge_new),
                "bridge_2_0_10_sha256": _sha256(bridge_old),
                "tripwire_sha256": _sha256(hook),
                "source_2_0_10_tree_sha256": _tree_digest(old_source),
                "pristine_restore_tree_sha256": _tree_digest(pristine),
            },
            "states": [fresh_result, old_result, copied_result, restored_result],
        }
        # The old source run is a seed/baseline and is expected to violate the
        # new model policy. Qualification applies to all states opened by the
        # reviewed 2.0.14 lexical distribution.
        artifact["passed"] = all(
            row["passed"] for row in (fresh_result, copied_result, restored_result)
        )
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
        output.write_text(payload, encoding="utf-8")
        checksum = output.with_suffix(output.suffix + ".sha256")
        checksum.write_text(f"{_sha256(output)}  {output.name}\n", encoding="utf-8")
        print(json.dumps({"passed": artifact["passed"], "evidence": str(output), "sha256": _sha256(output)}))
        return 0 if artifact["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
