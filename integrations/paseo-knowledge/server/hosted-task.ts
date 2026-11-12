import { randomUUID } from "node:crypto";
import type { ProviderRunRequest, ProviderRunResult } from "./provider-runner.js";
import type { HostedExecutionLedger, HostedLedgerOutcome, HostedReservation } from "./hosted-execution-ledger.js";
import {
  digest, HostedDisclosurePolicySchema, prepareHostedInput,
  type HostedDisclosurePolicy, type HostedTaskInput, type PreparedHostedInput, type HostedPreparationRegistry, type HostedTaskBinding,
} from "./hosted-task-policy.js";

export interface HostedDraftCompletion {
  readonly execution: HostedReservation;
  readonly completionId: string;
  readonly model: string;
  readonly output: string;
  readonly outputDigest: string;
  readonly sources: PreparedHostedInput["binding"]["sources"];
}
export interface HostedDraftReceipt {
  readonly draftId: string;
  readonly executionId: string;
  readonly outputDigest: string;
}
// A trusted private-draft capability. Production must use an atomic, cancellation-aware sink.
export interface HostedDraftSink {
  capture(completion: HostedDraftCompletion, signal: AbortSignal): Promise<HostedDraftReceipt>;
}
export type HostedProviderAuthorization = (signal?: AbortSignal) => Promise<boolean>;
export interface HostedProvider {
  run(request: ProviderRunRequest): Promise<ProviderRunResult>;
}
export type HostedTaskResult =
  | { readonly status: "completed"; readonly executionId: string; readonly completionId: string; readonly draftId: string }
  | { readonly status: "failed" | "cancelled" | "timeout" | "ambiguous"; readonly executionId?: string;
      readonly error: { readonly code: string; readonly retryable: false } };
const fail = (code: string, executionId?: string, status: "failed" | "cancelled" | "timeout" | "ambiguous" = "failed"): HostedTaskResult =>
  ({ status, ...(executionId ? { executionId } : {}), error: { code, retryable: false } });
const safeId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(value);

