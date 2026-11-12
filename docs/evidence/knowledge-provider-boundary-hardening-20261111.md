# Hosted Knowledge Runner Boundary Hardening — 2026-11-11

## Scope and identity

Base: `6e17576` (PRs #46–#50 merged). Branch: `feat/knowledge-provider-boundary-hardening`. This is an uncommitted, offline-only change to the provider prototype. Neither `knowledge.task.start` nor the installed plugin is changed or reloaded.

## Changes

- Pin the production request to the exact HTTPS OpenCode Go API base (`https://opencode.ai/zen/go/v1`). Reject other hosts, ports, paths, URL credentials, queries, and fragments before transport.
- Refuse redirects; cancel unused non-success bodies. Tests route requests to fake loopback servers through a trusted injected transport, not a production localhost bypass.
- Limit response consumption to 65,536 decoded bytes before JSON parsing, including irrelevant JSON fields. Cancel the reader on overflow; Content-Length is only an early rejection hint.
- Validate the completion schema using the existing Zod dependency. Reject malformed JSON/envelopes, blank text, non-text output, tool_calls, legacy function_call, explicit refusals, and unsupported/truncated finish reasons.
- Bound serialized request characters and UTF-8 bytes (65,536 hard ceiling), source count (64), positive integer configuration, token budget (4,096), and deadline (120 seconds hard ceiling).
- Preserve the deadline during body reads and refuse success after cancellation or expiry, including late transport results.
- Snapshot configuration; retain generic errors that do not include credentials or source text.
- Correct the test server so declared Location headers and malformed raw bodies are actually emitted.

## Verification

Using ARM64 Node v22.23.4:

- Regression RED: **6 failed / 51 passed** after adding the transport seam and repairing the fixture, before boundary fixes.
- Final typecheck: **PASS**.
- Final Vitest: **63 passed / 63 total**, four test files.
- `git diff --check`: **PASS**.

Tests use synthetic text, fake keys, local HTTP servers and injected Web Streams. They verify redirect destination receives zero requests, rejection occurs before transport, byte-overflow cancels streaming consumption, oversized Content-Length cancels an unread body, UTF-8 accounting, malformed/truncated/blank/legacy-call rejection, body-read timeout, pre-aborted requests, and late cancellation/timeout.

No real OpenCode request, credential export/refresh, personal-vault read, installed-plugin reload, daemon restart, commit, merge, or deployment was performed.

## Remaining gates

This module is trusted in-process code, not an OS filesystem/network sandbox. Destination pinning protects this HTTP call; it does not establish host-wide egress restrictions. It does not attest a network-free runner. The current offline RunnerRegistry explicitly requires network=none and therefore cannot honestly register this hosted backend without a separately reviewed hosted policy.

Hosted task integration still needs a verified completion identity and capture path, explicit disclosure policy, source/identity freshness checks, concurrency and no-replay behavior, independent review, and deployment approval. OAuth refresh is not implemented; expired credentials must fail closed. A fresh physical-iPhone acceptance test remains separate from desktop-browser evidence.

The installed plugin remains at `6e17576` with Start intentionally fail-closed.

## Independent review

Read-only reviewer Hubble approved the bounded hardening with no remaining HIGH/blocker, explicitly not approval to enable hosted execution. Addressed both non-blocking corrections: clarify ambient in-process permissions in the module comment, and explicitly test raw inputs that fit while serialized framing exceeds the budget.
