from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "qualify_memos_zero_egress.py"
SPEC = importlib.util.spec_from_file_location("zero_egress_qualification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ZeroEgressQualificationTest(unittest.TestCase):
    def test_tripwire_observes_and_blocks_socket_attempt(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmp:
            root = Path(tmp)
            hook = root / "tripwire.cjs"
            evidence = root / "attempts.jsonl"
            hook.write_text(MODULE.TRIPWIRE, encoding="utf-8")
            evidence.write_text("", encoding="utf-8")
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "NODE_OPTIONS": f"--require={hook}",
                "MEMOS_EGRESS_EVIDENCE": str(evidence),
            }
            result = subprocess.run(
                ["node", "-e", "require('net').connect(443, 'example.invalid')"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            rows = [json.loads(line) for line in evidence.read_text().splitlines()]
            self.assertEqual(rows[0]["kind"], "net.connect")

    def test_tripwire_source_checksum_is_stable(self) -> None:
        expected = "e6193a4569a519a6c7b692fadd542d024101a6c10f4afa9d07690d6425a7a6e6"
        import hashlib
        self.assertEqual(hashlib.sha256(MODULE.TRIPWIRE.encode()).hexdigest(), expected)

    def test_runner_exercises_populated_turn_and_retrieval(self) -> None:
        bridge_source = r'''const readline=require("readline");
const rl=readline.createInterface({input:process.stdin});
rl.on("line",line=>{const r=JSON.parse(line);let result={ok:true};
if(r.method==="core.health")result={ok:true,version:"2.0.14",agent:"hermes",embedder:{provider:"local"},llm:{provider:"local_only"}};
if(r.method==="turn.start")result={query:{episodeId:"episode-1"}};
if(r.method==="memory.search")result={hits:[{refId:"trace-1"}]};
process.stdout.write(JSON.stringify({jsonrpc:"2.0",id:r.id,result})+"\n");
if(r.method==="core.shutdown")setImmediate(()=>process.exit(0));});'''
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmp:
            root = Path(tmp)
            bridge = root / "bridge.cjs"
            hook = root / "tripwire.cjs"
            bridge.write_text(bridge_source, encoding="utf-8")
            hook.write_text(MODULE.TRIPWIRE, encoding="utf-8")
            result = MODULE._run_bridge(bridge, root / "state", hook, "test")
            self.assertTrue(result["passed"])
            self.assertEqual(result["workload"], {
                "populated_turn": True, "retrieval": True, "search_hit_count": 1,
            })


if __name__ == "__main__":
    unittest.main()
