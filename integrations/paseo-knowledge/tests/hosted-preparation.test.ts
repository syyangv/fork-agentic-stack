import { describe, expect, it, vi } from "vitest";
import { KnowledgeTaskService } from "../server/service.js";
import { StoredHostedPreparationSource } from "../server/hosted-preparation.js";
import { digest, prepareHostedInput, type HostedDisclosurePolicy } from "../server/hosted-task-policy.js";
import type { KnowledgeCore } from "../server/core.js";

const ref = { source_id: "core_source_1", vault_id: "work", note_id: "synthetic-note", path: "10-Projects/synthetic.md",
  heading: "Synthetic", line_start: 1, line_end: 1, content_hash: digest("WHOLE FILE BYTES") };
const excerpt = "APPROVED EXCERPT";
const bundle = () => ({ status: "ok", task_id: "task_1", context: "UNTRUSTED CONTEXT", personal_context_used: false,
  source_content_untrusted: true, truncated: false, source_manifest: [ref],
  snippets: [{ source_ref: ref, content: excerpt, snippet: excerpt, source_content_untrusted: true, truncated: false }] });
async function fixture(data: Record<string, unknown> = bundle()) {
  const core: KnowledgeCore = { status: async () => ({ status: "ready" }), search: async () => ({}),
    buildContext: async () => data, proposeNote: async () => ({}) };
  const service = new KnowledgeTaskService({ core });
  await service.prepare({ taskId: "task_1", workspaceId: "workspace_1", provider: "opencode-go/synthetic-model", query: "synthetic",
    vaultScope: "work", includeRelated: false, maxSnippets: 8, maxChars: 12_000 });
  const read = vi.fn(async () => ({ success: true, source_ref: ref, content: excerpt, source_content_untrusted: true, truncated: false }));
  const source = new StoredHostedPreparationSource(service.store, read);
  return { service, source, read };
}
const policy: HostedDisclosurePolicy = { policyId: "synthetic", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "synthetic-model",
  vaultScope: "work", tools: "none", network: "opencode-provider-api", disclosure: "work-context-to-opencode-and-upstream",
  maxInputChars: 10_000, maxOutputChars: 1_000, maxTokens: 1_024, timeoutMs: 1_000, freshnessMs: 10_000 };
describe("server-owned hosted provenance preparation", () => {
  it("derives opaque range identity and distinct file/excerpt hashes from stored core context", async () => {
    const f = await fixture();
    try {
      const snapshot = f.source.snapshot("task_1", "workspace_1");
      expect(snapshot.binding.sources[0]?.contentHash).toBe(ref.content_hash);
      expect(snapshot.binding.sources[0]?.excerptHash).toBe(digest(excerpt));
      expect(snapshot.binding.sources[0]?.sourceId).toMatch(/^src_[a-f0-9]{64}$/);
      const checked = prepareHostedInput(policy, { binding: snapshot.binding, expected: { taskId: "task_1", workspaceId: "workspace_1", provider: "opencode-go/synthetic-model" }, prompt: "synthetic" }, Date.now());
      expect("input" in checked).toBe(true);
      if (!("input" in checked)) throw new Error("Preparation failed");
      expect(await f.source.validate(snapshot, checked.input.binding)).toBe(true);
      expect(f.read.mock.calls).toHaveLength(1);
    } finally { f.service.dispose(); }
  });
  it("revalidates ranges with the original core identity and refuses changed content", async () => {
    const f = await fixture();
    try {
      const snapshot = f.source.snapshot("task_1", "workspace_1");
      f.read.mockResolvedValue({ success: true, source_ref: ref, content: "changed", source_content_untrusted: true, truncated: false });
      expect(await f.source.validate(snapshot, snapshot.binding)).toBe(false);
      expect(f.read).toHaveBeenCalledWith(expect.objectContaining({ taskId: "task_1", source: expect.objectContaining({ source_id: ref.source_id, line_start: 1, content_hash: ref.content_hash }), maxChars: 12_000 }));
    } finally { f.service.dispose(); }
  });
  it("ignores only the CLI's display heading loss, never authoritative provenance fields", async () => {
    const f = await fixture();
    try {
      const snapshot = f.source.snapshot("task_1", "workspace_1");
      f.read.mockResolvedValue({ success: true, source_ref: { ...ref, heading: null } as unknown as typeof ref, content: excerpt, source_content_untrusted: true, truncated: false });
      expect(await f.source.validate(snapshot, snapshot.binding)).toBe(true);
      for (const changed of [{ source_id: "other" }, { vault_id: "personal" }, { note_id: "other" }, { path: "other.md" },
        { line_start: 2 }, { line_end: 2 }, { content_hash: digest("other") }]) {
        f.read.mockResolvedValue({ success: true, source_ref: { ...ref, ...changed }, content: excerpt, source_content_untrusted: true, truncated: false });
        expect(await f.source.validate(snapshot, snapshot.binding)).toBe(false);
      }
    } finally { f.service.dispose(); }
  });
  it("refuses mismatched workspaces, personal or truncated context, and excluded core sources", async () => {
    const f = await fixture();
    try {
      expect(() => f.source.snapshot("task_1", "other")).toThrow();
      const task = f.service.store.get("task_1")!;
      task.excludedSourceIds.add(ref.source_id);
      expect(() => f.source.snapshot("task_1", "workspace_1")).toThrow();
    } finally { f.service.dispose(); }
    for (const data of [{ ...bundle(), personal_context_used: true }, { ...bundle(), truncated: true },
      { ...bundle(), task_id: "other" }, { ...bundle(), source_manifest: [{ ...ref, vault_id: "personal" }] }]) {
      const test = await fixture(data);
      try { expect(() => test.source.snapshot("task_1", "workspace_1")).toThrow(); }
      finally { test.service.dispose(); }
    }
  });
  it("invalidates a snapshot after the stored task is prepared again or its exclusions change", async () => {
    const f = await fixture();
    try {
      const snapshot = f.source.snapshot("task_1", "workspace_1");
      f.service.store.get("task_1")!.query = "changed query";
      expect(await f.source.validate(snapshot, snapshot.binding)).toBe(false);
      expect(f.read).not.toHaveBeenCalled();
    } finally { f.service.dispose(); }
  });
  it("does not read on cancellation or a client-forged binding", async () => {
    const f = await fixture();
    try {
      const snapshot = f.source.snapshot("task_1", "workspace_1"); const controller = new AbortController(); controller.abort();
      expect(await f.source.validate(snapshot, snapshot.binding, controller.signal)).toBe(false);
      expect(await f.source.validate(snapshot, { ...snapshot.binding, sources: [{ ...snapshot.binding.sources[0]!, text: "forged" }] })).toBe(false);
      expect(f.read).not.toHaveBeenCalled();
    } finally { f.service.dispose(); }
  });
});
