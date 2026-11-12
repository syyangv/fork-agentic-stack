import type { PaseoApi } from "@getpaseo/client";
import type { PluginServerContext } from "@getpaseo/plugin/server";
import { describe, expect, it, vi } from "vitest";
import { KnowledgeTaskService } from "../server/service.js";
import { HostedKnowledgeApplication } from "../server/hosted-application.js";
import { StoredHostedPreparationSource } from "../server/hosted-preparation.js";
import { registerHostedHandlers } from "../server/hosted-rpc.js";
import { digest, type HostedDisclosurePolicy } from "../server/hosted-task-policy.js";
import type { HostedExecutionLedger } from "../server/hosted-execution-ledger.js";
import type { ProviderRunResult } from "../server/provider-runner.js";
import { HostedExecuteRpc, HostedPreviewRpc } from "../shared/hosted-contracts.js";

const policy: HostedDisclosurePolicy = { policyId: "test-policy", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "synthetic-model",
  vaultScope: "work", tools: "none", network: "opencode-provider-api", disclosure: "work-context-to-opencode-and-upstream",
  maxInputChars: 10_000, maxOutputChars: 1_000, maxTokens: 1_024, timeoutMs: 300, freshnessMs: 30_000 };
const ref = { source_id: "core_1", vault_id: "work", note_id: "test-note", path: "10-Projects/test.md", heading: null,
  line_start: 1, line_end: 2, content_hash: digest("full-file") };
