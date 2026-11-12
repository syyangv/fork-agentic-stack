/**
 * Offline runner-contract prototype for knowledge task execution.
 *
 * This module is deliberately NOT registered with the plugin and has no
 * network, provider, or filesystem access. It pins the orchestration contract
 * before any real runner is considered: deny by default, explicit versioned
 * attestation, identity and freshness checks, bounded input/output, a single
 * submission, cancellation, timeout, and ambiguous-completion handling with no
 * automatic replay.
 *
 * It does NOT prove OS or provider isolation. A real runner needs its own
 * threat model and an independently verified enforcement mechanism.
 */
import { randomUUID } from "node:crypto";

export type RunnerFailureCode =
  | "tool_isolation_unavailable"
  | "task_identity_conflict"
  | "source_invalidated"
  | "needs_prepare"
  | "personal_access_unavailable"
  | "input_limit_exceeded"
  | "output_limit_exceeded"
  | "timeout"
  | "cancelled"
  | "busy"
  | "submission_unknown"
  | "invalid_output"
  | "capture_incomplete"
  | "agent_failed";

export interface RunnerFailure {
  readonly code: RunnerFailureCode;
  readonly message: string;
  readonly retryable: boolean;
}

/**
 * A versioned capability attestation tied to the launched implementation.
 * Only an explicitly attested runner with no tools and no network may receive
 * untrusted vault text. There is no default and no caller-supplied boolean
 * that can grant capability.
 */
export interface RunnerAttestation {
  readonly runnerId: string;
  readonly version: string;
  readonly attested: true;
  readonly tools: "none";
  readonly network: "none";
  readonly maxInputChars: number;
  readonly maxOutputChars: number;
  readonly maxTimeoutMs: number;
}

export interface RunnerLimits {
  readonly maxInputChars: number;
  readonly maxOutputChars: number;
  readonly maxTimeoutMs: number;
  readonly maxConcurrency: number;
  readonly freshnessMs: number;
}

export interface RunnerSource {
  readonly sourceId: string;
  readonly path: string;
  readonly contentHash: string;
  /** Untrusted source text. Never logged or persisted by this contract. */
  readonly text: string;
}

export interface RunnerTaskBinding {
  readonly taskId: string;
  readonly workspaceId: string;
  readonly provider: string;
  readonly vaultScope: "work" | "personal";
  readonly preparedAt: number;
  readonly sources: readonly RunnerSource[];
}

export interface RunnerExpectedIdentity {
  readonly taskId: string;
  readonly workspaceId: string;
  readonly provider: string;
}

export interface RunnerStartInput {
  readonly binding: RunnerTaskBinding;
  readonly expected: RunnerExpectedIdentity;
  readonly prompt: string;
  readonly runnerId: string;
  readonly runnerVersion: string;
  readonly requestId?: string;
  readonly timeoutMs?: number;
}

export interface RunnerRequest {
  readonly requestId: string;
  readonly taskId: string;
  readonly workspaceId: string;
  readonly provider: string;
  readonly prompt: string;
  readonly sources: readonly RunnerSource[];
  readonly maxOutputChars: number;
}

export type RunnerBackendResult =
  | { readonly kind: "completed"; readonly completionId: string; readonly output: string }
  | { readonly kind: "rejected"; readonly code: RunnerFailureCode; readonly message: string; readonly retryable?: boolean }
  | { readonly kind: "ambiguous"; readonly message?: string }
  | { readonly kind: "cancelled" }
  | { readonly kind: "timeout" };

export interface RunnerBackend {
  run(request: RunnerRequest, context: { readonly signal: AbortSignal }): Promise<RunnerBackendResult>;
}

export type RunnerOutcome =
  | {
    readonly status: "completed";
    readonly requestId: string;
    readonly completionId: string;
    readonly output: string;
  }
  | {
    readonly status: "failed" | "cancelled" | "timeout" | "ambiguous";
    readonly requestId: string;
    readonly error: RunnerFailure;
  };

