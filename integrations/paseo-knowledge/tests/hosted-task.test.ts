import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, expect, it, vi } from "vitest";
import { ProviderRunner, type ProviderRunResult } from "../server/provider-runner.js";
import { HostedTaskCoordinator, type HostedDraftCompletion } from "../server/hosted-task.js";
import { digest, prepareHostedInput, HostedPreparationRegistry, type HostedDisclosurePolicy, type HostedTaskInput } from "../server/hosted-task-policy.js";
import type { HostedExecutionLedger, HostedLedgerOutcome, HostedReservation } from "../server/hosted-execution-ledger.js";

const now = 10_000;
const policy: HostedDisclosurePolicy = {
  policyId: "synthetic-hosted-v1", version: "1", endpoint: "https://opencode.ai/zen/go/v1",
  model: "synthetic-model", vaultScope: "work", tools: "none", network: "opencode-provider-api",
  disclosure: "work-context-to-opencode-and-upstream", maxInputChars: 10_000,
  maxOutputChars: 1_000, maxTokens: 1_024, timeoutMs: 1_000, freshnessMs: 1_000,
};
function input(taskId = "task_1"): HostedTaskInput {
  return { binding: { taskId, workspaceId: "workspace_1", provider: "opencode-go/synthetic-model",
    vaultScope: "work", preparedAt: now,
    sources: [{ sourceId: "source_1", path: "10-Projects/private-path.md", text: "SYNTHETIC CONTEXT", contentHash: digest("SYNTHETIC CONTEXT"), excerptHash: digest("SYNTHETIC CONTEXT") }],
  }, expected: { taskId, workspaceId: "workspace_1", provider: "opencode-go/synthetic-model" }, prompt: "Summarize synthetic context" };
}
class MemoryLedger implements HostedExecutionLedger {
  public readonly records = new Map<string, { reservation: HostedReservation; outcome?: HostedLedgerOutcome }>();
  public async reserve(reservation: HostedReservation) {
    if (this.records.has(reservation.taskId)) return false;
    this.records.set(reservation.taskId, { reservation });
    return true;
  }
  public async finish(reservation: HostedReservation, outcome: HostedLedgerOutcome) {
    const current = this.records.get(reservation.taskId);
    if (current?.reservation.executionId !== reservation.executionId) throw new Error("Wrong execution");
    this.records.set(reservation.taskId, { reservation, outcome });
  }
}
function harness(options: { policy?: HostedDisclosurePolicy | null; ledger?: HostedExecutionLedger; now?: () => number } = {}) {
  const run = vi.fn(async (): Promise<ProviderRunResult> => ({ status: "completed", output: "SYNTHETIC DRAFT", completionId: "completion_1", model: "synthetic-model" }));
  const createProvider = vi.fn(() => ({ run }));
  const capture = vi.fn(async (completion: HostedDraftCompletion, _signal: AbortSignal) => ({ draftId: "draft_1", executionId: completion.execution.executionId, outputDigest: completion.outputDigest }));
  const ledger = options.ledger ?? new MemoryLedger();
  const preparations = new HostedPreparationRegistry();
  const checkSources = vi.fn(async () => true);
  const coordinator = new HostedTaskCoordinator({ preparations, checkSources, policy: options.policy === null ? undefined : options.policy ?? policy, ledger, createProvider,
    drafts: { capture }, now: options.now ?? (() => now) });
  const start = (candidate: HostedTaskInput) => {
    let approvalId: string | undefined;
    try { approvalId = preparations.approve(options.policy ?? policy, candidate, (options.now ?? (() => now))()); } catch { /* Invalid preparations are rejected by start. */ }
    return coordinator.start({ ...candidate, approvalId });
  };
  return { coordinator, start, preparations, checkSources, ledger, run, createProvider, capture };
}
const errorCode = (result: Awaited<ReturnType<HostedTaskCoordinator["start"]>>) => result.status === "completed" ? undefined : result.error.code;