async function fixture(overrides: Partial<HostedDisclosurePolicy> = {}) {
  const service = new KnowledgeTaskService({ core: { status: async () => ({}), search: async () => ({}), proposeNote: async () => ({}),
    buildContext: async (request) => ({ task_id: request.taskId, status: "ok", context: "source", personal_context_used: false, source_content_untrusted: true,
      truncated: false, source_manifest: [ref], snippets: [{ source_ref: ref, content: "APPROVED SOURCE", source_content_untrusted: true, truncated: false }] }) } });
  await service.prepare({ taskId: "task_1", workspaceId: "workspace_1", provider: "opencode-go/synthetic-model", query: "synthetic",
    vaultScope: "work", maxSnippets: 8, maxChars: 12_000, includeRelated: false });
  const readSource = vi.fn(async () => ({ success: true, source_ref: ref, content: "APPROVED SOURCE", truncated: false, source_content_untrusted: true }));
  const ledger: HostedExecutionLedger = { reserve: vi.fn(async () => true), finish: vi.fn(async () => {}) };
  const run = vi.fn(async (): Promise<ProviderRunResult> => ({ status: "completed", output: "PRIVATE DRAFT", completionId: "completion_1", model: policy.model }));
  const createProvider = vi.fn(() => ({ run }));
  const capture = vi.fn(async (completion) => ({ draftId: "draft_1", executionId: completion.execution.executionId, outputDigest: completion.outputDigest }));
  const review = { list: vi.fn(async () => ({ drafts: [], unreadableCount: 0 })), read: vi.fn(async () => { throw new Error("private read failure"); }), reject: vi.fn(async () => {}) };
  let clock = Date.now();
  const application = new HostedKnowledgeApplication({ policy: { ...policy, ...overrides }, preparations: new StoredHostedPreparationSource(service.store, readSource),
    ledger, createProvider, drafts: { capture }, review, now: () => clock });
  const refresh = vi.fn(async () => ({ id: "workspace_1", status: "active" }));
  const access = { workspaces: { ref: vi.fn(() => ({ refresh })) } } as unknown as Pick<PaseoApi, "workspaces">;
  const preview = () => application.preview({ taskId: "task_1", workspaceId: "workspace_1", prompt: "APPROVED PROMPT" }, access);
  return { application, service, access, refresh, readSource, ledger, run, createProvider, capture, review, preview, advance: (ms: number) => { clock += ms; } };
}
const executeInput = (preview: NonNullable<Awaited<ReturnType<HostedKnowledgeApplication["preview"]>>["preview"]>) => ({
  taskId: preview.taskId, workspaceId: preview.workspaceId, previewId: preview.previewId, challenge: preview.challenge, payloadDigest: preview.payloadDigest,
});
describe("hosted application daemon-session bridge", () => {
  it("serves exact disclosure preview without credentials/provider work then executes only that sealed snapshot", async () => {
    const f = await fixture();
    try {
      const { preview, error } = await f.preview();
      expect(error).toBeNull(); expect(preview?.sources[0]?.text).toBe("APPROVED SOURCE"); expect(preview?.prompt).toBe("APPROVED PROMPT");
      expect(preview?.policy.endpoint).toBe(policy.endpoint);
      expect(f.createProvider).not.toHaveBeenCalled(); expect(f.run).not.toHaveBeenCalled(); expect(f.ledger.reserve).not.toHaveBeenCalled();
      if (!preview) throw new Error("Missing preview");
      const result = await f.application.execute(executeInput(preview), f.access);
      expect(result.result?.status).toBe("completed");
      expect(f.run).toHaveBeenCalledOnce(); expect(f.capture).toHaveBeenCalledOnce();
      expect(await f.application.execute(executeInput(preview), f.access)).toMatchObject({ result: null, error: { code: "consent_required" } });
      expect(f.run).toHaveBeenCalledOnce();
    } finally { f.application.dispose(); f.service.dispose(); }
  });
  it("rejects forged challenge/digest and wrong workspace before creating a provider", async () => {
    const f = await fixture();
    try {
      const { preview } = await f.preview(); if (!preview) throw new Error("Missing preview");
      for (const change of [{ challenge: "forged" }, { payloadDigest: digest("forged") }, { taskId: "other" }, { workspaceId: "other" }]) {
        expect((await f.application.execute({ ...executeInput(preview), ...change }, f.access)).error).not.toBeNull();
      }
      expect(f.createProvider).not.toHaveBeenCalled();
    } finally { f.application.dispose(); f.service.dispose(); }
  });
  it("refuses expired/superseded previews and changed server preparation", async () => {
    const f = await fixture();
    try {
      const first = (await f.preview()).preview!; const second = (await f.preview()).preview!;
      expect((await f.application.execute(executeInput(first), f.access)).error?.code).toBe("consent_required");
      f.service.store.get("task_1")!.query = "changed on server";
      expect((await f.application.execute(executeInput(second), f.access)).error?.code).toBe("source_invalidated");
      const third = (await f.preview()).preview!; f.advance(31_000);
      expect((await f.application.execute(executeInput(third), f.access)).error?.code).toBe("consent_required");
      expect(f.createProvider).not.toHaveBeenCalled();
    } finally { f.application.dispose(); f.service.dispose(); }
  });
  it("validates workspace existence through the daemon before any stored context or draft access", async () => {
    const f = await fixture();
    try {
      f.refresh.mockResolvedValue({ id: "different-workspace", status: "active" });
      expect((await f.preview()).error?.code).toBe("workspace_unavailable");
      expect((await f.application.list("workspace_1", f.access)).error?.code).toBe("workspace_unavailable");
      expect(f.readSource).not.toHaveBeenCalled(); expect(f.review.list).not.toHaveBeenCalled();
    } finally { f.application.dispose(); f.service.dispose(); }
  });
  it("does not let returned preview objects mutate server-held consent data", async () => {
    const f = await fixture();
    try {
      const preview = (await f.preview()).preview!;
      preview.prompt = "MUTATED CLIENT PROMPT"; preview.sources[0]!.text = "MUTATED CLIENT SOURCE";
      expect((await f.application.execute(executeInput(preview), f.access)).result?.status).toBe("completed");
      expect(JSON.stringify(f.run.mock.calls)).not.toContain("MUTATED");
    } finally { f.application.dispose(); f.service.dispose(); }
  });
  it("cancels an approved in-flight execution without capturing its late completion", async () => {
    const f = await fixture();
    let complete!: (value: ProviderRunResult) => void; let began!: () => void;
    const started = new Promise<void>((done) => { began = done; });
    try {
      f.run.mockImplementation(() => { began(); return new Promise((done) => { complete = done; }); });
      const preview = (await f.preview()).preview!;
      const executing = f.application.execute(executeInput(preview), f.access); await started;
      f.refresh.mockClear().mockImplementation(() => new Promise(() => {}));
      expect(await f.application.cancel({ taskId: "task_1", workspaceId: "workspace_1" }, f.access)).toMatchObject({ cancelled: true });
      expect(f.refresh).not.toHaveBeenCalled();
      expect((await executing).result).toMatchObject({ status: "cancelled", error: { code: "submission_unknown" } });
      complete({ status: "completed", output: "late", completionId: "late", model: policy.model });
      await new Promise((done) => setImmediate(done)); expect(f.capture).not.toHaveBeenCalled();
    } finally { f.application.dispose(); f.service.dispose(); }
  });
  it("rejects client-provided source/binding fields at the RPC boundary and defaults all hosted RPCs to disabled", async () => {
    expect(HostedExecuteRpc.input.safeParse({ taskId: "task", workspaceId: "workspace", previewId: "id", challenge: "challenge", payloadDigest: digest("x"), binding: {} }).success).toBe(false);
    expect(HostedPreviewRpc.input.safeParse({ taskId: "task", workspaceId: "workspace", prompt: "prompt", sources: [{ text: "CLIENT SOURCE" }] }).success).toBe(false);
    const handlers = new Map<string, (input: unknown, context: unknown) => Promise<unknown> | unknown>();
    const server = { handle: vi.fn((rpc, handler) => { handlers.set(rpc.name, handler); }) } as unknown as PluginServerContext;
    const cleanup = registerHostedHandlers(server);
    expect(handlers.size).toBe(7);
    for (const handler of handlers.values()) {
      expect(await handler({}, {})).toMatchObject({ error: { code: "hosted_disabled", retryable: false } });
    }
    cleanup();
  });
  it("cancels a consented execution before a delayed workspace refresh can permit submission", async () => {
    const f = await fixture();
    let release!: (value: { id: string; status: string }) => void;
    try {
      const preview = (await f.preview()).preview!;
      f.refresh.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
      const executing = f.application.execute(executeInput(preview), f.access);
      expect(await f.application.cancel({ taskId: "task_1", workspaceId: "workspace_1" }, f.access)).toMatchObject({ cancelled: true });
      expect(await executing).toMatchObject({ result: null, error: { code: "cancelled" } });
      release({ id: "workspace_1", status: "active" });
      await new Promise((resolve) => setImmediate(resolve));
      expect(f.ledger.reserve).not.toHaveBeenCalled(); expect(f.createProvider).not.toHaveBeenCalled();
      expect(f.capture).not.toHaveBeenCalled();
    } finally { f.application.dispose(); f.service.dispose(); }
  });
  it("bounds concurrent preview verification and retains admission until abort-ignoring readers settle", async () => {
    const f = await fixture({ timeoutMs: 20 });
    const complete: Array<(value: Awaited<ReturnType<typeof f.readSource>>) => void> = [];
    let started!: () => void; const bothStarted = new Promise<void>((resolve) => { started = resolve; });
    try {
      await f.service.prepare({ taskId: "task_2", workspaceId: "workspace_1", provider: "opencode-go/synthetic-model", query: "synthetic",
        vaultScope: "work", maxSnippets: 8, maxChars: 12_000, includeRelated: false });
      f.readSource.mockImplementation(() => new Promise((resolve) => { complete.push(resolve); if (complete.length === 2) started(); }));
      const first = f.preview();
      const second = f.application.preview({ taskId: "task_2", workspaceId: "workspace_1", prompt: "second" }, f.access);
      await bothStarted;
      expect((await f.application.preview({ taskId: "task_3", workspaceId: "workspace_1", prompt: "third" }, f.access)).error?.code).toBe("busy");
      expect(f.readSource).toHaveBeenCalledTimes(2);
      await Promise.all([first, second]);
      expect((await f.preview()).error?.code).toBe("busy");
      expect(f.createProvider).not.toHaveBeenCalled();
    } finally {
      f.application.dispose();
      for (const done of complete) done({ success: true, source_ref: ref, content: "APPROVED SOURCE", truncated: false, source_content_untrusted: true });
      await new Promise((resolve) => setImmediate(resolve)); f.service.dispose();
    }
  });
  it("disposal prevents late preview restoration and further application work", async () => {
    const f = await fixture();
    try {
      const preview = (await f.preview()).preview!; f.application.dispose();
      expect((await f.application.execute(executeInput(preview), f.access)).error?.code).toBe("hosted_disabled");
      expect(f.createProvider).not.toHaveBeenCalled();
    } finally { f.application.dispose(); f.service.dispose(); }
  });
});
