import { describe, expect, it } from "vitest";
import {
  RunnerContract,
  RunnerRegistry,
  type RunnerAttestation,
  type RunnerAuditEvent,
  type RunnerBackend,
  type RunnerBackendResult,
  type RunnerRequest,
  type RunnerSource,
  type RunnerStartInput,
} from "../server/runner-contract.js";

const NOW = 1_000_000;
const HASH = "a".repeat(64);

const source: RunnerSource = {
  sourceId: "src_1",
  path: "10-Projects/synthetic.md",
  contentHash: HASH,
  text: "UNTRUSTED SOURCE TEXT",
};

const binding = {
  taskId: "task_1",
  workspaceId: "wks_1",
  provider: "codex/gpt-5.4",
  vaultScope: "work" as const,
  preparedAt: NOW,
  sources: [source],
};

const expected = { taskId: "task_1", workspaceId: "wks_1", provider: "codex/gpt-5.4" };

const limits = {
  maxInputChars: 10_000,
  maxOutputChars: 100,
  maxTimeoutMs: 1_000,
  maxConcurrency: 1,
  freshnessMs: 60_000,
};

const attestation: RunnerAttestation = {
  runnerId: "fake-runner",
  version: "1.0.0",
  attested: true,
  tools: "none",
  network: "none",
  maxInputChars: 10_000,
  maxOutputChars: 100,
  maxTimeoutMs: 1_000,
};

type Mode = "complete" | "ambiguous" | "hang" | "oversized" | "malformed" | "emptyCompletion" | "reject" | "throw";

function makeBackend(mode: Mode) {
  const calls: RunnerRequest[] = [];
  const backend: RunnerBackend = {
    async run(request, { signal }): Promise<RunnerBackendResult> {
      calls.push(request);
      switch (mode) {
        case "complete":
          return { kind: "completed", completionId: "cmp_1", output: "answer" };
        case "ambiguous":
          return { kind: "ambiguous" };
        case "oversized":
          return { kind: "completed", completionId: "cmp_1", output: "x".repeat(101) };
        case "malformed":
          return { kind: "completed", completionId: "cmp_1", output: 42 as never };
        case "emptyCompletion":
          return { kind: "completed", completionId: "", output: "answer" };
        case "reject":
          return { kind: "rejected", code: "agent_failed", message: "rejected" };
        case "throw":
          throw new Error("boom");
        case "hang":
          return new Promise<RunnerBackendResult>((resolve) => {
            signal.addEventListener("abort", () => resolve({ kind: "cancelled" }), { once: true });
          });
      }
    },
  };
  return { backend, calls };
}

function makeContract(backend: RunnerBackend, overrides: Partial<typeof limits> = {}) {
  const registry = new RunnerRegistry();
  registry.register(attestation, backend);
  const audit: RunnerAuditEvent[] = [];
  const contract = new RunnerContract({
    registry,
    limits: { ...limits, ...overrides },
    now: () => NOW,
    audit: (event) => audit.push(event),
  });
  return { contract, registry, audit };
}

const startInput = (overrides: Partial<RunnerStartInput> = {}): RunnerStartInput => ({
  binding,
  expected,
  prompt: "Summarize the note.",
  runnerId: "fake-runner",
  runnerVersion: "1.0.0",
  ...overrides,
});

async function waitFor(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("condition was not met");
}

