import {existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, symlinkSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import type {PaseoApi} from "@getpaseo/client";
import {afterEach, describe, expect, it, vi} from "vitest";
import {KnowledgeTaskService} from "../server/service.js";
import type {OpenCodeCredential} from "../server/provider-runner.js";
import type {KnowledgeCore, CoreReadSourceRequest} from "../server/core.js";
import {HostedRequestBudget} from "../server/hosted-request-budget.js";
import {createProductionHostedRuntime} from "../server/hosted-production.js";
import {digest} from "../server/hosted-task-policy.js";
import type {HostedKnowledgeApplication} from "../server/hosted-application.js";
const cleanup: Array<() => void> = [];
afterEach(() => { for (const done of cleanup.splice(0).reverse()) done(); });
async function fixture(limit = 2) {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "hosted-production-"));
  const runtime = join(root, "runtime"), work = join(root, "work"), personal = join(root, "personal");
  for (const dir of [runtime, work, personal]) mkdirSync(dir, {mode: 0o700});
  cleanup.push(() => rmSync(root, {recursive: true, force: true}));
  let clock = Date.now();
  const ref = {source_id: "synthetic-source", vault_id: "work", note_id: "synthetic-note", path: "10-Projects/synthetic.md",
    heading: null, line_start: 1, line_end: 1, content_hash: digest("synthetic full file")};
  const readSource = vi.fn(async (_request: CoreReadSourceRequest) => ({success: true, source_ref: ref,
    content: "SYNTHETIC EXCERPT", truncated: false, source_content_untrusted: true}));
  const core: KnowledgeCore = {status: async () => ({}), search: async () => ({}), proposeNote: async () => ({}), readSource,
    buildContext: async request => ({status: "ok", task_id: request.taskId, personal_context_used: false, source_content_untrusted: true, truncated: false,
      source_manifest: [ref], snippets: [{source_ref: ref, content: "SYNTHETIC EXCERPT", source_content_untrusted: true, truncated: false}]})};
  const service = new KnowledgeTaskService({core}); cleanup.push(() => service.dispose());
  const config = {version: 1, enabled: true, mode: "synthetic-only", credentialSource: "environment", credentialReuseApproved: true,
    disclosureAccepted: "work-context-to-opencode-and-upstream", approvedAt: new Date(clock - 1_000).toISOString(), expiresAt: new Date(clock + 3_600_000).toISOString(),
    maxRequestsPerUtcDay: limit, allowedPromptHashes: [digest("Summarize only the synthetic source")], allowedWorkspaceIds: ["workspace_1"], allowedSources: [{path: ref.path, contentHash: ref.content_hash}],
    policy: {policyId: "synthetic-policy", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "synthetic-model", vaultScope: "work", tools: "none",
      network: "opencode-provider-api", disclosure: "work-context-to-opencode-and-upstream", maxInputChars: 12_000, maxOutputChars: 1_000, maxTokens: 1_024,
      timeoutMs: 1_000, freshnessMs: 300_000}};
  const env: NodeJS.ProcessEnv = {PASEO_KNOWLEDGE_RUNTIME_DIR: runtime, PASEO_KNOWLEDGE_WORK_VAULT: work,
    PASEO_KNOWLEDGE_PERSONAL_VAULT: personal, OPENCODE_API_KEY: "FAKE TEST KEY - NEVER A REAL CREDENTIAL"};
  const write = (value: unknown = config) => writeFileSync(join(runtime, "hosted-policy.json"), JSON.stringify(value), {mode: 0o600});
  const transport = vi.fn(async (_url: Parameters<typeof fetch>[0], _init?: Parameters<typeof fetch>[1]) => new Response(JSON.stringify({id: "completion_synthetic", model: "synthetic-model",
    choices: [{finish_reason: "stop", message: {role: "assistant", content: "SYNTHETIC PRIVATE DRAFT"}}]})));
  const session = vi.fn(async (): Promise<OpenCodeCredential | null> => ({kind: "oauth" as const, token: "SYNTHETIC OAUTH", expiresAt: clock + 1_000_000}));
  const build = () => {
    const result = createProductionHostedRuntime({store: service.store, env, core, transport, loadSessionCredential: session, now: () => clock});
    if (result.application) cleanup.push(() => result.application?.dispose());
    return result;
  };
  const access = {workspaces: {ref: (id: string) => ({refresh: async () => ({id, status: "active"})})}} as unknown as Pick<PaseoApi, "workspaces">;
  const prepare = async (id: string, workspaceId = "workspace_1") => {
    await service.prepare({taskId: id, workspaceId, provider: "opencode-go/synthetic-model", vaultScope: "work", query: "synthetic", includeRelated: false, maxSnippets: 8, maxChars: 12_000});
    service.store.get(id)!.updatedAt = new Date(clock).toISOString();
  };
  const preview = async (app: HostedKnowledgeApplication, id: string, workspaceId = "workspace_1") => app.preview({taskId: id, workspaceId, prompt: "Summarize only the synthetic source"}, access);
  const execute = async (app: HostedKnowledgeApplication, id: string) => {
    const {preview: p, error} = await preview(app, id); expect(error).toBeNull(); if (!p) throw new Error("Missing synthetic preview");
    return app.execute({taskId: id, workspaceId: p.workspaceId, previewId: p.previewId, challenge: p.challenge, payloadDigest: p.payloadDigest}, access);
  };
  await prepare("task_1");
  return {root, runtime, config, env, write, build, service, core, readSource, transport, session, access, prepare, preview, execute, advance: (ms: number) => { clock += ms; }};
}
describe("operator-opted-in production assembly (synthetic tests only)", () => {
  it("defaults disabled with no policy and never loads credentials or contacts a provider", async () => {
    const f = await fixture(); expect(f.build()).toMatchObject({disabledReason: "hosted_disabled"}); expect(f.build().application).toBeUndefined();
    expect(f.session).not.toHaveBeenCalled(); expect(f.transport).not.toHaveBeenCalled();
    f.write({version: 1, enabled: false}); expect(f.build().application).toBeUndefined();
  });
  it("requires the private runtime env path and a valid explicit approval", async () => {
    const f = await fixture(); delete f.env.PASEO_KNOWLEDGE_RUNTIME_DIR; expect(f.build().application).toBeUndefined();
    f.env.PASEO_KNOWLEDGE_RUNTIME_DIR = f.runtime; f.write({...f.config, credentialReuseApproved: false});
    expect(f.build()).toMatchObject({disabledReason: "hosted_policy_invalid"}); expect(f.transport).not.toHaveBeenCalled();
  });
  it("shows exact allowlisted source consent and produces only a private pending-review draft", async () => {
    const f = await fixture(); f.write(); const app = f.build().application!;
    expect(await app.availability("workspace_1", f.access)).toMatchObject({available: true});
    const result = await f.execute(app, "task_1"); expect(result.result?.status).toBe("completed");
    expect(f.transport).toHaveBeenCalledOnce(); expect(f.session).not.toHaveBeenCalled();
    expect(readdirSync(join(f.runtime, "hosted-drafts"))).toHaveLength(1);
    expect(readFileSync(join(f.runtime, "hosted-policy.json"), "utf8")).not.toContain(f.env.OPENCODE_API_KEY!);
    expect(f.service.store.get("task_1")?.agentId).toBeNull();
  });
  it("enforces workspace and synthetic file-hash allowlists before credentials or rereads", async () => {
    const f = await fixture(); f.write({...f.config, allowedSources: [{path: f.config.allowedSources[0]!.path, contentHash: digest("not allowlisted")}]});
    const app = f.build().application!;
    expect(await app.availability("workspace_other", f.access)).toMatchObject({available: false});
    expect((await f.preview(app, "task_1")).error?.code).toBe("source_invalidated");
    expect(f.readSource).not.toHaveBeenCalled(); expect(f.session).not.toHaveBeenCalled(); expect(f.transport).not.toHaveBeenCalled();
  });
  it("enforces a shared daily attempt cap across different tasks and application recreation", async () => {
    const f = await fixture(1); f.write(); const app = f.build().application!;
    expect((await f.execute(app, "task_1")).result?.status).toBe("completed");
    await f.prepare("task_2"); expect((await f.execute(f.build().application!, "task_2")).result)
      .toMatchObject({status: "failed", error: {code: "budget_exceeded", retryable: false}});
    expect(f.transport).toHaveBeenCalledOnce(); expect(readdirSync(join(f.runtime, "hosted-provider-attempts"))).toHaveLength(1);
  });
  it("counts failed HTTP attempts and never refunds an uncertain outcome", async () => {
    const f = await fixture(1); f.write(); f.transport.mockRejectedValue(new Error("synthetic network failure"));
    const app = f.build().application!;
    expect((await f.execute(app, "task_1")).result?.status).toBe("failed"); await f.prepare("task_2");
    expect((await f.execute(app, "task_2")).result).toMatchObject({status: "failed", error: {code: "budget_exceeded"}});
    expect(f.transport).toHaveBeenCalledOnce();
  });
  it("revokes loaded approval on any policy change and requires a reload to use the new policy", async () => {
    const f = await fixture(); f.write(); const app = f.build().application!;
    f.write({...f.config, maxRequestsPerUtcDay: 3});
    expect(await app.availability("workspace_1", f.access)).toMatchObject({available: false});
    expect((await f.preview(app, "task_1")).error?.code).toBe("source_invalidated"); expect(f.transport).not.toHaveBeenCalled();
    expect((await f.execute(f.build().application!, "task_1")).result?.status).toBe("completed");
  });
  it("fails closed after approval expiry without using a credential or reserving a provider slot", async () => {
    const f = await fixture(); f.write(); const app = f.build().application!; f.advance(3_600_001);
    expect(await app.availability("workspace_1", f.access)).toMatchObject({available: false}); expect(f.build().application).toBeUndefined();
    expect(f.build().disabledReason).toBe("hosted_policy_expired"); expect(f.session).not.toHaveBeenCalled(); expect(f.transport).not.toHaveBeenCalled();
    expect(existsSync(join(f.runtime, "hosted-provider-attempts"))).toBe(false);
  });
  it("uses only an explicitly approved stored OAuth session and revalidates policy after acquisition", async () => {
    const f = await fixture(); f.write({...f.config, credentialSource: "opencode-session"});
    const app = f.build().application!;
    f.session.mockImplementation(async () => { f.write({...f.config, enabled: false}); return {kind: "oauth", token: "SYNTHETIC OAUTH", expiresAt: Date.now() + 1_000_000}; });
    expect((await f.execute(app, "task_1")).result?.status).toBe("failed"); expect(f.session).toHaveBeenCalledOnce(); expect(f.transport).not.toHaveBeenCalled();
  });
  it("does not fall back to an API key or expired OAuth when stored-session reuse is selected", async () => {
    for (const credential of [{kind: "api_key", token: "SYNTHETIC OLD GO KEY"},
      {kind: "oauth", token: "SYNTHETIC EXPIRED SESSION", expiresAt: 1}, null] as const) {
      const f = await fixture(); f.write({...f.config, credentialSource: "opencode-session"});
      f.session.mockResolvedValue(credential);
      expect((await f.execute(f.build().application!, "task_1")).result).toMatchObject({status: "failed", error: {code: "not_configured"}});
      expect(f.transport).not.toHaveBeenCalled(); expect(existsSync(join(f.runtime, "hosted-provider-attempts"))).toBe(false);
    }
  });
  it("does not reset daily request slots when an operator reloads a new policy identity", async () => {
    const f = await fixture(1); f.write();
    expect((await f.execute(f.build().application!, "task_1")).result?.status).toBe("completed");
    f.write({...f.config, policy: {...f.config.policy, policyId: "replacement-policy", version: "2"}}); await f.prepare("task_2");
    expect((await f.execute(f.build().application!, "task_2")).result).toMatchObject({status: "failed", error: {code: "budget_exceeded"}});
    expect(f.transport).toHaveBeenCalledOnce();
  });
  it("rechecks policy revocation after a pending budget reservation before dispatch", async () => {
    const f = await fixture(); f.write(); const app = f.build().application!;
    let release!: (value: boolean) => void, entered!: () => void;
    const started = new Promise<void>(resolve => { entered = resolve; });
    const take = vi.spyOn(HostedRequestBudget.prototype, "take").mockImplementation(() => { entered(); return new Promise(resolve => { release = resolve; }); });
    try {
      const execution = f.execute(app, "task_1"); await started;
      f.write({...f.config, enabled: false}); release(true);
      expect((await execution).result).toMatchObject({status: "failed", error: {code: "policy_unavailable"}});
      expect(f.transport).not.toHaveBeenCalled();
    } finally { take.mockRestore(); }
  });
  it("uses a canonical private runtime when the trusted environment path is an alias", async () => {
    const f = await fixture(); f.write(); const alias = join(f.root, "runtime-alias"); symlinkSync(f.runtime, alias);
    f.env.PASEO_KNOWLEDGE_RUNTIME_DIR = alias;
    expect((await f.execute(f.build().application!, "task_1")).result?.status).toBe("completed");
    expect(f.transport).toHaveBeenCalledOnce();
  });
  it("rejects non-allowlisted synthetic-mode prompts before credentials or provider quota", async () => {
    const f = await fixture(); f.write({...f.config, credentialSource: "opencode-session"}); const app = f.build().application!;
    const response = await app.preview({taskId: "task_1", workspaceId: "workspace_1", prompt: "UNAPPROVED PRIVATE PROMPT"}, f.access);
    const p = response.preview!; expect(response.error).toBeNull();
    const result = await app.execute({taskId: p.taskId, workspaceId: p.workspaceId, previewId: p.previewId, challenge: p.challenge, payloadDigest: p.payloadDigest}, f.access);
    expect(result.result).toMatchObject({status: "failed", error: {code: "policy_unavailable"}});
    expect(f.session).not.toHaveBeenCalled(); expect(f.transport).not.toHaveBeenCalled();
    expect(existsSync(join(f.runtime, "hosted-provider-attempts"))).toBe(false);
  });
  it("allows work mode only after explicit policy opt-in and never touches the personal source seam", async () => {
    const f = await fixture(); f.write({...f.config, mode: "work", allowedSources: undefined, allowedPromptHashes: undefined}); const app = f.build().application!;
    expect((await f.execute(app, "task_1")).result?.status).toBe("completed");
    expect(f.readSource.mock.calls.every(([request]) => request.source.vault_id === "work")).toBe(true);
  });
});
