import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";
import type { PluginServerContext } from "@getpaseo/plugin/server";
import type { PaseoApi } from "@getpaseo/client";
import { expect, it, vi } from "vitest";
import { KnowledgeCliRunner } from "../server/core.js";
import { KnowledgeTaskService } from "../server/service.js";
import { createCandidateHostedApplication } from "../server/hosted-setup.js";
import { registerHostedHandlers } from "../server/hosted-rpc.js";
import { HostedPreviewRpc, HostedExecuteRpc, HostedDraftListRpc, HostedDraftReadRpc, HostedDraftRejectRpc } from "../shared/hosted-contracts.js";

it("bridges real synthetic Python preparation/provenance through consent RPC to private draft review without modifying a formal note", async () => {
  const python = execFileSync("/usr/bin/which", ["python3"], { encoding: "utf8" }).trim();
  expect(existsSync(python)).toBe(true);
  const root = mkdtempSync(join(realpathSync(tmpdir()), "knowledge-core-bridge-"));
  const runtime = join(root, "runtime"); const work = join(root, "work"); const personal = join(root, "empty-personal"); const home = join(root, "empty-home");
  for (const path of [runtime, personal, home]) mkdirSync(path, { mode: 0o700 });
  const oldHome = process.env.HOME; process.env.HOME = home;
  const cli = realpathSync(new URL("../../../.agent/tools/knowledge.py", import.meta.url));
  let service: KnowledgeTaskService | undefined; let cleanup: (() => void) | undefined;
  let sent = ""; let count = 0;
  const server = createServer((req, res) => { count++; req.on("data", (chunk) => { sent += chunk; }); req.on("end", () => {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ id: "synthetic_completion_1", model: "synthetic-model", choices: [{ finish_reason: "stop", message: { role: "assistant", content: "OFFLINE SYNTHETIC DRAFT" } }] }));
  }); });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  try {
    execFileSync(python, [cli, "init", "--work-vault", work, "--runtime-dir", runtime], { stdio: "pipe" });
    const note = "---\nnote_id: synthetic-hosted-note\ntitle: Hosted synthetic fixture\ntype: project\n---\n\n# Hosted synthetic fixture\n\nThe offline acceptance word is BLUE-LANTERN.\n";
    writeFileSync(join(work, "10-Projects/synthetic.md"), note);
    execFileSync(python, [cli, "index", "--work-vault", work, "--runtime-dir", runtime], { stdio: "pipe" });
    const core = new KnowledgeCliRunner({ pythonExecutable: python, cliPath: cli, workVaultPath: work, personalVaultPath: personal, runtimeDir: runtime });
    service = new KnowledgeTaskService({ core });
    const prepared = await service.prepare({ taskId: "task_bridge_1", workspaceId: "workspace_bridge_1", provider: "opencode-go/synthetic-model",
      query: "BLUE", vaultScope: "work", includeRelated: false, maxSnippets: 8, maxChars: 12_000 });
    expect(prepared.error).toBeNull();
    const resolveCredential = vi.fn(async () => ({ token: "synthetic-no-real-access", kind: "api_key" as const }));
    const application = createCandidateHostedApplication({ store: service.store, core, privateRuntimeDir: runtime, forbiddenVaultRoots: [work, personal],
      policy: { policyId: "synthetic-policy", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "synthetic-model",
        vaultScope: "work", tools: "none", network: "opencode-provider-api", disclosure: "work-context-to-opencode-and-upstream",
        maxInputChars: 12_000, maxOutputChars: 1_000, maxTokens: 1_024, timeoutMs: 10_000, freshnessMs: 30_000 },
      resolveCredential, transport: (url, init) => fetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}${new URL(String(url)).pathname}`, init),
    });
    const handlers = new Map<string, (input: unknown, context: { paseo: PaseoApi }) => unknown>();
    const host = { handle: vi.fn((rpc, handler) => handlers.set(rpc.name, handler)) } as unknown as PluginServerContext;
    cleanup = registerHostedHandlers(host, application);
    const paseo = { workspaces: { ref: () => ({ refresh: async () => ({ id: "workspace_bridge_1", status: "active" }) }) } } as unknown as PaseoApi;
    const previewResponse = HostedPreviewRpc.output.parse(await handlers.get(HostedPreviewRpc.name)!({ taskId: "task_bridge_1", workspaceId: "workspace_bridge_1", prompt: "Summarize the approved synthetic work excerpt." }, { paseo }));
    expect(previewResponse.error).toBeNull();
    const preview = previewResponse.preview!;
    expect(preview.sources.length).toBeGreaterThan(0);
    expect(preview.sources.some((source) => source.text.includes("BLUE-LANTERN"))).toBe(true);
    expect(count).toBe(0); expect(resolveCredential).not.toHaveBeenCalled();
    const executed = HostedExecuteRpc.output.parse(await handlers.get(HostedExecuteRpc.name)!({ taskId: preview.taskId, workspaceId: preview.workspaceId, previewId: preview.previewId,
      challenge: preview.challenge, payloadDigest: preview.payloadDigest }, { paseo }));
    expect(executed.error).toBeNull(); expect(executed.result?.status).toBe("completed");
    if (executed.result?.status !== "completed") throw new Error("Synthetic execution did not complete");
    const draftId = executed.result.draftId;
    expect(count).toBe(1); expect(resolveCredential).toHaveBeenCalledOnce();
    expect(sent).not.toContain("10-Projects"); expect(sent).not.toContain("synthetic.md");
    const listed = HostedDraftListRpc.output.parse(await handlers.get(HostedDraftListRpc.name)!({ workspaceId: preview.workspaceId }, { paseo }));
    expect(listed.error).toBeNull(); expect(listed.drafts.map((draft) => draft.draftId)).toContain(draftId);
    const read = HostedDraftReadRpc.output.parse(await handlers.get(HostedDraftReadRpc.name)!({ workspaceId: preview.workspaceId, draftId }, { paseo }));
    expect(read.error).toBeNull(); expect(read.draft?.output).toBe("OFFLINE SYNTHETIC DRAFT"); expect(read.draft?.status).toBe("pending_review");
    const rejected = HostedDraftRejectRpc.output.parse(await handlers.get(HostedDraftRejectRpc.name)!({ workspaceId: preview.workspaceId, draftId, outputDigest: read.draft!.outputDigest }, { paseo }));
    expect(rejected).toEqual({ rejected: true, error: null });
    const reread = HostedDraftReadRpc.output.parse(await handlers.get(HostedDraftReadRpc.name)!({ workspaceId: preview.workspaceId, draftId }, { paseo }));
    expect(reread.draft?.status).toBe("rejected");
    expect(readFileSync(join(work, "10-Projects/synthetic.md"), "utf8")).toBe(note);
    expect(service.store.get(preview.taskId)?.agentId).toBeNull(); expect(service.store.get(preview.taskId)?.turnId).toBeNull();
  } finally {
    cleanup?.(); service?.dispose();
    await new Promise<void>((done) => server.close(() => done()));
    if (oldHome === undefined) delete process.env.HOME; else process.env.HOME = oldHome;
    rmSync(root, { force: true, recursive: true });
  }
}, 30_000);
