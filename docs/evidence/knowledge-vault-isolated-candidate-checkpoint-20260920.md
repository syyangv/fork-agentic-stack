# Knowledge Vault Isolated Candidate — Pre-Restart Checkpoint

**Date:** 2026-09-20  
**Purpose:** Durable recovery point before restarting only the disposable
candidate daemon for persistence/reconnect verification.

## Isolation boundary

- Live daemon `127.0.0.1:6767`: untouched.
- Candidate daemon: `127.0.0.1:16767`.
- Candidate home: `/private/tmp/kv-candidate-runtime-20260920`.
- Relay, Agent MCP, and MCP injection: disabled.
- Candidate agents/workspaces: zero.
- Stores: synthetic work vault, empty synthetic personal vault, private
  synthetic runtime/draft/task-state paths under the candidate home.
- No production config, credentials, or vault paths were copied.

## Candidate identity

- Base commit: `af2c471c45d9f4b9b735ec5cf3e88528e8c46dd0`.
- Bounded hardening delta: `service.ts`, `task-store.ts`, and
  `paseo-knowledge.test.ts` only.
- Delta committed as `52ba9f9` (`chore/knowledge-vault-remaining-acceptance`).
- Delta SHA256:
  `a0f2be9219bdd7d207f9046a020634f70c5d50c0e5f6249909a23667f64315c4`.
- Lockfile SHA256:
  `549589ff13420ebc568a9a2a05f3583a3b6f5cf109213eea9823e42aea8bb884`.

File hashes:

| File | SHA256 |
|---|---|
| `index.client.tsx` | `c6adb6df51495fb781c2571000c1f9a263f37485423493993d2447adfca645f7` |
| `server/service.ts` | `ecba2017ee9f305060e76bd6b32250a4e06f822beced24dc7bfc9e8ad36b4bf1` |
| `server/task-store.ts` | `81c7684c065a17fd18815b402b78c9ca4de94569f2fccf9ed123e0dd10d37c32` |
| `tests/paseo-knowledge.test.ts` | `164d8b04e629f7a36c08a012b1325cebf5513fe6c541f1ee0f110e4b563f1549` |
| synthetic `task-state.json` | `c4b89835467ee1a3afcdfbeb15b730dd56a6275a6930096e67e9c931e2aadb31` |

## Verification before restart

- Explicit ARM64 Node toolchain: typecheck **PASS**.
- Vitest: **26/26 PASS** across 2 files. Bound-state coverage includes
  `starting`, `submitted`, `running`, `awaiting_approval`, `completed`,
  `failed`, `timed_out`, plus the explicit `unknown` ambiguity case.
- Plugin installed only on candidate host: enabled/running.
- `knowledge.status`: configured/core ready.
- `knowledge.search`: synthetic result returned.
- `knowledge.task.prepare`: synthetic untrusted context prepared.
- `knowledge.task.start`: failed non-retryably with
  `tool_isolation_unavailable`; `agentId` and `turnId` remained null.
- Disable: plugin status became disabled and RPC probe exited nonzero.
- Re-enable: plugin returned running exactly once.
- Post-enable status retained task identity, failed status, exact error code,
  message, and `retryable=false`.

## Durable artifacts

`docs/evidence/logs/knowledge-vault-isolated-candidate-20260920/`

- `source-delta.patch.txt`
- `plugin-list-before-restart.txt`
- `status-before-restart.txt`

## Restart recovery contract

Restart only the disposable candidate daemon using the same home, loopback
listener, synthetic environment variables, and disabled relay/MCP flags. After
restart, require plugin running, configured/core ready, and exact persisted
`ktask_isolated_001` identity plus `tool_isolation_unavailable` error. On
failure, retain this checkpoint and logs; do not touch or restart the live
daemon.

## Reconciliation — 2026-11-10

This table was first written after the code fix but before the
reviewer-requested status-coverage tests landed. It recorded delta
`29204224c22791359cdaddaacf5958f29dea1777e92dadbfce3ba10ca143e61a`,
test file `97dc4d3beeae15d5038a756792b132a63b21b4d7e9e6851872060b82dca2d69d`,
and 25/25. The restart evidence in
`knowledge-vault-isolated-candidate-runtime-20260920.md` was collected on the
later delta, so the values above are the ones the restart actually used.

`server/service.ts` and `server/task-store.ts` hashes are unchanged between the
two revisions; only the test file differs. Both sets of values are kept here so
the earlier checkpoint stays auditable. The delta was committed as `52ba9f9`
and re-verified on 2026-11-10: ARM64 typecheck exit 0 and Vitest 26/26.

