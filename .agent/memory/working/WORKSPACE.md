# Workspace (live task state)

> Replace this template on your first real task. The dream cycle auto-archives
> this file after 2 days of inactivity — don't keep long-lived notes here.

## Current task
- Implement and activate a project-bound Opus-only MemOS full-evolution pilot for `/Users/syang/projects/raycast-custom-commands` without a local model or separate API key.

## Open files
- `.agent/memory/orchestration/host_evolution.py`
- `.agent/memory/orchestration/memos_factory.py`
- `.agent/memory/orchestration/memos_runtime.py`
- `.agent/memory/orchestration/evolution_eval.py`
- `tests/test_phase8_host_evolution.py`
- `tests/test_phase8_evolution_pilot.py`
- `tests/test_phase8_evolution_eval.py`

## Active hypotheses
- Exact MemOS 2.0.10 prompt fingerprints plus structural sanitization form a preventive privacy boundary for native host completions.
- Claude CLI OAuth with `--safe-mode --tools ''`, empty MCP config, stdin-only payload, and an empty owner-only cwd provides the required no-tools Opus surface.
- Model output remains probationary because translation always stages candidates and only explicit human review can accept them.

## Checkpoints
- [x] Merged fail-closed Phase 8 preflight as PR 10 / `951175fa5502aa0787b69d6b81f8379b99336424`.
- [x] Replaced dual GPT/Opus pilot schema with project-bound Opus-only v2.
- [x] Added exact prompt fingerprinting, nested sanitization, quotas/cache, metadata-only success/failure audits, and concrete factory wiring.
- [x] Added project/protocol/injection-bound held-out accounting: 20 paired tasks, canonical executed-test evidence, no success regression, and >=10% median improvement.
- [x] Independent senior review approved after closing plugin inventory, evaluation provenance, quota rollover, and migration-atomicity findings.
- [x] Full verification: 362 pytest passed with 115 subtests.
- [x] Attested the exact cached MemOS 2.0.10 tree (5,212 files) and prepared owner-only pilot runtime/config for project `5efa1310d8759984`.
- [x] Created and restored a real whole-runtime baseline backup; real MemOS `core.health` passed with host Opus and local embeddings available.
- [ ] Real Opus completion smoke was explicitly approved. The narrow macOS credential-routing environment reaches authenticated Claude, but the account returned HTTP 429 session-limit before inference; no model output was produced.
- [ ] Collect 20 paired held-out tasks; no automatic authority changes.

## Next step
Commit, push, and open the activation PR without merging, recording the authenticated HTTP 429 smoke as the remaining rollout blocker until the Claude session limit resets. The pilot repository's pre-existing dirty files remain untouched.
