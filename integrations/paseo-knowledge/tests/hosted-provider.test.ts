import { describe, expect, it, vi } from "vitest";
import { createHostedProviderFactory } from "../server/hosted-provider.js";
import type { HostedDisclosurePolicy } from "../server/hosted-task-policy.js";
import type { OpenCodeCredential } from "../server/provider-runner.js";
const policy: HostedDisclosurePolicy = {
  policyId: "synthetic-policy", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "synthetic-model",
  vaultScope: "work", tools: "none", network: "opencode-provider-api", disclosure: "work-context-to-opencode-and-upstream",
  maxInputChars: 10_000, maxOutputChars: 100, maxTokens: 1_024, timeoutMs: 1_000, freshnessMs: 1_000,
};
const request = { prompt: "SYNTHETIC PROMPT", sources: [{ path: "source_1", text: "SYNTHETIC SOURCE" }], sessionId: "execution_1" };
const reply = () => new Response(JSON.stringify({ id: "completion_1", model: policy.model,
  choices: [{ finish_reason: "stop", message: { role: "assistant", content: "SYNTHETIC DRAFT" } }] }));
describe("internal hosted provider policy adapter", () => {
  it("enforces the pinned policy and forwards completion identity without exposing credentials", async () => {
    const sent: { url: string; init?: RequestInit }[] = [];
    const transport: typeof fetch = async (url, init) => { sent.push({ url: String(url), init }); return reply(); };
    const factory = createHostedProviderFactory({ resolveCredential: async () => ({ token: "synthetic-key", kind: "api_key" }), transport });
    const result = await factory(policy, async () => true).run(request);
    expect(result).toEqual({ status: "completed", output: "SYNTHETIC DRAFT", completionId: "completion_1", model: policy.model });
    expect(sent[0]?.url).toBe(`${policy.endpoint}/chat/completions`);
    expect(sent[0]?.init?.redirect).toBe("manual");
    const payload = JSON.parse(String(sent[0]?.init?.body));
    expect(payload.model).toBe(policy.model);
    expect(payload.max_tokens).toBe(policy.maxTokens);
    expect(payload).not.toHaveProperty("tools");
    expect(JSON.stringify(result)).not.toContain("synthetic-key");
  });
  it("rejects absent, expired and near-expiry OAuth credentials before transport", async () => {
    for (const credential of [null, { token: "expired", kind: "oauth", expiresAt: 1 },
      { token: "missing-expiry", kind: "oauth" }, { token: "near-expiry", kind: "oauth", expiresAt: 70_000 }] as (OpenCodeCredential | null)[]) {
      const transport = vi.fn(async () => reply());
      const provider = createHostedProviderFactory({ resolveCredential: async () => credential, transport, now: () => 10_000 })(policy, async () => true);
      expect(await provider.run(request)).toMatchObject({ status: "failed", error: { code: "not_configured" } });
      expect(transport).not.toHaveBeenCalled();
    }
  });
  it("resolves credentials afresh for each execution instead of caching an expiring token", async () => {
    const resolveCredential = vi.fn(async (): Promise<OpenCodeCredential> => ({ token: "fresh-oauth", kind: "oauth", expiresAt: 100_000 }));
    const transport = vi.fn(async () => reply());
    let time = 10_000;
    const provider = createHostedProviderFactory({ resolveCredential, transport, now: () => time })(policy, async () => true);
    expect((await provider.run(request)).status).toBe("completed");
    time = 50_000;
    expect(await provider.run(request)).toMatchObject({ status: "failed", error: { code: "not_configured" } });
    expect(resolveCredential).toHaveBeenCalledTimes(2);
    expect(transport).toHaveBeenCalledOnce();
  });
  it("does not load credentials for rejected policies or pre-aborted requests", async () => {
    const resolveCredential = vi.fn(async () => null);
    const factory = createHostedProviderFactory({ resolveCredential, transport: async () => reply() });
    const invalid = { ...policy, network: "none" } as unknown as HostedDisclosurePolicy;
    expect(await factory(invalid, async () => true).run(request)).toMatchObject({ status: "failed", error: { code: "not_configured" } });
    const controller = new AbortController(); controller.abort();
    expect(await factory(policy, async () => true).run({ ...request, signal: controller.signal })).toMatchObject({ status: "failed", error: { code: "cancelled" } });
    expect(resolveCredential).not.toHaveBeenCalled();
  });
  it("does not send after cancellation during credential acquisition or leak resolver errors", async () => {
    const controller = new AbortController();
    const transport = vi.fn(async () => reply());
    const factory = createHostedProviderFactory({ resolveCredential: async () => { controller.abort(); return { token: "synthetic-key", kind: "api_key" }; }, transport });
    expect(await factory(policy, async () => true).run({ ...request, signal: controller.signal })).toMatchObject({ status: "failed", error: { code: "cancelled" } });
    expect(transport).not.toHaveBeenCalled();
    const broken = createHostedProviderFactory({ resolveCredential: async () => { throw new Error("SECRET DETAILS"); }, transport });
    const result = await broken(policy, async () => true).run(request);
    expect(result).toMatchObject({ status: "failed", error: { code: "not_configured" } });
    expect(JSON.stringify(result)).not.toContain("SECRET");
  });
  it("rechecks source authorization after credentials resolve and refuses an invalidated snapshot", async () => {
    const transport = vi.fn(async () => reply());
    let credentialsResolved = false;
    const authorize = vi.fn(async () => { expect(credentialsResolved).toBe(true); return false; });
    const provider = createHostedProviderFactory({ resolveCredential: async () => { credentialsResolved = true; return { token: "synthetic-key", kind: "api_key" }; }, transport })(policy, authorize);
    expect(await provider.run(request)).toMatchObject({ status: "failed", error: { code: "source_invalidated" } });
    expect(authorize).toHaveBeenCalledOnce();
    expect(transport).not.toHaveBeenCalled();
  });

  it("revalidates OAuth expiry after asynchronous source authorization", async () => {
    let time = 10_000;
    const transport = vi.fn(async () => reply());
    const provider = createHostedProviderFactory({ resolveCredential: async () => ({ token: "synthetic-oauth", kind: "oauth", expiresAt: 100_000 }),
      transport, now: () => time })(policy, async () => { time = 50_000; return true; });
    expect(await provider.run(request)).toMatchObject({ status: "failed", error: { code: "not_configured" } });
    expect(transport).not.toHaveBeenCalled();
  });

});
