import { z } from "zod";
import { defineRpc } from "@getpaseo/plugin";
const id = z.string().min(1).max(256);
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const failure = z.object({ code: z.string().min(1).max(128), retryable: z.literal(false) });
export const HostedConsentPreviewSchema = z.object({
  previewId: id, challenge: id, payloadDigest: hash, expiresAt: z.number().int().nonnegative(),
  taskId: id, workspaceId: id, provider: id, prompt: z.string().max(65_536),
  policy: z.object({ policyId: id, version: id, endpoint: z.literal("https://opencode.ai/zen/go/v1"), model: id,
    disclosure: z.literal("work-context-to-opencode-and-upstream") }),
  sources: z.array(z.object({ sourceId: id, path: z.string().max(1_024), text: z.string().max(65_536),
    contentHash: hash, excerptHash: hash, lineStart: z.number().int().positive(), lineEnd: z.number().int().positive() })).max(8),
});
export type HostedConsentPreview = z.infer<typeof HostedConsentPreviewSchema>;
export const HostedDraftSummarySchema = z.object({
  draftId: id, taskId: id, workspaceId: id, model: id, completionId: id,
  outputDigest: hash, status: z.enum(["pending_review", "rejected"]),
});
export const HostedDraftDetailsSchema = HostedDraftSummarySchema.extend({
  output: z.string().max(65_536),
  sources: z.array(z.object({ sourceId: id, path: z.string().max(1_024), contentHash: hash, excerptHash: hash })).max(64),
});
export type HostedDraftSummary = z.infer<typeof HostedDraftSummarySchema>;
export type HostedDraftDetails = z.infer<typeof HostedDraftDetailsSchema>;
const result = z.union([
  z.object({ status: z.literal("completed"), executionId: id, completionId: id, draftId: id }),
  z.object({ status: z.enum(["failed", "cancelled", "timeout", "ambiguous"]), executionId: id.optional(), error: failure }),
]);
export const HostedAvailabilityRpc = defineRpc({ name: "knowledge.hosted.availability", input: z.object({ workspaceId: id }),
  output: z.object({ available: z.boolean(), error: failure.nullable() }) });
export const HostedPreviewRpc = defineRpc({ name: "knowledge.hosted.preview",
  input: z.object({ taskId: id, workspaceId: id, prompt: z.string().min(1).max(20_000) }).strict(),
  output: z.object({ preview: HostedConsentPreviewSchema.nullable(), error: failure.nullable() }) });
export const HostedExecuteRpc = defineRpc({ name: "knowledge.hosted.execute",
  input: z.object({ taskId: id, workspaceId: id, previewId: id, challenge: id, payloadDigest: hash }).strict(),
  output: z.object({ result: result.nullable(), error: failure.nullable() }) });
export const HostedDraftListRpc = defineRpc({ name: "knowledge.hosted.drafts.list", input: z.object({ workspaceId: id }),
  output: z.object({ drafts: z.array(HostedDraftSummarySchema).max(50), unreadableCount: z.number().int().nonnegative(), error: failure.nullable() }) });
export const HostedDraftReadRpc = defineRpc({ name: "knowledge.hosted.drafts.read", input: z.object({ workspaceId: id, draftId: id }),
  output: z.object({ draft: HostedDraftDetailsSchema.nullable(), error: failure.nullable() }) });
export const HostedDraftRejectRpc = defineRpc({ name: "knowledge.hosted.drafts.reject", input: z.object({ workspaceId: id, draftId: id, outputDigest: hash }),
  output: z.object({ rejected: z.boolean(), error: failure.nullable() }) });

export const HostedCancelRpc = defineRpc({ name: "knowledge.hosted.cancel", input: z.object({ workspaceId: id, taskId: id }),
  output: z.object({ cancelled: z.boolean(), error: failure.nullable() }) });