describe("runner contract prototype", () => {
  it("denies by default when no attested runner is registered", async () => {
    const { calls } = makeBackend("complete");
    const audit: RunnerAuditEvent[] = [];
    const contract = new RunnerContract({
      registry: new RunnerRegistry(),
      limits,
      now: () => NOW,
      audit: (event) => audit.push(event),
    });

    const outcome = await contract.start(startInput());
    expect(outcome.status).toBe("failed");
    expect(outcome.status === "failed" ? outcome.error.code : null).toBe("tool_isolation_unavailable");
    expect(calls).toHaveLength(0);
    expect(audit).toHaveLength(0);
  });

  it("refuses to register an unattested, tool-capable, or networked runner", () => {
    const { backend } = makeBackend("complete");
    const registry = new RunnerRegistry();
    expect(() => registry.register({ ...attestation, attested: false } as never, backend)).toThrow();
    expect(() => registry.register({ ...attestation, tools: "native" } as never, backend)).toThrow();
    expect(() => registry.register({ ...attestation, network: "allowed" } as never, backend)).toThrow();
  });

  it("denies a runner whose attested version does not match the requested version", async () => {
    const { backend, calls } = makeBackend("complete");
    const { contract } = makeContract(backend);
    const outcome = await contract.start(startInput({ runnerVersion: "2.0.0" }));
    expect(outcome.status === "failed" ? outcome.error.code : null).toBe("tool_isolation_unavailable");
    expect(calls).toHaveLength(0);
  });

  it("rejects a task whose workspace or provider identity differs from the prepared binding", async () => {
    const { backend, calls } = makeBackend("complete");
    const { contract } = makeContract(backend);
    const outcome = await contract.start(startInput({ expected: { ...expected, workspaceId: "wks_other" } }));
    expect(outcome.status === "failed" ? outcome.error.code : null).toBe("task_identity_conflict");
    expect(calls).toHaveLength(0);
  });

  it("rejects personal scope before selecting a runner", async () => {
    const { backend, calls } = makeBackend("complete");
    const { contract } = makeContract(backend);
    const outcome = await contract.start(startInput({ binding: { ...binding, vaultScope: "personal" } }));
    expect(outcome.status === "failed" ? outcome.error.code : null).toBe("personal_access_unavailable");
    expect(calls).toHaveLength(0);
  });

  it("rejects an empty source manifest and a stale or malformed manifest", async () => {
    const { backend, calls } = makeBackend("complete");
    const { contract } = makeContract(backend);

    const empty = await contract.start(startInput({ binding: { ...binding, sources: [] } }));
    expect(empty.status === "failed" ? empty.error.code : null).toBe("needs_prepare");

    const stale = await contract.start(startInput({ binding: { ...binding, preparedAt: NOW - 60_001 } }));
    expect(stale.status === "failed" ? stale.error.code : null).toBe("source_invalidated");

    const badHash = await contract.start(startInput({
      binding: { ...binding, sources: [{ ...source, contentHash: "not-a-hash" }] },
    }));
    expect(badHash.status === "failed" ? badHash.error.code : null).toBe("source_invalidated");
    expect(calls).toHaveLength(0);
  });

  it("rejects an input above the configured bound before calling the backend", async () => {
    const { backend, calls } = makeBackend("complete");
    const { contract } = makeContract(backend, { maxInputChars: 5 });
    const outcome = await contract.start(startInput());
    expect(outcome.status === "failed" ? outcome.error.code : null).toBe("input_limit_exceeded");
    expect(calls).toHaveLength(0);
  });

  it("completes once with a verified completion identity and audits only non-sensitive fields", async () => {
    const { backend, calls } = makeBackend("complete");
    const { contract, audit } = makeContract(backend);

    const outcome = await contract.start(startInput());
    expect(outcome).toEqual({
      status: "completed",
      requestId: expect.any(String),
      completionId: "cmp_1",
      output: "answer",
    });
    expect(calls).toHaveLength(1);
    expect(audit).toHaveLength(1);
    const auditText = JSON.stringify(audit);
    expect(auditText).not.toContain("UNTRUSTED SOURCE TEXT");
    expect(auditText).not.toContain("Summarize the note.");
    expect(auditText).not.toContain("answer");
    expect(audit[0]).toMatchObject({ status: "completed", sourceCount: 1, outputChars: 6 });
  });

  it("rejects oversized, malformed, and unidentified completions without retrying", async () => {
    const oversized = makeBackend("oversized");
    const oversizedOutcome = await makeContract(oversized.backend).contract.start(startInput());
    expect(oversizedOutcome.status === "failed" ? oversizedOutcome.error.code : null).toBe("output_limit_exceeded");
    expect(oversized.calls).toHaveLength(1);

    const malformed = makeBackend("malformed");
    const malformedOutcome = await makeContract(malformed.backend).contract.start(startInput());
    expect(malformedOutcome.status === "failed" ? malformedOutcome.error.code : null).toBe("invalid_output");
    expect(malformed.calls).toHaveLength(1);

    const unidentified = makeBackend("emptyCompletion");
    const unidentifiedOutcome = await makeContract(unidentified.backend).contract.start(startInput());
    expect(unidentifiedOutcome.status === "failed" ? unidentifiedOutcome.error.code : null).toBe("capture_incomplete");
    expect(unidentified.calls).toHaveLength(1);
  });

  it("treats an ambiguous submission as unknown and never replays it", async () => {
    const { backend, calls } = makeBackend("ambiguous");
    const { contract } = makeContract(backend);
    const outcome = await contract.start(startInput());
    expect(outcome.status).toBe("ambiguous");
    expect(outcome.status === "ambiguous" ? outcome.error.code : null).toBe("submission_unknown");
    expect(calls).toHaveLength(1);
  });

  it("times out once and does not retry", async () => {
    const { backend, calls } = makeBackend("hang");
    const { contract } = makeContract(backend, { maxTimeoutMs: 20 });
    const outcome = await contract.start(startInput({ timeoutMs: 20 }));
    expect(outcome.status).toBe("timeout");
    expect(outcome.status === "timeout" ? outcome.error.code : null).toBe("timeout");
    expect(calls).toHaveLength(1);
  });

  it("cancels an in-flight request and does not capture a completion", async () => {
    const { backend, calls } = makeBackend("hang");
    const { contract } = makeContract(backend);
    const pending = contract.start(startInput({ requestId: "req_cancel" }));
    await waitFor(() => calls.length === 1);
    expect(contract.cancel("req_cancel")).toBe(true);
    const outcome = await pending;
    expect(outcome.status).toBe("cancelled");
    expect(outcome.status === "cancelled" ? outcome.error.code : null).toBe("cancelled");
    expect(calls).toHaveLength(1);
    expect(contract.cancel("req_cancel")).toBe(false);
  });

  it("rejects a second concurrent request as busy without calling the backend", async () => {
    const { backend, calls } = makeBackend("hang");
    const { contract } = makeContract(backend);
    const first = contract.start(startInput({ requestId: "req_first" }));
    await waitFor(() => calls.length === 1);

    const second = await contract.start(startInput({ requestId: "req_second" }));
    expect(second.status === "failed" ? second.error.code : null).toBe("busy");
    expect(calls).toHaveLength(1);

    expect(contract.cancel("req_first")).toBe(true);
    await first;
  });

  it("reports backend rejection and crash as failures without retrying", async () => {
    const rejected = makeBackend("reject");
    const rejectedOutcome = await makeContract(rejected.backend).contract.start(startInput());
    expect(rejectedOutcome.status === "failed" ? rejectedOutcome.error.code : null).toBe("agent_failed");
    expect(rejected.calls).toHaveLength(1);

    const crashed = makeBackend("throw");
    const crashedOutcome = await makeContract(crashed.backend).contract.start(startInput());
    expect(crashedOutcome.status === "failed" ? crashedOutcome.error.code : null).toBe("agent_failed");
    expect(crashed.calls).toHaveLength(1);
  });
});
