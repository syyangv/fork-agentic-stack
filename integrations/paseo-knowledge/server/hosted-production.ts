import {isAbsolute, join} from "node:path";
import {homedir} from "node:os";
import {KnowledgeCliRunner, type KnowledgeCore, type CoreReadSourceRequest} from "./core.js";
import {HostedKnowledgeApplication} from "./hosted-application.js";
import {StoredHostedPreparationSource, HostedPreparationError, type StoredHostedPreparation} from "./hosted-preparation.js";
import {FileHostedExecutionLedger} from "./hosted-execution-ledger.js";
import {FileHostedDraftSink} from "./hosted-draft-sink.js";
import {FileHostedDraftReview} from "./hosted-draft-review.js";
import {createHostedProviderFactory} from "./hosted-provider.js";
import {loadOpenCodeCredential, type OpenCodeCredential, type ProviderRunResult} from "./provider-runner.js";
import type {TaskStore} from "./task-store.js";
import type {HostedProvider, HostedProviderAuthorization} from "./hosted-task.js";
import {digest, type HostedDisclosurePolicy} from "./hosted-task-policy.js";
import {HostedRequestBudget} from "./hosted-request-budget.js";
import {readHostedRuntimeConfig, HostedRuntimeConfigError, type LoadedHostedRuntimeConfig} from "./hosted-runtime-config.js";

class PolicyPreparationSource extends StoredHostedPreparationSource {
  public constructor(store: TaskStore, readSource: (request: CoreReadSourceRequest) => Promise<Record<string, unknown>>,
    private readonly loaded: LoadedHostedRuntimeConfig, private readonly current: () => boolean) { super(store, readSource); }
  public override snapshot(taskId: string, workspaceId: string): StoredHostedPreparation {
    const config = this.loaded.config;
    if (!this.current() || !config.allowedWorkspaceIds.includes(workspaceId)) throw new HostedPreparationError("source_invalidated");
    const snapshot = super.snapshot(taskId, workspaceId);
    if (config.allowedSources?.length && snapshot.binding.sources.some(source =>
      !config.allowedSources!.some(allowed => allowed.path === source.path && allowed.contentHash === source.contentHash))) {
      throw new HostedPreparationError("source_invalidated");
    }
    return snapshot;
  }
}
class OperatorBoundApplication extends HostedKnowledgeApplication {
  public constructor(options: ConstructorParameters<typeof HostedKnowledgeApplication>[0], private readonly allowed: (workspaceId: string) => boolean) { super(options); }
  public override async availability(workspaceId: string, access: Parameters<HostedKnowledgeApplication["availability"]>[1]) {
    if (!this.allowed(workspaceId)) return {available: false, error: {code: "hosted_policy_unavailable", retryable: false as const}};
    return super.availability(workspaceId, access);
  }
}
export interface ProductionHostedRuntime {
  readonly application?: HostedKnowledgeApplication;
  readonly disabledReason: string;
}

function createPolicyProviderFactory(options: {
  readonly current: () => boolean; readonly budget: HostedRequestBudget; readonly promptAllowed: (prompt: string) => boolean;
  readonly resolveCredential: (signal?: AbortSignal) => Promise<OpenCodeCredential | null>;
  readonly transport?: typeof fetch; readonly now: () => number;
}): (policy: HostedDisclosurePolicy, authorize: HostedProviderAuthorization) => HostedProvider {
  return (policy, authorize) => ({async run(request) {
        let denied: "budget_exceeded" | "policy_unavailable" | "cancelled" | undefined;
        const reject = (code: NonNullable<typeof denied>): ProviderRunResult => ({status: "failed", error: {code, message: "The hosted operator policy or request budget denied execution.", retryable: false}});
        if (!options.promptAllowed(request.prompt)) return reject("policy_unavailable");
        const transport: typeof fetch = async (url, init) => {
          if (init?.signal?.aborted) { denied = "cancelled"; throw new Error("Cancelled"); }
          if (!options.current()) { denied = "policy_unavailable"; throw new Error("Policy unavailable"); }
          try {
            if (!await options.budget.take()) { denied = "budget_exceeded"; throw new Error("Budget unavailable"); }
          } catch { denied ??= "policy_unavailable"; throw new Error("Budget unavailable"); }
          if (!options.current() || init?.signal?.aborted) { denied = init?.signal?.aborted ? "cancelled" : "policy_unavailable"; throw new Error("Execution no longer allowed"); }
          return (options.transport ?? fetch)(url, init);
        };
        const provider = createHostedProviderFactory({resolveCredential: options.resolveCredential, transport, now: options.now})(policy, authorize);
        const result = await provider.run(request);
        return denied ? reject(denied) : result;
  }});
}