// No credential loader, live SDK, plugin registration or production path is used here.
describe("offline hosted Knowledge Task integration", () => {
  it("denies missing or incompatible disclosure policy before creating a provider", async () => {
    for (const configured of [null, { ...policy, network: "none" } as unknown as HostedDisclosurePolicy, { ...policy, endpoint: "https://other.example" } as unknown as HostedDisclosurePolicy]) {
      const test = harness({ policy: configured });
      expect(errorCode(await test.start(input()))).toBe("not_configured");
      expect(test.createProvider).not.toHaveBeenCalled();
      expect(test.capture).not.toHaveBeenCalled();
    }
  });
  it("requires a trusted approval binding the exact payload and policy", async () => {
    const test = harness();
    expect(errorCode(await test.coordinator.start(input()))).toBe("approval_required");
    const approvalId = test.preparations.approve(policy, input(), now);
    expect(errorCode(await test.coordinator.start({ ...input(), approvalId, prompt: "changed prompt" }))).toBe("approval_required");
    expect(errorCode(await test.coordinator.start({ ...input(), approvalId: "forged" }))).toBe("approval_required");
    expect(test.createProvider).not.toHaveBeenCalled();
    expect((await test.coordinator.start({ ...input(), approvalId })).status).toBe("completed");
    expect(errorCode(await test.coordinator.start({ ...input(), approvalId }))).toBe("approval_required");
  });
  it("binds provider completion to execution and captures only a private draft", async () => {
    const test = harness();
    const result = await test.start(input());
    expect(result).toMatchObject({ status: "completed", completionId: "completion_1", draftId: "draft_1" });
    expect(test.run).toHaveBeenCalledOnce();
    const sent = test.run.mock.calls[0] as unknown as [{ sources: { path: string }[]; sessionId: string }];
    expect(sent[0].sources[0]?.path).toBe("source_1");
    expect(JSON.stringify(sent)).not.toContain("private-path.md");
    const captured = test.capture.mock.calls[0]![0];
    expect(captured.execution.inputDigest).toMatch(/^[a-f0-9]{64}$/);
    expect(captured.outputDigest).toBe(digest(captured.output));
    expect(captured.execution.executionId).toBe(sent[0].sessionId);
    expect(captured.sources[0]?.path).toBe("10-Projects/private-path.md");
    const stored = JSON.stringify((test.ledger as MemoryLedger).records.get("task_1"));
    expect(stored).not.toContain("SYNTHETIC");
    expect(stored).not.toContain("private-path.md");
    expect(stored).not.toContain("Summarize");
  });
  it("never resubmits a task across coordinator recreation", async () => {
    const ledger = new MemoryLedger();
    const first = harness({ ledger });
    await first.start(input());
    const restarted = harness({ ledger });
    expect(errorCode(await restarted.start(input()))).toBe("already_submitted");
    expect(restarted.createProvider).not.toHaveBeenCalled();
  });
  it("denies personal scope, identity mismatch, stale/hash-changed/excluded sources before submission", async () => {
    const base = input();
    const variants: [HostedTaskInput, string][] = [
      [{ ...base, binding: { ...base.binding, vaultScope: "personal" } }, "personal_access_unavailable"],
      [{ ...base, expected: { ...base.expected, workspaceId: "other" } }, "task_identity_conflict"],
      [{ ...base, binding: { ...base.binding, provider: "other/model" }, expected: { ...base.expected, provider: "other/model" } }, "task_identity_conflict"],
      [{ ...base, binding: { ...base.binding, preparedAt: 0 } }, "source_invalidated"],
      [{ ...base, binding: { ...base.binding, preparedAt: now + 1 } }, "source_invalidated"],
      [{ ...base, binding: { ...base.binding, sources: [{ ...base.binding.sources[0]!, text: "altered" }] } }, "source_invalidated"],
      [{ ...base, excludedSourceIds: ["source_1"] }, "source_invalidated"],
      [{ ...base, binding: { ...base.binding, sources: [base.binding.sources[0]!, base.binding.sources[0]!] } }, "source_invalidated"],
    ];
    for (const [candidate, code] of variants) {
      const test = harness();
      expect(errorCode(await test.start(candidate))).toBe(code);
      expect(test.createProvider).not.toHaveBeenCalled();
    }
  });
  it("normalizes approved source hashes and rejects paths the private sink cannot capture", async () => {
    const base = input();
    const source = base.binding.sources[0]!;
    const uppercase = { ...base, binding: { ...base.binding, sources: [{ ...source,
      contentHash: source.contentHash.toUpperCase(), excerptHash: source.excerptHash.toUpperCase() }] } };
    const accepted = harness();
    expect((await accepted.start(uppercase)).status).toBe("completed");
    expect(accepted.capture.mock.calls[0]![0].sources[0]?.contentHash).toBe(source.contentHash);
    for (const path of ["bad\npath.md", "../outside.md", "/absolute.md", "C:\\outside.md", "notes/../outside.md"]) {
      const rejected = harness();
      expect(errorCode(await rejected.start({ ...base, binding: { ...base.binding, sources: [{ ...source, path }] } }))).toBe("needs_prepare");
      expect(rejected.createProvider).not.toHaveBeenCalled();
    }
  });
  it("enforces bounded input before reserving or contacting a provider", async () => {
    const ledger = new MemoryLedger();
    const test = harness({ policy: { ...policy, maxInputChars: 10 }, ledger });
    expect(errorCode(await test.start(input()))).toBe("input_limit_exceeded");
    expect(ledger.records.size).toBe(0);
    expect(test.createProvider).not.toHaveBeenCalled();
  });
  it("does not submit when reservation persistence fails", async () => {
    const ledger = new MemoryLedger();
    ledger.reserve = async () => { throw new Error("private filesystem details"); };
    const test = harness({ ledger });
    expect(await test.start(input())).toMatchObject({ status: "failed", error: { code: "storage_unavailable", retryable: false } });
    expect(test.createProvider).not.toHaveBeenCalled();
  });
  it("reserves before provider creation and refuses replays after a crash or HTTP failure", async () => {
    for (const mode of ["throw", "reject"] as const) {
      const ledger = new MemoryLedger();
      const test = harness({ ledger });
      test.run.mockImplementation(async () => {
        expect(ledger.records.has("task_1")).toBe(true);
        if (mode === "throw") throw new Error("provider result unknown");
        return { status: "failed", error: { code: "provider_error", message: "SENSITIVE PROVIDER ERROR", retryable: true } };
      });
      const result = await test.start(input());
      expect(errorCode(result)).toBe(mode === "throw" ? "submission_unknown" : "provider_error");
      expect(JSON.stringify(result)).not.toContain("SENSITIVE");
      expect(errorCode(await test.start(input()))).toBe("already_submitted");
      expect(test.run).toHaveBeenCalledOnce();
      expect(test.capture).not.toHaveBeenCalled();
    }
  });
  it("refuses missing completion identity, wrong model, malformed ID and oversized output", async () => {
    for (const completion of [
      { output: "text", model: policy.model }, { output: "text", completionId: "completion_1", model: "wrong-model" },
      { output: "text", completionId: "../bad", model: policy.model },
      { output: "x".repeat(1_001), completionId: "completion_1", model: policy.model },
    ]) {
      const test = harness();
      test.run.mockResolvedValue({ status: "completed", ...completion });
      expect((await test.start(input())).status).toBe("failed");
      expect(test.capture).not.toHaveBeenCalled();
    }
  });
  it("never captures cancellation or a late completion and keeps the provider slot occupied", async () => {
    const test = harness();
    let resolve!: (result: ProviderRunResult) => void;
    let began!: () => void;
    const started = new Promise<void>((done) => { began = done; });
    test.run.mockImplementation(() => { began(); return new Promise((done) => { resolve = done; }); });
    const pending = test.start(input());
    await started;
    expect(test.coordinator.cancel("task_1")).toBe(true);
    expect(errorCode(await pending)).toBe("submission_unknown");
    expect(errorCode(await test.start(input("task_2")))).toBe("busy");
    resolve({ status: "completed", output: "late", completionId: "late", model: policy.model });
    await new Promise((done) => setImmediate(done));
    expect(test.capture).not.toHaveBeenCalled();
    expect(errorCode(await test.start(input()))).toBe("already_submitted");
  });
  it("times out without automatic replay or draft capture", async () => {
    const test = harness({ policy: { ...policy, timeoutMs: 10 } });
    test.run.mockImplementation(() => new Promise(() => {}));
    expect(errorCode(await test.start(input()))).toBe("submission_unknown");
    expect(test.run).toHaveBeenCalledOnce();
    expect(test.capture).not.toHaveBeenCalled();
  });
  it("bounds stalled reservation, source checks, and final persistence without disclosing or replaying", async () => {
    const stalledLedger = new MemoryLedger();
    stalledLedger.reserve = () => new Promise(() => {});
    const waiting = harness({ ledger: stalledLedger, policy: { ...policy, timeoutMs: 10 } });
    expect(await waiting.start(input())).toMatchObject({ status: "timeout", error: { code: "timeout" } });
    expect(waiting.createProvider).not.toHaveBeenCalled();
    expect(errorCode(await waiting.start(input("task_2")))).toBe("busy");
    const checking = harness({ policy: { ...policy, timeoutMs: 10 } });
    checking.checkSources.mockImplementation(() => new Promise(() => {}));
    expect(await checking.start(input())).toMatchObject({ status: "timeout", error: { code: "timeout" } });
    expect(checking.createProvider).not.toHaveBeenCalled();
    const finalLedger = new MemoryLedger();
    finalLedger.finish = () => new Promise(() => {});
    const finalizing = harness({ ledger: finalLedger, policy: { ...policy, timeoutMs: 10 } });
    expect(errorCode(await finalizing.start(input()))).toBe("capture_incomplete");
    expect(finalizing.run).toHaveBeenCalledOnce();
  });
  it("rejects invalidated current sources before credentials/provider creation", async () => {
    const test = harness();
    test.checkSources.mockResolvedValue(false);
    expect(errorCode(await test.start(input()))).toBe("source_invalidated");
    expect(test.createProvider).not.toHaveBeenCalled();
    expect(test.capture).not.toHaveBeenCalled();
  });
  it("does not acquire credentials when source verification itself outlives preparation freshness", async () => {
    let clock = now;
    const test = harness({ now: () => clock });
    test.checkSources.mockImplementation(async () => { clock += 2_000; return true; });
    expect(errorCode(await test.start(input()))).toBe("source_invalidated");
    expect(test.createProvider).not.toHaveBeenCalled();
  });
  it("does not capture if work sources change while the provider is running", async () => {
    const test = harness();
    let current = true;
    test.checkSources.mockImplementation(async () => current);
    test.run.mockImplementation(async () => {
      current = false;
      return { status: "completed", output: "now stale", completionId: "completion_1", model: policy.model };
    });
    expect(errorCode(await test.start(input()))).toBe("source_invalidated");
    expect(test.capture).not.toHaveBeenCalled();
    expect(errorCode(await test.start(input()))).toBe("already_submitted");
    expect(test.run).toHaveBeenCalledOnce();
  });
  it("snapshots approved context before asynchronous reservation", async () => {
    const candidate = input();
    const ledger = new MemoryLedger();
    const reserve = ledger.reserve.bind(ledger);
    ledger.reserve = async (record) => {
      (candidate.binding.sources[0] as { text: string }).text = "TAMPERED AFTER APPROVAL";
      return reserve(record);
    };
    const test = harness({ ledger });
    expect((await test.start(candidate)).status).toBe("completed");
    expect(JSON.stringify(test.run.mock.calls)).not.toContain("TAMPERED");
    expect(test.capture.mock.calls[0]![0].sources[0]?.text).toBe("SYNTHETIC CONTEXT");
  });
  it("treats an in-flight private draft timeout as incomplete and never captures twice", async () => {
    const test = harness({ policy: { ...policy, timeoutMs: 10 } });
    test.capture.mockImplementation(() => new Promise(() => {}));
    expect(errorCode(await test.start(input()))).toBe("capture_incomplete");
    expect(test.capture).toHaveBeenCalledOnce();
    expect(errorCode(await test.start(input("task_2")))).toBe("busy");
  });
  it("does not reserve a pre-aborted request", async () => {
    const test = harness();
    const controller = new AbortController(); controller.abort();
    expect(errorCode(await test.start({ ...input(), signal: controller.signal }))).toBe("cancelled");
    expect((test.ledger as MemoryLedger).records.size).toBe(0);
  });
  it("rechecks freshness after asynchronous provider acquisition before disclosure", async () => {
    let clock = now;
    const test = harness({ now: () => clock });
    test.createProvider.mockImplementation(() => { clock += 2_000; return { run: test.run }; });
    expect(errorCode(await test.start(input()))).toBe("source_invalidated");
    expect(test.run).not.toHaveBeenCalled();
  });
  it("does not claim success when private draft capture fails or returns a mismatched receipt", async () => {
    for (const mode of ["throw", "wrong-execution", "wrong-digest"] as const) {
      const test = harness();
      test.capture.mockImplementation(async (completion) => {
        if (mode === "throw") throw new Error("private sink error");
        return { draftId: "draft_1", executionId: mode === "wrong-execution" ? "wrong" : completion.execution.executionId,
          outputDigest: mode === "wrong-digest" ? digest("wrong") : completion.outputDigest };
      });
      expect(errorCode(await test.start(input()))).toBe("capture_incomplete");
      expect(errorCode(await test.start(input()))).toBe("already_submitted");
      expect(test.capture).toHaveBeenCalledOnce();
    }
  });
  it("does not claim success when completion cannot be durably recorded", async () => {
    const ledger = new MemoryLedger();
    ledger.finish = async () => { throw new Error("disk full"); };
    const test = harness({ ledger });
    expect(errorCode(await test.start(input()))).toBe("capture_incomplete");
    expect(errorCode(await test.start(input()))).toBe("already_submitted");
  });
  it("runs the tool-free provider HTTP boundary through the draft seam using synthetic data only", async () => {
    let sent = "";
    const server = createServer((req, res) => { req.on("data", (chunk) => { sent += chunk; }); req.on("end", () => {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ id: "http_completion_1", model: policy.model, choices: [{ finish_reason: "stop", message: { role: "assistant", content: "LOCAL SYNTHETIC DRAFT" } }] }));
    }); });
    await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
    try {
      const ledger = new MemoryLedger();
      const captured: HostedDraftCompletion[] = [];
      const preparations = new HostedPreparationRegistry();
      const coordinator = new HostedTaskCoordinator({ policy, ledger, preparations, checkSources: async () => true, now: () => now,
        createProvider: () => new ProviderRunner({ endpoint: policy.endpoint, model: policy.model, apiKey: "fake-test-key",
          maxInputChars: policy.maxInputChars, maxOutputChars: policy.maxOutputChars, timeoutMs: policy.timeoutMs },
        (url, init) => fetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}${new URL(String(url)).pathname}`, init)),
        drafts: { capture: async (completion) => { captured.push(completion); return { draftId: "draft_http", executionId: completion.execution.executionId, outputDigest: completion.outputDigest }; } },
      });
      expect(await coordinator.start({ ...input(), approvalId: preparations.approve(policy, input(), now) })).toMatchObject({ status: "completed", completionId: "http_completion_1", draftId: "draft_http" });
      expect(captured[0]?.output).toBe("LOCAL SYNTHETIC DRAFT");
      expect(sent).not.toContain("private-path.md");
      expect(JSON.parse(sent)).not.toHaveProperty("tools");
    } finally {
      await new Promise<void>((done) => server.close(() => done()));
    }
  });
});

describe("hosted collection pre-validation", () => {
  it("rejects limit+1 collections before traversing either collection", () => {
    for (const field of ["sources", "excludedSourceIds"] as const) {
      const touched = vi.fn(() => { throw new Error("Collection element traversed"); });
      const oversized = new Array(65);
      Object.defineProperty(oversized, 0, { get: touched });
      // Even a valid-sized sibling must remain unparsed on count rejection.
      const sibling = new Array(1);
      Object.defineProperty(sibling, 0, { get: touched });
      const base = input();
      const candidate = field === "sources" ? { ...base, binding: { ...base.binding, sources: oversized }, excludedSourceIds: sibling }
        : { ...base, binding: { ...base.binding, sources: sibling }, excludedSourceIds: oversized };
      expect(prepareHostedInput(policy, candidate, now)).toEqual({ error: "needs_prepare" });
      expect(touched).not.toHaveBeenCalled();
    }
  });
  it("rejects malformed collection envelopes safely", () => {
    const base = input();
    const malformed: unknown[] = [null, undefined, 1, "input", {}, { binding: null }];
    for (const value of [null, {}, "sources", { length: 64 }]) {
      malformed.push({ ...base, binding: { ...base.binding, sources: value } }, { ...base, excludedSourceIds: value });
    }
    for (const candidate of malformed) {
      expect(prepareHostedInput(policy, candidate as HostedTaskInput, now)).toEqual({ error: "needs_prepare" });
    }
  });
  it("preserves exact-boundary collections and optional exclusions", () => {
    const base = input();
    const sources = Array.from({ length: 64 }, (_, index) => ({ ...base.binding.sources[0]!, sourceId: `source_${index}` }));
    const candidate = { ...base, binding: { ...base.binding, sources }, excludedSourceIds: Array.from({ length: 64 }, (_, index) => `excluded_${index}`) };
    const checked = prepareHostedInput({ ...policy, maxInputChars: 65_536 }, candidate, now);
    expect(checked).toEqual({ input: candidate });
    expect(prepareHostedInput(policy, base, now)).toEqual({ input: { ...base, excludedSourceIds: [] } });
  });
});
