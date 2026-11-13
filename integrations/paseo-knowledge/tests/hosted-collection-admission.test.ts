import { beforeEach, describe, expect, it, vi } from "vitest";
import { TaskStore } from "../server/task-store.js";
import { StoredHostedPreparationSource, HostedPreparationError } from "../server/hosted-preparation.js";
import { digest, prepareHostedInput, type HostedTaskInput, type HostedDisclosurePolicy } from "../server/hosted-task-policy.js";

const parser = vi.hoisted(() => ({ attempts: 0, block: false }));
vi.mock("zod", async (importOriginal) => {
  const actual = await importOriginal<typeof import("zod")>();
  return { ...actual, z: { ...actual.z, object: (...args: Parameters<typeof actual.z.object>) => {
    const schema = actual.z.object(...args);
    const shape = args[0];
    if (shape && (("binding" in shape && "expected" in shape) || ("source_manifest" in shape && "snippets" in shape))) {
      const safeParse = schema.safeParse.bind(schema);
      schema.safeParse = (...input: Parameters<typeof safeParse>) => {
        parser.attempts++;
        if (parser.block) throw new Error("Schema parser must not run before collection admission");
        return safeParse(...input);
      };
    }
    return schema;
  } } };
});
const now = Date.now();
const policy: HostedDisclosurePolicy = { policyId: "synthetic", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "synthetic-model",
  vaultScope: "work", tools: "none", network: "opencode-provider-api", disclosure: "work-context-to-opencode-and-upstream",
  maxInputChars: 65_536, maxOutputChars: 1_000, maxTokens: 1_024, timeoutMs: 1_000, freshnessMs: 10_000 };
function input(count = 1): HostedTaskInput {
  return { binding: { taskId: "task", workspaceId: "workspace", provider: "opencode-go/synthetic-model", vaultScope: "work", preparedAt: now,
    sources: Array.from({ length: count }, (_, n) => ({ sourceId: `source_${n}`, path: `notes/${n}.md`, text: "synthetic", contentHash: digest("file"), excerptHash: digest("synthetic") })) },
    expected: { taskId: "task", workspaceId: "workspace", provider: "opencode-go/synthetic-model" }, prompt: "synthetic" };
}
beforeEach(() => { parser.attempts = 0; parser.block = false; });
describe("collection admission independent of Zod's own patched behavior", () => {
  it("rejects oversized sources before invoking any input schema parser", () => {
    parser.block = true;
    expect(prepareHostedInput(policy, input(65), now)).toEqual({ error: "needs_prepare" });
    expect(parser.attempts).toBe(0);
  });
  it("rejects oversized exclusions before invoking any input schema parser", () => {
    parser.block = true;
    expect(prepareHostedInput(policy, { ...input(), excludedSourceIds: Array.from({ length: 65 }, (_, n) => `excluded_${n}`) }, now))
      .toEqual({ error: "needs_prepare" });
    expect(parser.attempts).toBe(0);
  });
  it("still invokes real schema validation for the exact allowed source boundary", () => {
    expect(prepareHostedInput(policy, input(64), now)).toHaveProperty("input");
    expect(parser.attempts).toBe(1);
  });
  it("rejects oversized stored manifests or snippets without invoking the bundle parser", () => {
    const store = new TaskStore();
    const task = store.configure("task", { workspaceId: "workspace", provider: "opencode-go/synthetic-model", vaultScope: "work", query: "synthetic" });
    const ref = { source_id: "source", vault_id: "work", note_id: "note", path: "note.md", heading: null, line_start: 1, line_end: 1, content_hash: digest("file") };
    store.prepared(task, { task_id: "task", status: "ok", personal_context_used: false, source_content_untrusted: true, truncated: false,
      source_manifest: [ref], snippets: [{ source_ref: ref, content: "synthetic", source_content_untrusted: true, truncated: false }] });
    const baseline = task.context!;
    const source = new StoredHostedPreparationSource(store, async () => { throw new Error("No reader should run"); });
    parser.block = true;
    for (const field of ["source_manifest", "snippets"]) {
      task.context = { ...baseline, [field]: Array.from({ length: 9 }, () => ({})) };
      expect(() => source.snapshot("task", "workspace")).toThrow(HostedPreparationError);
    }
    expect(parser.attempts).toBe(0);
  });
});