/** Offline integration seam. Not imported by index.server.ts or KnowledgeTaskService. */
export class HostedTaskCoordinator {
  private readonly policy: HostedDisclosurePolicy | null;
  private readonly active = new Map<string, AbortController>();
  private readonly now: () => number;
  public constructor(private readonly options: {
    readonly policy?: HostedDisclosurePolicy;
    readonly preparations: HostedPreparationRegistry;
    readonly ledger: HostedExecutionLedger;
    readonly createProvider: (policy: HostedDisclosurePolicy, authorize: HostedProviderAuthorization) => Promise<HostedProvider> | HostedProvider;
    readonly checkSources: (binding: HostedTaskBinding, signal?: AbortSignal) => Promise<boolean>;
    readonly drafts: HostedDraftSink;
    readonly now?: () => number;
  }) {
    const parsed = HostedDisclosurePolicySchema.safeParse(options.policy);
    this.policy = parsed.success ? Object.freeze(parsed.data) : null;
    this.now = options.now ?? Date.now;
  }
  public cancel(taskId: string): boolean {
    const controller = this.active.get(taskId);
    if (!controller) return false;
    controller.abort();
    return true;
  }
  public async start(input: HostedTaskInput): Promise<HostedTaskResult> {
    const policy = this.policy;
    if (!policy) return fail("not_configured");
    const checked = prepareHostedInput(policy, input, this.now());
    if ("error" in checked) return fail(checked.error);
    if (input.signal?.aborted) return fail("cancelled", undefined, "cancelled");
    if (this.active.size) return fail("busy");
    const prepared = checked.input;
    if (!this.options.preparations.consume(input.approvalId, policy, prepared, this.now())) return fail("approval_required");
    const reservation: HostedReservation = {
      taskId: prepared.binding.taskId, workspaceId: prepared.binding.workspaceId,
      provider: prepared.binding.provider, executionId: randomUUID(),
      inputDigest: digest(JSON.stringify({ policy, prepared })),
      policyId: policy.policyId, policyVersion: policy.version, createdAt: this.now(),
    };
    const controller = new AbortController();
    const abort = () => controller.abort();
    input.signal?.addEventListener("abort", abort, { once: true });
    this.active.set(reservation.taskId, controller);
    const pending = new Set<Promise<unknown>>();
    const track = <T>(operation: Promise<T>) => {
      pending.add(operation);
      operation.then(() => pending.delete(operation), () => pending.delete(operation));
      return operation;
    };
    let timedOut = false;
    let reserved = false;
    let submitted = false;
    const deadline = performance.now() + policy.timeoutMs;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, policy.timeoutMs);
    const aborted = new Promise<"aborted">((resolve) => controller.signal.addEventListener("abort", () => resolve("aborted"), { once: true }));
    const stopped = () => {
      if (!controller.signal.aborted && performance.now() >= deadline) { timedOut = true; controller.abort(); }
      return controller.signal.aborted;
    };
    const cancelled = () => fail(submitted ? "submission_unknown" : timedOut ? "timeout" : "cancelled",
      reservation.executionId, timedOut ? "timeout" : "cancelled");
    const finish = async (outcome: HostedLedgerOutcome): Promise<HostedTaskResult> => {
      try {
        const recorded = await Promise.race([track(this.options.ledger.finish(reservation, outcome)), aborted]);
        if (recorded === "aborted" || stopped()) return cancelled();
      } catch { return fail("storage_unavailable", reservation.executionId); }
      return fail(outcome.code ?? "provider_error", reservation.executionId,
        outcome.status === "cancelled" || outcome.status === "timeout" || outcome.status === "ambiguous" ? outcome.status : "failed");
    };
    try {
      try {
        const claim = await Promise.race([track(this.options.ledger.reserve(reservation)), aborted]);
        if (claim === "aborted" || stopped()) return cancelled();
        reserved = claim;
      } catch { return fail("storage_unavailable"); }
      if (!reserved) return fail("already_submitted");
      const authorize: HostedProviderAuthorization = async (signal) => {
        if (stopped() || signal?.aborted || "error" in prepareHostedInput(policy, { ...prepared }, this.now())) return false;
        try {
          const current = await this.options.checkSources(prepared.binding, signal);
          return current && !stopped() && !signal?.aborted
            && !("error" in prepareHostedInput(policy, { ...prepared }, this.now()));
        } catch { return false; }
      };
      const sourceCheck = await Promise.race([track(authorize(controller.signal)), aborted]);
      if (sourceCheck === "aborted" || stopped()) return cancelled();
      if (!sourceCheck) return await finish({ status: "failed", code: "source_invalidated" });
      const providerPending = track(Promise.resolve().then(async () => {
        if (stopped()) return "aborted" as const;
        const provider = await this.options.createProvider(policy, authorize);
        if (stopped()) return "aborted" as const;
        const rechecked = prepareHostedInput(policy, { ...prepared }, this.now());
        if ("error" in rechecked) return { preparationError: rechecked.error } as const;
        submitted = true;
        return provider.run({ prompt: prepared.prompt,
          sources: prepared.binding.sources.map((item) => ({ path: item.sourceId, text: item.text })),
          signal: controller.signal, sessionId: reservation.executionId });
      }));
      const result = await Promise.race([providerPending, aborted]);
      if (result === "aborted" || stopped()) return cancelled();
      if ("preparationError" in result) return await finish({ status: "failed", code: result.preparationError });
      if (result.status === "failed") {
        const unknown = result.error.code === "timeout" || result.error.code === "cancelled";
        return await finish({ status: unknown ? "ambiguous" : "failed", code: unknown ? "submission_unknown" : result.error.code });
      }
      if (!safeId(result.completionId) || result.model !== policy.model) {
        return await finish({ status: "capture_incomplete", code: "capture_incomplete" });
      }
      if (!result.output.trim() || result.output.length > policy.maxOutputChars) {
        return await finish({ status: "failed", code: "invalid_output" });
      }
      const captureSourceCheck = await Promise.race([track(authorize(controller.signal)), aborted]);
      if (captureSourceCheck === "aborted" || stopped()) return fail("capture_incomplete", reservation.executionId);
      if (!captureSourceCheck) return await finish({ status: "failed", code: "source_invalidated" });
      const completion: HostedDraftCompletion = {
        execution: reservation, completionId: result.completionId, model: result.model,
        output: result.output, outputDigest: digest(result.output), sources: prepared.binding.sources,
      };
      let receipt;
      try {
        receipt = await Promise.race([track(this.options.drafts.capture(completion, controller.signal)), aborted]);
      } catch { return await finish({ status: "capture_incomplete", code: "capture_incomplete" }); }
      if (receipt === "aborted" || stopped() || !safeId(receipt.draftId)
        || receipt.executionId !== reservation.executionId || receipt.outputDigest !== completion.outputDigest) {
        return fail("capture_incomplete", reservation.executionId);
      }
      try {
        const saved = await Promise.race([track(this.options.ledger.finish(reservation, { status: "completed", completionId: completion.completionId, draftId: receipt.draftId })), aborted]);
        if (saved === "aborted" || stopped()) return fail("capture_incomplete", reservation.executionId);
      } catch { return fail("capture_incomplete", reservation.executionId); }
      return { status: "completed", executionId: reservation.executionId, completionId: completion.completionId, draftId: receipt.draftId };
    } catch {
      return reserved ? await finish({ status: "ambiguous", code: "submission_unknown" }) : fail("storage_unavailable");
    } finally {
      clearTimeout(timer);
      input.signal?.removeEventListener("abort", abort);
      if (!pending.size) this.active.delete(reservation.taskId);
      else Promise.allSettled([...pending]).then(() => this.active.delete(reservation.taskId));
    }
  }
}
