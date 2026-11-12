import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";
import { expect, it, vi } from "vitest";
import { FileHostedDraftSink } from "../server/hosted-draft-sink.js";
import { FileHostedExecutionLedger } from "../server/hosted-execution-ledger.js";
import { createHostedProviderFactory } from "../server/hosted-provider.js";
import { HostedTaskCoordinator } from "../server/hosted-task.js";
import { digest, HostedPreparationRegistry, type HostedDisclosurePolicy, type HostedTaskInput } from "../server/hosted-task-policy.js";

it("runs approved synthetic context through pinned HTTP, durable no-replay and a private pending-review draft", async () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "knowledge-hosted-flow-"));
  const runtime = join(root, "runtime"); const work = join(root, "work");
  mkdirSync(runtime, { mode: 0o700 }); mkdirSync(work, { mode: 0o700 });
  const fullSource = "# SYNTHETIC SOURCE\n\nApproved excerpt.\n";
  writeFileSync(join(work, "synthetic.md"), fullSource);
  let sent = ""; let requests = 0;
  const server = createServer((req, res) => {
    requests++; req.on("data", (chunk) => { sent += chunk; }); req.on("end", () => {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ id: "provider_completion_1", model: "synthetic-model",
        choices: [{ finish_reason: "stop", message: { role: "assistant", content: "SYNTHETIC PRIVATE DRAFT" } }] }));
    });
  });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  try {
    const timestamp = Date.now();
    const policy: HostedDisclosurePolicy = {
      policyId: "synthetic-policy", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "synthetic-model",
      vaultScope: "work", tools: "none", network: "opencode-provider-api", disclosure: "work-context-to-opencode-and-upstream",
      maxInputChars: 10_000, maxOutputChars: 1_000, maxTokens: 1_024, timeoutMs: 2_000, freshnessMs: 10_000,
    };
    const data: HostedTaskInput = { prompt: "Summarize approved synthetic excerpt", binding: {
      taskId: "task_flow_1", workspaceId: "workspace_flow_1", provider: "opencode-go/synthetic-model", vaultScope: "work", preparedAt: timestamp,
      sources: [{ sourceId: "opaque_source_1", path: "synthetic.md", text: "Approved excerpt.", contentHash: digest(fullSource), excerptHash: digest("Approved excerpt.") }],
    }, expected: { taskId: "task_flow_1", workspaceId: "workspace_flow_1", provider: "opencode-go/synthetic-model" } };
    const resolveCredential = vi.fn(async () => ({ token: "synthetic-test-key", kind: "api_key" as const }));
    const provider = createHostedProviderFactory({ resolveCredential,
      transport: (url, init) => fetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}${new URL(String(url)).pathname}`, init) });
    const preparations = new HostedPreparationRegistry();
    const checkSources = async (binding: HostedTaskInput["binding"]) => binding.sources.every((source) =>
      digest(readFileSync(join(work, source.path), "utf8")) === source.contentHash);
    const options = { policy, preparations, checkSources, ledger: new FileHostedExecutionLedger(join(runtime, "executions")),
      createProvider: provider, drafts: new FileHostedDraftSink({ privateRuntimeDir: runtime, forbiddenVaultRoots: [work] }) };
    const coordinator = new HostedTaskCoordinator(options);
    const result = await coordinator.start({ ...data, approvalId: preparations.approve(policy, data, timestamp) });
    expect(result.status).toBe("completed");
    if (result.status !== "completed") throw new Error("Synthetic flow did not complete");
    expect(requests).toBe(1);
    expect(resolveCredential).toHaveBeenCalledOnce();
    const wire = JSON.parse(sent);
    expect(wire).not.toHaveProperty("tools");
    expect(sent).toContain("opaque_source_1");
    expect(sent).not.toContain("synthetic.md");
    expect(sent).not.toContain("# SYNTHETIC SOURCE");
    const draft = JSON.parse(readFileSync(join(runtime, "hosted-drafts", `${result.draftId}.json`), "utf8"));
    expect(draft.status).toBe("pending_review");
    expect(draft.executionId).toBe(result.executionId);
    expect(draft.completionId).toBe(result.completionId);
    expect(draft.output).toBe("SYNTHETIC PRIVATE DRAFT");
    expect(draft.sources[0].contentHash).toBe(digest(fullSource));
    expect(draft.sources[0].excerptHash).toBe(digest("Approved excerpt."));
    expect(draft.sources[0]).not.toHaveProperty("text");
    const storedLedger = readFileSync(join(runtime, "executions", `${digest(data.binding.taskId)}.json`), "utf8");
    const terminal = JSON.parse(storedLedger);
    expect(terminal.outcome).toEqual({ status: "completed", completionId: result.completionId, draftId: result.draftId });
    expect(terminal.reservation.executionId).toBe(result.executionId);
    expect(terminal.reservation.inputDigest).toBe(draft.reservation.inputDigest);
    const expectedPreparation = { binding: {
      taskId: data.binding.taskId, workspaceId: data.binding.workspaceId, provider: data.binding.provider,
      vaultScope: data.binding.vaultScope, preparedAt: data.binding.preparedAt,
      sources: data.binding.sources.map((source) => ({ sourceId: source.sourceId, path: source.path,
        contentHash: source.contentHash, excerptHash: source.excerptHash, text: source.text })),
    }, expected: data.expected, prompt: data.prompt, excludedSourceIds: [] };
    const independentInputDigest = createHash("sha256").update(JSON.stringify({ policy, prepared: expectedPreparation })).digest("hex");
    expect(terminal.reservation.inputDigest).toBe(independentInputDigest);
    expect(storedLedger).not.toContain("Approved excerpt");
    expect(storedLedger).not.toContain("SYNTHETIC PRIVATE DRAFT");
    expect(storedLedger).not.toContain("synthetic-test-key");
    expect(storedLedger).not.toContain("Summarize");
    const restarted = new HostedTaskCoordinator({ ...options, ledger: new FileHostedExecutionLedger(join(runtime, "executions")) });
    expect(await restarted.start({ ...data, approvalId: preparations.approve(policy, data, timestamp) }))
      .toMatchObject({ status: "failed", error: { code: "already_submitted", retryable: false } });
    expect(requests).toBe(1);
    writeFileSync(join(work, "synthetic.md"), "changed after approval");
    const changed = { ...data, binding: { ...data.binding, taskId: "task_flow_2" }, expected: { ...data.expected, taskId: "task_flow_2" } };
    expect(await coordinator.start({ ...changed, approvalId: preparations.approve(policy, changed, timestamp) }))
      .toMatchObject({ status: "failed", error: { code: "source_invalidated" } });
    expect(resolveCredential).toHaveBeenCalledOnce();
    expect(requests).toBe(1);
    expect(readdirSync(join(runtime, "hosted-drafts"))).toHaveLength(1);
    expect(readdirSync(work)).toEqual(["synthetic.md"]);
  } finally {
    await new Promise<void>((done) => server.close(() => done()));
    rmSync(root, { force: true, recursive: true });
  }
});
