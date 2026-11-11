# Knowledge Vault Isolated Candidate Runtime Evidence — 2026-09-20

## Verdict

**PARTIAL PASS.** Clean ARM64 tests, isolated server/RPC behavior, fail-closed
execution, durable error persistence, daemon reconnect, and plugin
disable/re-enable passed. Visual workspace/agent panel and Command Center
acceptance remain open because the available Chrome automation could not
navigate to the loopback web UI.

## Candidate under test

- Base commit: `af2c471c45d9f4b9b735ec5cf3e88528e8c46dd0`.
- Additional reviewed working delta:
  - persist `tool_isolation_unavailable` through task-store reload;
  - return explicit isolation error for already-bound historical tasks without
    changing their truthful status or invoking execution APIs;
  - focused tests for both behaviors.
- Delta SHA256 after reviewer-requested status-coverage tests:
  `a0f2be9219bdd7d207f9046a020634f70c5d50c0e5f6249909a23667f64315c4`.
- Delta committed as `52ba9f9`; re-verified 2026-11-10 (ARM64 typecheck PASS,
  Vitest 26/26).
- Lockfile SHA256:
  `549589ff13420ebc568a9a2a05f3583a3b6f5cf109213eea9823e42aea8bb884`.

## Isolation

- Candidate daemon/home: `127.0.0.1:16767`,
  `/private/tmp/kv-candidate-runtime-20260920`.
- Live daemon `127.0.0.1:6767` and installed source were untouched.
- Relay, Agent MCP, and MCP injection were disabled.
- Synthetic core shim, synthetic work vault, empty synthetic personal vault,
  and synthetic private runtime paths were used.
- No production configuration, credentials, or vault data were copied.
- Candidate had zero agents. One synthetic local workspace was created only
  after restart: `wks_2d660bb44e879ad4`.

## Tests

Using explicit ARM64 Node `v22.23.2`:

- `npm run typecheck`: **PASS**.
- `npm test`: **PASS**, 2 files and **26/26 tests**. Bound-state coverage
  includes `starting`, `submitted`, `running`, `awaiting_approval`,
  `completed`, `failed`, `timed_out`, plus explicit `unknown` ambiguity
  semantics with zero execution calls.

An earlier coordinator rerun accidentally resolved x64 Node and failed Vitest
startup against ARM64 dependencies. Repeating with the explicit ARM64 binary
passed; this was a toolchain-architecture mismatch, not a candidate failure.

## Runtime behavior

### Enabled path

- Plugin installed on isolated daemon: enabled/running.
- `knowledge.status`: `configured=true`, `coreStatus=ready`.
- `knowledge.search`: returned one synthetic work-vault result.
- `knowledge.task.prepare`: returned synthetic untrusted context and prepared
  `ktask_isolated_001`.
- `knowledge.task.start`: returned non-retryable
  `tool_isolation_unavailable`; task became failed while `agentId` and `turnId`
  stayed null. No provider execution was created.

### Disable/re-enable

- Disable changed plugin to `enabled=false`, `status=disabled`.
- While disabled, `knowledge.status` failed with `Plugin is not available` and
  the probe exited 1.
- Re-enable restored `enabled=true`, `status=running` exactly once.
- The persisted task and exact isolation error remained available.

### Daemon reconnect/restart

A durable checkpoint was written before stopping only the isolated daemon:
`knowledge-vault-isolated-candidate-checkpoint-20260920.md`.

After restart with the same candidate home and synthetic environment:

- health: OK;
- plugin: enabled/running;
- core: configured/ready;
- task identity: unchanged (`ktask_isolated_001`);
- status: failed;
- error code/message/retryable: preserved exactly;
- `agentId` and `turnId`: still null.

## Visual UI status

The isolated web root returned HTTP 200 and a Paseo HTML shell. The connected
Chrome automation extension was healthy and listed tabs, but navigation to
`http://127.0.0.1:16767/` timed out. No visual claim is made for:

- workspace or agent panel visibility;
- Command Center items;
- close/reopen panel identity;
- visual cleanup/restoration across disable/re-enable.

The synthetic workspace is ready for a manual or recovered-browser visual
test. No synthetic agent was created because doing so could involve provider
credential discovery and is outside this isolated no-credentials boundary.

## Durable artifacts

Raw artifacts are under
`docs/evidence/logs/knowledge-vault-isolated-candidate-20260920/`, including
the source delta, pre/post-restart status, health, plugin lists, and
disable/re-enable results.

## Acceptance impact

This evidence closes the reviewer-requested server/RPC lifecycle, restart
persistence, error persistence, enabled ordinary prepare/search/status, and
non-retryable zero-agent start checks. It does **not** close visual UI or
functional successful Knowledge Task execution. Merge remains gated on final
review and explicit resolution or reduced-scope acceptance for the missing
provider-agnostic no-tools execution boundary.