/** Explicit private-file opt-in only. No default inference or default credential reuse. */
export function createProductionHostedRuntime(options: {
  readonly store: TaskStore; readonly env?: NodeJS.ProcessEnv; readonly now?: () => number;
  readonly core?: KnowledgeCore; readonly transport?: typeof fetch;
  readonly loadSessionCredential?: (signal?: AbortSignal) => Promise<OpenCodeCredential | null>;
}): ProductionHostedRuntime {
  const env = options.env ?? process.env, now = options.now ?? Date.now;
  const directory = env.PASEO_KNOWLEDGE_RUNTIME_DIR;
  if (!directory) return {disabledReason: "hosted_disabled"};
  try {
    const loaded = readHostedRuntimeConfig(directory, now());
    if (!loaded) return {disabledReason: "hosted_disabled"};
    const config = loaded.config;
    const current = () => {
      try {
        const next = readHostedRuntimeConfig(directory, now());
        return next?.fingerprint === loaded.fingerprint && next?.runtime === loaded.runtime;
      } catch { return false; }
    };
    const work = env.PASEO_KNOWLEDGE_WORK_VAULT;
    const personal = env.PASEO_KNOWLEDGE_PERSONAL_VAULT ?? join(homedir(), "obsidian", "syang");
    if (!work || !isAbsolute(work) || !isAbsolute(personal)) return {disabledReason: "hosted_runtime_unavailable"};
    const core = options.core ?? KnowledgeCliRunner.fromEnvironment(env);
    const reader = core.readSource?.bind(core);
    if (!reader) return {disabledReason: "hosted_runtime_unavailable"};
    const roots = {privateRuntimeDir: loaded.runtime, forbiddenVaultRoots: [work, personal]};
    const budget = new HostedRequestBudget(join(loaded.runtime, "hosted-provider-attempts"), config.maxRequestsPerUtcDay, now);
    const resolveCredential = async (signal?: AbortSignal): Promise<OpenCodeCredential | null> => {
      if (!current() || signal?.aborted) return null;
      if (config.credentialSource === "environment") {
        const token = env.OPENCODE_API_KEY?.trim();
        return token ? {token, kind: "api_key"} : null;
      }
      const credential = options.loadSessionCredential
        ? await options.loadSessionCredential(signal) : await loadOpenCodeCredential({env: {}});
      return credential?.kind === "oauth" && current() && !signal?.aborted ? credential : null;
    };
    const app = new OperatorBoundApplication({policy: config.policy, now,
      preparations: new PolicyPreparationSource(options.store, reader, loaded, current),
      ledger: new FileHostedExecutionLedger(join(loaded.runtime, "hosted-executions")),
      createProvider: createPolicyProviderFactory({current, budget, promptAllowed: prompt => !config.allowedPromptHashes || config.allowedPromptHashes.includes(digest(prompt)), resolveCredential, transport: options.transport, now}),
      drafts: new FileHostedDraftSink(roots), review: new FileHostedDraftReview(roots),
    }, workspaceId => current() && config.allowedWorkspaceIds.includes(workspaceId));
    return {application: app, disabledReason: "hosted_disabled"};
  } catch (error) {
    return {disabledReason: error instanceof HostedRuntimeConfigError ? error.code : "hosted_runtime_unavailable"};
  }
}
