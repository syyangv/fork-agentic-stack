import { randomUUID } from "node:crypto";
import type { PaseoApi } from "@getpaseo/client";
import type { HostedConsentPreview } from "../shared/hosted-contracts.js";
import { HostedTaskCoordinator, type HostedDraftSink, type HostedProvider, type HostedProviderAuthorization } from "./hosted-task.js";
import { digest, HostedDisclosurePolicySchema, HostedPreparationRegistry, prepareHostedInput, type HostedDisclosurePolicy, type HostedTaskInput } from "./hosted-task-policy.js";
import { HostedPreparationError, type StoredHostedPreparation, type StoredHostedPreparationSource } from "./hosted-preparation.js";
import type { HostedExecutionLedger } from "./hosted-execution-ledger.js";
import type { HostedDraftReview } from "./hosted-draft-review.js";

type Access = Pick<PaseoApi, "workspaces">;
interface HeldPreview { readonly view: HostedConsentPreview; readonly preparation: StoredHostedPreparation; readonly input: HostedTaskInput; }
const failure = (code: string) => ({ code, retryable: false as const });
const errorCode = (error: unknown) => error instanceof HostedPreparationError ? error.code : "hosted_unavailable";

/** Single-owner daemon session boundary, not per-user/workspace ACL enforcement. */
export class HostedKnowledgeApplication {
  private readonly policy: HostedDisclosurePolicy;
  private readonly approvals = new HostedPreparationRegistry();
  private readonly previews = new Map<string, HeldPreview>();
  private readonly active = new Map<string, StoredHostedPreparation>();
  private readonly executing = new Map<string, { readonly workspaceId: string; readonly controller: AbortController }>();
  private readonly verifying = new Set<AbortController>();
  private readonly previewing = new Set<string>();
  private readonly coordinator: HostedTaskCoordinator;
  private readonly now: () => number;
  private closed = false;
  public constructor(private readonly options: {
    readonly policy: HostedDisclosurePolicy; readonly preparations: StoredHostedPreparationSource;
    readonly ledger: HostedExecutionLedger; readonly createProvider: (policy: HostedDisclosurePolicy, authorize: HostedProviderAuthorization) => HostedProvider | Promise<HostedProvider>;
    readonly drafts: HostedDraftSink; readonly review: HostedDraftReview; readonly now?: () => number;
  }) {
    this.policy = Object.freeze(HostedDisclosurePolicySchema.parse(options.policy));
    this.now = options.now ?? Date.now;
    this.coordinator = new HostedTaskCoordinator({ policy: this.policy, preparations: this.approvals,
      ledger: options.ledger, createProvider: options.createProvider, drafts: options.drafts, now: this.now,
      checkSources: (binding, signal) => {
        const prepared = this.active.get(binding.taskId);
        return prepared ? options.preparations.validate(prepared, binding, signal) : Promise.resolve(false);
      } });
  }
  private async workspace(access: Access, workspaceId: string, pending?: Set<Promise<unknown>>, signal?: AbortSignal): Promise<boolean> {
    if (this.closed || signal?.aborted) return false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const refresh = access.workspaces.ref(workspaceId).refresh();
      pending?.add(refresh);
      refresh.then(() => pending?.delete(refresh), () => pending?.delete(refresh));
      const workspace = await Promise.race([refresh,
        new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), this.policy.timeoutMs); }),
        new Promise<null>((resolve) => signal?.addEventListener("abort", () => resolve(null), { once: true }))]);
      return !this.closed && workspace?.id === workspaceId && String(workspace.status) !== "archived";
    } catch { return false; }
    finally { if (timer) clearTimeout(timer); }
  }
  public async availability(workspaceId: string, access: Access) {
    const available = await this.workspace(access, workspaceId);
    return { available, error: available ? null : failure("workspace_unavailable") };
  }
  public async preview(input: { taskId: string; workspaceId: string; prompt: string }, access: Access) {
    if (this.closed) return { preview: null, error: failure("hosted_disabled") };
    if (this.previewing.size >= 2 || this.previewing.has(input.taskId)) return { preview: null, error: failure("busy") };
    for (const [key, previous] of this.previews) if (previous.view.expiresAt <= this.now()) this.previews.delete(key);
    if (this.previews.size >= 32 && ![...this.previews.values()].some((held) => held.view.taskId === input.taskId)) return { preview: null, error: failure("busy") };
    const controller = new AbortController(); this.verifying.add(controller); this.previewing.add(input.taskId);
    const pending = new Set<Promise<unknown>>();
    const timer = setTimeout(() => controller.abort(), this.policy.timeoutMs);
    try {
      if (!await this.workspace(access, input.workspaceId, pending, controller.signal)) return { preview: null, error: failure("workspace_unavailable") };
      const preparation = this.options.preparations.snapshot(input.taskId, input.workspaceId);
      const task: HostedTaskInput = { binding: preparation.binding, prompt: input.prompt,
        expected: { taskId: preparation.binding.taskId, workspaceId: preparation.binding.workspaceId, provider: preparation.binding.provider } };
      const checked = prepareHostedInput(this.policy, task, this.now());
      if ("error" in checked) return { preview: null, error: failure(checked.error) };
      const verification = this.options.preparations.validate(preparation, task.binding, controller.signal);
      pending.add(verification);
      verification.then(() => pending.delete(verification), () => pending.delete(verification));
      const valid = await Promise.race([verification,
        new Promise<false>((resolve) => controller.signal.addEventListener("abort", () => resolve(false), { once: true }))]);
      if (!valid || controller.signal.aborted || this.closed) return { preview: null, error: failure("source_invalidated") };
      const now = this.now();
      const expiresAt = preparation.binding.preparedAt + this.policy.freshnessMs;
      if (expiresAt <= now) return { preview: null, error: failure("source_invalidated") };
      for (const [key, previous] of this.previews) if (previous.view.expiresAt <= now || previous.view.taskId === input.taskId) this.previews.delete(key);
      if (this.previews.size >= 32) return { preview: null, error: failure("busy") };
      const view: HostedConsentPreview = {
        previewId: randomUUID(), challenge: randomUUID(), payloadDigest: digest(JSON.stringify({ policy: this.policy, input: checked.input, revision: preparation.revision })),
        expiresAt, taskId: input.taskId, workspaceId: input.workspaceId, provider: preparation.binding.provider, prompt: checked.input.prompt,
        policy: { policyId: this.policy.policyId, version: this.policy.version, endpoint: this.policy.endpoint, model: this.policy.model, disclosure: this.policy.disclosure },
        sources: checked.input.binding.sources.map((source, index) => ({ ...source,
          lineStart: preparation.refs[index]!.line_start, lineEnd: preparation.refs[index]!.line_end })),
      };
      this.previews.set(view.previewId, { view: structuredClone(view), preparation, input: checked.input });
      return { preview: view, error: null };
    } catch (error) { return { preview: null, error: failure(errorCode(error)) }; }
    finally {
      clearTimeout(timer);
      const release = () => { this.verifying.delete(controller); this.previewing.delete(input.taskId); };
      if (!pending.size) release(); else Promise.allSettled([...pending]).then(release);
    }
  }
  public async execute(input: { taskId: string; workspaceId: string; previewId: string; challenge: string; payloadDigest: string }, access: Access) {
    if (this.closed) return { result: null, error: failure("hosted_disabled") };
    const held = this.previews.get(input.previewId);
    if (!held || held.view.taskId !== input.taskId || held.view.workspaceId !== input.workspaceId
      || held.view.challenge !== input.challenge || held.view.payloadDigest !== input.payloadDigest || held.view.expiresAt <= this.now()) {
      return { result: null, error: failure("consent_required") };
    }
    if (this.executing.size) return { result: null, error: failure("busy") };
    this.previews.delete(input.previewId);
    const controller = new AbortController();
    const pending = new Set<Promise<unknown>>();
    this.executing.set(input.taskId, { workspaceId: input.workspaceId, controller });
    try {
      if (!await this.workspace(access, input.workspaceId, pending, controller.signal)) {
        return { result: null, error: failure(controller.signal.aborted ? "cancelled" : "workspace_unavailable") };
      }
      if (controller.signal.aborted || this.closed) return { result: null, error: failure("cancelled") };
      if (this.options.preparations.snapshot(input.taskId, input.workspaceId).revision !== held.preparation.revision) {
        return { result: null, error: failure("source_invalidated") };
      }
      const approvalId = this.approvals.approve(this.policy, held.input, this.now());
      this.active.set(input.taskId, held.preparation);
      const result = await this.coordinator.start({ ...held.input, approvalId, signal: controller.signal });
      return { result, error: null };
    } catch (error) { return { result: null, error: failure(errorCode(error)) }; }
    finally {
      this.active.delete(input.taskId);
      const release = () => this.executing.delete(input.taskId);
      if (!pending.size) release(); else Promise.allSettled([...pending]).then(release);
    }
  }
  public async cancel(input: { taskId: string; workspaceId: string }, _access: Access) {
    const execution = this.executing.get(input.taskId);
    if (!execution || execution.workspaceId !== input.workspaceId) return { cancelled: false, error: failure("task_identity_conflict") };
    execution.controller.abort();
    this.coordinator.cancel(input.taskId);
    return { cancelled: true, error: null };
  }
  public async list(workspaceId: string, access: Access) {
    if (!await this.workspace(access, workspaceId)) return { drafts: [], unreadableCount: 0, error: failure("workspace_unavailable") };
    try { return { ...await this.options.review.list(workspaceId), error: null }; }
    catch { return { drafts: [], unreadableCount: 0, error: failure("draft_unavailable") }; }
  }
  public async read(input: { workspaceId: string; draftId: string }, access: Access) {
    if (!await this.workspace(access, input.workspaceId)) return { draft: null, error: failure("workspace_unavailable") };
    try { return { draft: await this.options.review.read(input.workspaceId, input.draftId), error: null }; }
    catch { return { draft: null, error: failure("draft_unavailable") }; }
  }
  public async reject(input: { workspaceId: string; draftId: string; outputDigest: string }, access: Access) {
    if (!await this.workspace(access, input.workspaceId)) return { rejected: false, error: failure("workspace_unavailable") };
    try { await this.options.review.reject(input.workspaceId, input.draftId, input.outputDigest); return { rejected: true, error: null }; }
    catch { return { rejected: false, error: failure("draft_unavailable") }; }
  }
  public dispose(): void {
    this.closed = true; this.previews.clear();
    for (const controller of this.verifying) controller.abort();
    for (const execution of this.executing.values()) execution.controller.abort();
    for (const taskId of this.active.keys()) this.coordinator.cancel(taskId);
  }
}
