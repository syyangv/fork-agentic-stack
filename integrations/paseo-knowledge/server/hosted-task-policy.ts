import { createHash, randomUUID } from "node:crypto";
import { z } from "zod";
import { posix, win32 } from "node:path";
import type { RunnerExpectedIdentity, RunnerTaskBinding, RunnerSource } from "./runner-contract.js";

export const HostedDisclosurePolicySchema = z.object({
  policyId: z.string().regex(/^[A-Za-z0-9._:-]{1,128}$/),
  version: z.string().regex(/^[A-Za-z0-9._:-]{1,128}$/),
  endpoint: z.literal("https://opencode.ai/zen/go/v1"),
  model: z.string().regex(/^[A-Za-z0-9._:-]{1,128}$/),
  vaultScope: z.literal("work"),
  tools: z.literal("none"),
  network: z.literal("opencode-provider-api"),
  disclosure: z.literal("work-context-to-opencode-and-upstream"),
  maxInputChars: z.number().int().positive().max(65_536),
  maxOutputChars: z.number().int().positive().max(65_536),
  maxTokens: z.number().int().positive().max(4_096),
  timeoutMs: z.number().int().positive().max(120_000),
  freshnessMs: z.number().int().positive().max(300_000),
}).strict();

export type HostedDisclosurePolicy = Readonly<z.infer<typeof HostedDisclosurePolicySchema>>;
export interface HostedSource extends RunnerSource { readonly excerptHash: string; }
export interface HostedTaskBinding extends Omit<RunnerTaskBinding, "sources"> { readonly sources: readonly HostedSource[]; }
export interface HostedTaskInput {
  readonly binding: HostedTaskBinding;
  readonly expected: RunnerExpectedIdentity;
  readonly prompt: string;
  readonly excludedSourceIds?: readonly string[];
  readonly signal?: AbortSignal;
  readonly approvalId?: string;
}

const id = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$/);
const identity = z.object({ taskId: id, workspaceId: id, provider: id });
const source = z.object({
  sourceId: id,
  path: z.string().min(1).max(1_024).refine((value) => !/[\x00-\x1f]/.test(value)
    && !posix.isAbsolute(value) && !win32.isAbsolute(value) && !value.includes("\\")
    && posix.normalize(value) === value && !value.split("/").includes("..") && value !== "."),
  contentHash: z.string().regex(/^[a-f0-9]{64}$/i).transform((value) => value.toLowerCase()),
  excerptHash: z.string().regex(/^[a-f0-9]{64}$/i).transform((value) => value.toLowerCase()),
  text: z.string().max(65_536),
});
const MAX_HOSTED_SOURCES = 64;
const inputSchema = z.object({
  binding: identity.extend({
    vaultScope: z.enum(["work", "personal"]),
    preparedAt: z.number().int().nonnegative(),
    sources: z.array(source).min(1).max(MAX_HOSTED_SOURCES),
  }),
  expected: identity,
  prompt: z.string().min(1).max(65_536),
  excludedSourceIds: z.array(id).max(MAX_HOSTED_SOURCES).default([]),
});
export type PreparedHostedInput = z.infer<typeof inputSchema>;
export type HostedPreparationError = "needs_prepare" | "personal_access_unavailable" | "task_identity_conflict" | "source_invalidated" | "input_limit_exceeded";
export const digest = (value: string) => createHash("sha256").update(value).digest("hex");

export function prepareHostedInput(policy: HostedDisclosurePolicy, input: HostedTaskInput, now: number):
  { readonly input: PreparedHostedInput } | { readonly error: HostedPreparationError } {
  // Zod collection limits need not prevent element parsing; admit counts first.
  const sources = input?.binding?.sources;
  const exclusions = input?.excludedSourceIds;
  if (!Array.isArray(sources) || sources.length < 1 || sources.length > MAX_HOSTED_SOURCES
    || (exclusions !== undefined && (!Array.isArray(exclusions) || exclusions.length > MAX_HOSTED_SOURCES))) {
    return { error: "needs_prepare" };
  }
  const parsed = inputSchema.safeParse(input);
  if (!parsed.success) return { error: "needs_prepare" };
  const prepared = parsed.data;
  const { binding, expected } = prepared;
  if (binding.vaultScope !== "work") return { error: "personal_access_unavailable" };
  if (binding.taskId !== expected.taskId || binding.workspaceId !== expected.workspaceId
    || binding.provider !== expected.provider || binding.provider !== `opencode-go/${policy.model}`) {
    return { error: "task_identity_conflict" };
  }
  if (binding.preparedAt > now || now - binding.preparedAt > policy.freshnessMs
    || new Set(binding.sources.map((item) => item.sourceId)).size !== binding.sources.length
    || binding.sources.some((item) => prepared.excludedSourceIds.includes(item.sourceId)
      || digest(item.text) !== item.excerptHash.toLowerCase())) {
    return { error: "source_invalidated" };
  }
  const payload = JSON.stringify({ prompt: prepared.prompt, sources: binding.sources });
  if (payload.length > policy.maxInputChars || Buffer.byteLength(payload, "utf8") > 65_536) {
    return { error: "input_limit_exceeded" };
  }
  return { input: prepared };
}

// Owned by trusted preparation/approval code, never exposed as a client RPC.
// An approval covers the exact prompt, manifest, exclusions, model and disclosure policy.
export class HostedPreparationRegistry {
  private readonly approvals = new Map<string, { readonly digest: string; readonly expiresAt: number }>();
  public approve(policy: HostedDisclosurePolicy, input: HostedTaskInput, now: number): string {
    const validPolicy = HostedDisclosurePolicySchema.parse(policy);
    const checked = prepareHostedInput(validPolicy, input, now);
    if ("error" in checked) throw new Error("Hosted preparation cannot be approved.");
    for (const [key, approval] of this.approvals) if (approval.expiresAt < now) this.approvals.delete(key);
    if (this.approvals.size >= 256) throw new Error("Hosted approval capacity reached.");
    const approvalId = randomUUID();
    this.approvals.set(approvalId, { digest: digest(JSON.stringify({ policy: validPolicy, prepared: checked.input })),
      expiresAt: checked.input.binding.preparedAt + validPolicy.freshnessMs });
    return approvalId;
  }
  public consume(approvalId: string | undefined, policy: HostedDisclosurePolicy, input: PreparedHostedInput, now: number): boolean {
    if (!approvalId) return false;
    const approval = this.approvals.get(approvalId);
    if (!approval || approval.expiresAt < now || approval.digest !== digest(JSON.stringify({ policy, prepared: input }))) return false;
    this.approvals.delete(approvalId);
    return true;
  }
}
