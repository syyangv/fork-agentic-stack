import { join } from "node:path";
import { HostedKnowledgeApplication } from "./hosted-application.js";
import { StoredHostedPreparationSource } from "./hosted-preparation.js";
import { FileHostedExecutionLedger } from "./hosted-execution-ledger.js";
import { FileHostedDraftSink } from "./hosted-draft-sink.js";
import { FileHostedDraftReview } from "./hosted-draft-review.js";
import { createHostedProviderFactory } from "./hosted-provider.js";
import type { HostedDisclosurePolicy } from "./hosted-task-policy.js";
import type { OpenCodeCredential } from "./provider-runner.js";
import type { KnowledgeCore } from "./core.js";
import type { TaskStore } from "./task-store.js";

/** Explicit candidate assembly. The production entrypoint does not call this. */
export function createCandidateHostedApplication(options: {
  readonly store: TaskStore; readonly core: KnowledgeCore; readonly policy: HostedDisclosurePolicy;
  readonly privateRuntimeDir: string; readonly forbiddenVaultRoots: readonly string[];
  readonly resolveCredential: (signal?: AbortSignal) => Promise<OpenCodeCredential | null>;
  readonly transport?: typeof fetch; readonly now?: () => number;
}): HostedKnowledgeApplication {
  const readSource = options.core.readSource?.bind(options.core);
  if (!readSource) throw new Error("Hosted execution requires a verified work-source reader.");
  return new HostedKnowledgeApplication({ policy: options.policy, now: options.now,
    preparations: new StoredHostedPreparationSource(options.store, readSource),
    ledger: new FileHostedExecutionLedger(join(options.privateRuntimeDir, "hosted-executions")),
    createProvider: createHostedProviderFactory({ resolveCredential: options.resolveCredential, transport: options.transport, now: options.now }),
    drafts: new FileHostedDraftSink(options), review: new FileHostedDraftReview(options),
  });
}
