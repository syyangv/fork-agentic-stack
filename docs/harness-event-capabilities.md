# Harness event capabilities

Phase 4 records only signals supplied by a harness's native hook surface. A
dash means **not observed**; agentic-stack does not infer it from nearby
activity.

| Adapter | User prompt / task start | Pre-tool | Post-tool | Feedback | Subagent start | Finalize |
|---|---|---|---|---|---|---|
| Claude Code | `UserPromptSubmit` | `PreToolUse` | `PostToolUse` | — | `SubagentStart` | `Stop` |
| Gemini CLI | `BeforeAgent` | `BeforeTool` | `AfterTool` | — | — | `SessionEnd` |
| GitHub Copilot CLI | `userPromptSubmitted` | `preToolUse` | `postToolUse` | — | — | `sessionEnd` |
| Pi Coding Agent | `before_agent_start` | — | `tool_result` | — | — | `session_shutdown` |
| Antigravity | — | — | — | — | — | — |
| Codex | — | — | — | — | — | — |
| Cursor | — | — | — | — | — | — |
| Hermes | — | — | — | — | — | — |
| OpenClaw | — | — | — | — | — | — |
| OpenCode | — | — | — | — | — | — |
| Standalone Python | — | — | — | — | — | — |
| Windsurf | — | — | — | — | — | — |

The executable matrix is `CAPABILITIES` in
`.agent/harness/hooks/orchestration_event.py`; tests require one row for every
shipped adapter. Feedback is false for every adapter because none of the
installed adapters exposes an unambiguous user-feedback hook.

## Normalized event mapping

| Native signal | EventEnvelope type | Stored content |
|---|---|---|
| user prompt | `task.started` | content-free intent marker and source signal |
| pre-tool | `tool.started` | tool name and bounded input summary |
| post-tool | `tool.completed` | tool name, bounded input/output summaries, and coarse error code |
| feedback | `feedback.recorded` | polarity, magnitude, explicit channel, bounded rationale |
| subagent start | `subagent.started` | agent type and bounded task description |
| Stop / SessionEnd | `task.completed` | status and source signal |

Native hooks validate that a prompt exists but persist only the fixed
`user request received` intent marker; prompt text is never hashed, summarized
heuristically, or retained. Raw prompts, environments, complete file bodies,
complete tool payloads, model reasoning, and unbounded stdout are never
forwarded. The shared contract redactor runs again when the immutable
`EventEnvelope` is built. On first worker access after upgrade, valid legacy
hook events are rewritten with the content-free marker and a newly canonical
event ID while retaining their idempotency key. Malformed legacy files are
moved into an owner-only quarantine and are never delivered.

## Correlation and shutdown behavior

Task start creates a stable run ID and a task-start event ID. Later native
events for that harness/session use both identifiers; existing episodic tool
records receive `orchestration_event_id` and `orchestration_run_id`. The
correlation file contains only these identifiers, a finalization marker, and
the content-free intent marker. Legacy correlation intents are rewritten
before reuse. Locally accepted finalization clears correlation only when the
stored run ID still matches; a timed-out finalization stays retryable but
cannot block the next prompt.

Hooks atomically enqueue an event under private runtime state and start a
detached, serialized worker; they do not wait for MemOS startup or retrieval.
Repository identity and revision Git probes share one 500 ms enrichment
budget, and correlation locks have a 250 ms ceiling, keeping normal
pre-enqueue work below the host's three-second deadline even when Git stalls.
The worker submits bounded batches and moves only locally accepted events to
the delivered spool. Its structured health file exposes provider failures and
pending counts. Finalization retains a three-second host-hook budget, but its
normal path is only a filesystem enqueue. Malformed input or a missing active
run skips/degrades capture while exiting zero and emitting no Stop-hook stdout,
so memory cannot block harness shutdown.

Runtime migration repairs orchestration, pending, delivered, quarantine,
correlation, health, and lock artifacts to owner-only POSIX modes (`0700` for
directories and `0600` for files). Windows inherits repository ACLs;
equivalent owner-only ACL enforcement remains platform-dependent.

## Explicit lifecycle commands

For a harness without native task-start or feedback hooks, submit the signal
explicitly. Reuse the same `session_id` for the lifecycle:

```bash
printf '%s' '{"session_id":"manual-1","prompt":"Refactor the parser"}' \
  | python3 .agent/harness/hooks/orchestration_event.py \
      --harness codex --signal user_prompt --explicit

printf '%s' '{"session_id":"manual-1","polarity":"positive","magnitude":1,"rationale":"The fix worked"}' \
  | python3 .agent/harness/hooks/orchestration_event.py \
      --harness codex --signal feedback --explicit

printf '%s' '{"session_id":"manual-1","status":"completed"}' \
  | python3 .agent/harness/hooks/orchestration_event.py \
      --harness codex --signal finalize --explicit --timeout 1.5
```

These commands are explicit operator observations, not synthesized tool data.