/** Non-sensitive audit event. Never contains source text, prompt, or output. */
export interface RunnerAuditEvent {
  readonly event: "runner_submitted";
  readonly requestId: string;
  readonly taskId: string;
  readonly sourceCount: number;
  readonly inputChars: number;
  readonly status: RunnerOutcome["status"];
  readonly code?: RunnerFailureCode;
  readonly outputChars?: number;
}

export type RunnerAuditSink = (event: RunnerAuditEvent) => void;

export class RunnerRegistry {
  private readonly entries = new Map<string, { attestation: RunnerAttestation; backend: RunnerBackend }>();

  public register(attestation: RunnerAttestation, backend: RunnerBackend): void {
    if (attestation.attested !== true || attestation.tools !== "none" || attestation.network !== "none") {
      throw new Error("Runner attestation must be explicit, tool-free, and network-free.");
    }
    if (!attestation.runnerId || !attestation.version) {
      throw new Error("Runner attestation requires an id and a version.");
    }
    this.entries.set(attestation.runnerId, { attestation, backend });
  }

  public select(runnerId: string, version: string): { attestation: RunnerAttestation; backend: RunnerBackend } | null {
    const entry = this.entries.get(runnerId);
    if (!entry || entry.attestation.version !== version) return null;
    return entry;
  }
}

interface ActiveRequest {
  readonly controller: AbortController;
  cancelled: boolean;
  timedOut: boolean;
}

const failure = (
  requestId: string,
  code: RunnerFailureCode,
  message: string,
  retryable = false,
): RunnerOutcome => ({ status: "failed", requestId, error: { code, message, retryable } });

export class RunnerContract {
  private readonly registry: RunnerRegistry;
  private readonly limits: RunnerLimits;
  private readonly now: () => number;
  private readonly audit?: RunnerAuditSink;
  private readonly active = new Map<string, ActiveRequest>();

  public constructor(options: {
    registry: RunnerRegistry;
    limits: RunnerLimits;
    now?: () => number;
    audit?: RunnerAuditSink;
  }) {
    this.registry = options.registry;
    this.limits = options.limits;
    this.now = options.now ?? (() => Date.now());
    this.audit = options.audit;
  }

  public cancel(requestId: string): boolean {
    const record = this.active.get(requestId);
    if (!record) return false;
    record.cancelled = true;
    record.controller.abort();
    return true;
  }

  public async start(input: RunnerStartInput): Promise<RunnerOutcome> {
    const requestId = input.requestId ?? randomUUID();
    const binding = input.binding;

    if (binding.vaultScope !== "work") {
      return failure(requestId, "personal_access_unavailable", "Personal sources require a supervised grant transport.");
    }
    if (
      binding.taskId !== input.expected.taskId
      || binding.workspaceId !== input.expected.workspaceId
      || binding.provider !== input.expected.provider
    ) {
      return failure(requestId, "task_identity_conflict", "Runner start must use the prepared task identity.");
    }
    if (binding.sources.length === 0) {
      return failure(requestId, "needs_prepare", "Prepare a bounded work-source manifest before starting.");
    }
    const now = this.now();
    if (binding.preparedAt > now || now - binding.preparedAt > this.limits.freshnessMs) {
      return failure(requestId, "source_invalidated", "The prepared source manifest is stale.");
    }
    if (binding.sources.some((source) => !/^[0-9a-f]{64}$/i.test(source.contentHash))) {
      return failure(requestId, "source_invalidated", "The prepared source manifest contains an invalid hash.");
    }

    const selected = this.registry.select(input.runnerId, input.runnerVersion);
    if (!selected) {
      return failure(
        requestId,
        "tool_isolation_unavailable",
        "No attested tool-free runner is available for untrusted indexed vault text.",
      );
    }

    const maxInputChars = Math.min(this.limits.maxInputChars, selected.attestation.maxInputChars);
    const inputChars = binding.sources.reduce((total, source) => total + source.text.length, 0) + input.prompt.length;
    if (inputChars > maxInputChars) {
      return failure(requestId, "input_limit_exceeded", "The bounded request exceeds the configured input limit.");
    }

    const maxOutputChars = Math.min(this.limits.maxOutputChars, selected.attestation.maxOutputChars);
    const timeoutMs = Math.min(
      input.timeoutMs ?? this.limits.maxTimeoutMs,
      this.limits.maxTimeoutMs,
      selected.attestation.maxTimeoutMs,
    );
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      return failure(requestId, "agent_failed", "The runner timeout is invalid.");
    }

