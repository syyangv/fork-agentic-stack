import { ProviderRunner, type OpenCodeCredential, type ProviderRunResult } from "./provider-runner.js";
import { HostedDisclosurePolicySchema, type HostedDisclosurePolicy } from "./hosted-task-policy.js";
import type { HostedProvider, HostedProviderAuthorization } from "./hosted-task.js";

/** Internal adapter: no default credential resolver and no plugin registration. */
export function createHostedProviderFactory(options: {
  readonly resolveCredential: (signal?: AbortSignal) => Promise<OpenCodeCredential | null>;
  readonly transport?: typeof fetch;
  readonly now?: () => number;
}): (policy: HostedDisclosurePolicy, authorize: HostedProviderAuthorization) => HostedProvider {
  const resolveCredential = options.resolveCredential;
  const transport = options.transport ?? fetch;
  const now = options.now ?? Date.now;
  const rejected = (code: "not_configured" | "cancelled" | "source_invalidated"): ProviderRunResult => ({ status: "failed",
    error: { code, message: "The hosted provider is unavailable or the request was cancelled.", retryable: false } });
  return (configured, authorize) => ({
    async run(request) {
      const policy = HostedDisclosurePolicySchema.safeParse(configured);
      if (!policy.success) return rejected("not_configured");
      if (request.signal?.aborted) return rejected("cancelled");
      let credential;
      try { credential = await resolveCredential(request.signal); } catch { return rejected("not_configured"); }
      if (request.signal?.aborted) return rejected("cancelled");
      const validCredential = () => credential !== null && credential.token.trim().length > 0
        && (credential.kind !== "oauth" || (Number.isFinite(credential.expiresAt) && credential.expiresAt! > now() + 60_000));
      if (!validCredential()) return rejected("not_configured");
      try {
        if (!await authorize(request.signal)) return rejected("source_invalidated");
      } catch { return rejected("source_invalidated"); }
      if (request.signal?.aborted) return rejected("cancelled");
      if (!validCredential()) return rejected("not_configured");
      return new ProviderRunner({ endpoint: policy.data.endpoint, model: policy.data.model, apiKey: credential!.token,
        maxInputChars: policy.data.maxInputChars, maxOutputChars: policy.data.maxOutputChars,
        maxTokens: policy.data.maxTokens, timeoutMs: policy.data.timeoutMs }, transport).run(request);
    },
  });
}
