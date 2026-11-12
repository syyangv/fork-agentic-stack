import { z } from "zod";
import { SourceRefSchema, type SourceRef } from "../shared/contracts.js";
import { digest, type HostedTaskBinding } from "./hosted-task-policy.js";
import type { CoreReadSourceRequest } from "./core.js";
import type { TaskStore } from "./task-store.js";

const refSchema = SourceRefSchema.extend({ heading: z.string().max(512).nullable().optional(), path: z.string().min(1).max(1_024) }).transform((ref) => ({ ...ref, heading: ref.heading ?? null, content_hash: ref.content_hash.toLowerCase() }));
const bundleSchema = z.object({
  task_id: z.string(), status: z.literal("ok"), personal_context_used: z.literal(false),
  source_content_untrusted: z.literal(true), truncated: z.literal(false),
  source_manifest: z.array(refSchema).min(1).max(8),
  snippets: z.array(z.object({ source_ref: refSchema, content: z.string().max(12_000).optional(),
    snippet: z.string().max(12_000).optional(), source_content_untrusted: z.literal(true), truncated: z.literal(false).optional() })).min(1).max(8),
});
export class HostedPreparationError extends Error {
  public constructor(public readonly code: "needs_prepare" | "task_identity_conflict" | "personal_access_unavailable" | "source_invalidated") {
    super("Hosted preparation is unavailable or changed; prepare context again.");
  }
}
export interface StoredHostedPreparation {
  readonly binding: HostedTaskBinding;
  readonly revision: string;
  readonly refs: readonly SourceRef[];
}

// CLI lookup by source ID does not preserve the search-result heading label.
const provenanceKey = (ref: SourceRef) => JSON.stringify([ref.source_id, ref.vault_id, ref.note_id, ref.path, ref.line_start, ref.line_end, ref.content_hash]);
const bindingKey = (binding: HostedTaskBinding) => JSON.stringify([binding.taskId, binding.workspaceId, binding.provider,
  binding.vaultScope, binding.preparedAt, binding.sources.map((source) => [source.sourceId, source.path, source.contentHash, source.excerptHash, source.text])]);

/** Reads only server TaskStore and the provenance-checked work-source CLI port. */
export class StoredHostedPreparationSource {
  public constructor(private readonly store: TaskStore,
    private readonly readSource: (request: CoreReadSourceRequest) => Promise<Record<string, unknown>>) {}

  public snapshot(taskId: string, workspaceId: string): StoredHostedPreparation {
    const task = this.store.get(taskId);
    if (!task || !task.context || task.status !== "prepared") throw new HostedPreparationError("needs_prepare");
    if (task.workspaceId !== workspaceId || !task.provider || task.agentId) throw new HostedPreparationError("task_identity_conflict");
    if (task.vaultScope !== "work" || task.personalContextUsed) throw new HostedPreparationError("personal_access_unavailable");
    const parsed = bundleSchema.safeParse(task.context);
    if (!parsed.success || parsed.data.task_id !== taskId) throw new HostedPreparationError("needs_prepare");
    const bundle = parsed.data;
    const manifest = task.sourceManifest.map((source) => refSchema.parse(source));
    if (JSON.stringify(manifest) !== JSON.stringify(bundle.source_manifest) || manifest.length !== bundle.snippets.length
      || manifest.some((ref) => ref.vault_id !== "work" || task.excludedSourceIds.has(ref.source_id))) {
      throw new HostedPreparationError("source_invalidated");
    }
    const sources = bundle.snippets.map((snippet, index) => {
      if (JSON.stringify(snippet.source_ref) !== JSON.stringify(manifest[index])) throw new HostedPreparationError("source_invalidated");
      const text = snippet.content ?? snippet.snippet;
      if (!text || (snippet.content !== undefined && snippet.snippet !== undefined && snippet.content !== snippet.snippet)) {
        throw new HostedPreparationError("source_invalidated");
      }
      return { sourceId: `src_${digest(JSON.stringify(snippet.source_ref))}`, path: snippet.source_ref.path,
        text, contentHash: snippet.source_ref.content_hash, excerptHash: digest(text) };
    });
    const preparedAt = Date.parse(task.updatedAt);
    if (!Number.isSafeInteger(preparedAt)) throw new HostedPreparationError("needs_prepare");
    const binding: HostedTaskBinding = { taskId, workspaceId, provider: task.provider, vaultScope: "work", preparedAt, sources };
    const revision = digest(JSON.stringify({ binding, query: task.query, project: task.project,
      exclusions: [...task.excludedSourceIds].sort(), manifest }));
    return { binding, revision, refs: manifest };
  }

  public async validate(snapshot: StoredHostedPreparation, binding: HostedTaskBinding, signal?: AbortSignal): Promise<boolean> {
    if (signal?.aborted) return false;
    let current;
    try { current = this.snapshot(binding.taskId, binding.workspaceId); } catch { return false; }
    if (current.revision !== snapshot.revision || bindingKey(binding) !== bindingKey(snapshot.binding)) return false;
    for (let index = 0; index < snapshot.refs.length; index++) {
      if (signal?.aborted) return false;
      try {
        const read = await this.readSource({ taskId: binding.taskId, source: snapshot.refs[index]!, maxChars: 12_000, signal });
        if (signal?.aborted || read.success !== true || read.source_content_untrusted !== true || read.truncated !== false) return false;
        const ref = refSchema.safeParse(read.source_ref);
        const text = typeof read.content === "string" ? read.content : read.snippet;
        if (!ref.success || provenanceKey(ref.data) !== provenanceKey(snapshot.refs[index]!) || typeof text !== "string"
          || digest(text) !== binding.sources[index]?.excerptHash) return false;
      } catch { return false; }
    }
    try { return this.snapshot(binding.taskId, binding.workspaceId).revision === snapshot.revision && !signal?.aborted; }
    catch { return false; }
  }
}