    if (this.active.size >= this.limits.maxConcurrency) {
      return failure(requestId, "busy", "The runner is at its concurrency limit; retry after the current request finishes.", true);
    }

    const request: RunnerRequest = {
      requestId,
      taskId: binding.taskId,
      workspaceId: binding.workspaceId,
      provider: binding.provider,
      prompt: input.prompt,
      sources: binding.sources,
      maxOutputChars,
    };

    const record: ActiveRequest = { controller: new AbortController(), cancelled: false, timedOut: false };
    this.active.set(requestId, record);
    const timer = setTimeout(() => {
      record.timedOut = true;
      record.controller.abort();
    }, timeoutMs);

    let outcome: RunnerOutcome;
    try {
      const backendPromise = Promise.resolve().then(() => selected.backend.run(request, { signal: record.controller.signal }));
      // A backend that rejects after an abort must not surface as unhandled.
      backendPromise.catch(() => undefined);
      const abortPromise = new Promise<"cancelled" | "timeout">((resolve) => {
        record.controller.signal.addEventListener(
          "abort",
          () => resolve(record.cancelled ? "cancelled" : "timeout"),
          { once: true },
        );
      });
      const raced = await Promise.race([backendPromise, abortPromise]);
      if (raced === "cancelled") {
        outcome = {
          status: "cancelled",
          requestId,
          error: { code: "cancelled", message: "The runner request was cancelled.", retryable: false },
        };
      } else if (raced === "timeout") {
        outcome = {
          status: "timeout",
          requestId,
          error: { code: "timeout", message: "The runner did not finish before its deadline.", retryable: false },
        };
      } else {
        outcome = this.readResult(requestId, raced, maxOutputChars);
      }
    } catch {
      outcome = failure(requestId, "agent_failed", "The runner failed; the submission is not retried automatically.");
    } finally {
      clearTimeout(timer);
      this.active.delete(requestId);
    }

    this.audit?.({
      event: "runner_submitted",
      requestId,
      taskId: binding.taskId,
      sourceCount: binding.sources.length,
      inputChars,
      status: outcome.status,
      ...(outcome.status === "completed"
        ? { outputChars: outcome.output.length }
        : { code: outcome.error.code }),
    });
    return outcome;
  }

  private readResult(requestId: string, result: RunnerBackendResult, maxOutputChars: number): RunnerOutcome {
    if (result.kind === "ambiguous") {
      return {
        status: "ambiguous",
        requestId,
        error: {
          code: "submission_unknown",
          message: "The runner did not confirm whether the submission was accepted; do not retry automatically.",
          retryable: false,
        },
      };
    }
    if (result.kind === "cancelled") {
      return {
        status: "cancelled",
        requestId,
        error: { code: "cancelled", message: "The runner request was cancelled.", retryable: false },
      };
    }
    if (result.kind === "timeout") {
      return {
        status: "timeout",
        requestId,
        error: { code: "timeout", message: "The runner did not finish before its deadline.", retryable: false },
      };
    }
    if (result.kind === "rejected") {
      return {
        status: "failed",
        requestId,
        error: { code: result.code, message: result.message, retryable: result.retryable ?? false },
      };
    }
    if (typeof result.output !== "string") {
      return failure(requestId, "invalid_output", "The runner returned a non-text result.");
    }
    if (!result.completionId) {
      return failure(requestId, "capture_incomplete", "The runner completion has no verified identity.");
    }
    if (result.output.length > maxOutputChars) {
      return failure(requestId, "output_limit_exceeded", "The runner output exceeds the configured limit.");
    }
    return { status: "completed", requestId, completionId: result.completionId, output: result.output };
  }
}
