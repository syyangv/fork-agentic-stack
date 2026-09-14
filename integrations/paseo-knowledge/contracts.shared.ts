import { z } from "zod";
import { defineAttachmentSource, defineRpc } from "@getpaseo/plugin/server";

const id = z.string().trim().min(1).max(256);
const taskId = id.regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/);
const provider = id.refine((value) => value.includes("/"), {
  message: "Paseo provider must be the caller-selected provider/model value",
});
const sourceId = id;

export const VaultScopeSchema = z.enum(["work", "personal"]);
export type VaultScope = z.infer<typeof VaultScopeSchema>;

export const UserErrorCodeSchema = z.enum([
  "configuration",
  "retrieval_failed",
  "provider_unavailable",
  "awaiting_approval",
  "timeout",
  "source_invalidated",
  "personal_access_denied",
  "personal_access_unavailable",
  "task_not_found",
  "task_identity_conflict",
  "needs_prepare",
  "agent_create_unknown",
  "agent_send_unknown",
  "agent_failed",
  "capture_incomplete",
]);
export type UserErrorCode = z.infer<typeof UserErrorCodeSchema>;

export const UserVisibleErrorSchema = z.object({
  code: UserErrorCodeSchema,
  message: z.string(),
  retryable: z.boolean(),
});
export type UserVisibleError = z.infer<typeof UserVisibleErrorSchema>;
export type UserError = UserVisibleError;

export const SourceRefSchema = z.object({
  source_id: id,
  vault_id: id,
  note_id: id,
  path: z.string().min(1),
  heading: z.string().nullable().optional(),
  line_start: z.number().int().positive(),
  line_end: z.number().int().positive(),
  content_hash: z.string().regex(/^[0-9a-f]{64}$/i),
});
export type SourceRef = z.infer<typeof SourceRefSchema>;

const CorePayloadSchema = z.record(z.string(), z.unknown());

export const TaskStatusSchema = z.enum([
  "prepared",
  "starting",
  "submitted",
  "running",
  "awaiting_approval",
  "completed",
  "failed",
  "timed_out",
  "invalidated",
  "unknown",
  "needs_prepare",
]);
export type TaskStatus = z.infer<typeof TaskStatusSchema>;

export const CaptureStatusSchema = z.enum([
  "not_attempted",
  "pending",
  "draft_created",
  "duplicate",
  "quarantined",
  "capture_incomplete",
]);
export type CaptureStatus = z.infer<typeof CaptureStatusSchema>;

export const TaskSummarySchema = z.object({
  taskId,
  workspaceId: id.nullable(),
  agentId: id.nullable(),
  turnId: id.nullable(),
  provider: provider.nullable(),
  vaultScope: VaultScopeSchema,
  status: TaskStatusSchema,
  captureStatus: CaptureStatusSchema,
  sourceManifest: z.array(SourceRefSchema),
  excludedSourceIds: z.array(sourceId),
  personalContextUsed: z.boolean(),
  lastTimelineEvent: z.string().nullable(),
  error: UserVisibleErrorSchema.nullable(),
  createdAt: z.string(),
  updatedAt: z.string(),
});
export type TaskSummary = z.infer<typeof TaskSummarySchema>;

export const PluginStatusRpc = defineRpc({
  name: "knowledge.status",
  input: z.object({
    taskId: taskId.optional(),
    agentId: id.optional(),
    workspaceId: id.optional(),
    turnId: id.optional(),
  }),
  output: z.object({
    configured: z.boolean(),
    coreStatus: z.string(),
    task: TaskSummarySchema.nullable(),
    tasks: z.array(TaskSummarySchema),
    error: UserVisibleErrorSchema.nullable(),
  }),
});

export const SearchKnowledgeRpc = defineRpc({
  name: "knowledge.search",
  input: z.object({
    query: z.string().trim().min(1).max(2_000),
    taskId: taskId.optional(),
    project: z.string().trim().min(1).max(256).optional(),
    vaultScope: VaultScopeSchema.default("work"),
    limit: z.number().int().positive().max(8).default(8),
    includeRelated: z.boolean().default(false),
  }),
  output: z.object({
    status: z.string(),
    results: z.array(CorePayloadSchema),
    sourceContentUntrusted: z.literal(true),
    error: UserVisibleErrorSchema.nullable(),
  }),
});

export const PrepareKnowledgeTaskRpc = defineRpc({
  name: "knowledge.task.prepare",
  input: z.object({
    taskId,
    workspaceId: id,
    provider,
    query: z.string().trim().min(1).max(2_000),
    project: z.string().trim().min(1).max(256).optional(),
    vaultScope: VaultScopeSchema.default("work"),
    includeRelated: z.boolean().default(false),
    maxSnippets: z.number().int().positive().max(8).default(8),
    maxChars: z.number().int().positive().max(12_000).default(12_000),
  }),
  output: z.object({
    task: TaskSummarySchema,
    context: CorePayloadSchema.nullable(),
    error: UserVisibleErrorSchema.nullable(),
  }),
});

export const StartKnowledgeTaskRpc = defineRpc({
  name: "knowledge.task.start",
  input: z.object({
    taskId,
    workspaceId: id,
    /** Paseo's exact provider/model selection; the plugin never chooses one. */
    provider,
    prompt: z.string().trim().min(1).max(20_000),
  }),
  output: z.object({
    task: TaskSummarySchema,
    error: UserVisibleErrorSchema.nullable(),
  }),
});

export const ReconcileKnowledgeTaskRpc = defineRpc({
  name: "knowledge.task.reconcile",
  input: z.object({
    taskId,
    agentId: id.optional(),
    workspaceId: id.optional(),
    turnId: id.optional(),
  }),
  output: z.object({
    task: TaskSummarySchema.nullable(),
    timeline: z.object({
      entryCount: z.number().int().nonnegative(),
      lastTurnId: id.nullable(),
    }).nullable(),
    error: UserVisibleErrorSchema.nullable(),
  }),
});

export const WaitKnowledgeTaskRpc = defineRpc({
  name: "knowledge.task.wait",
  input: z.object({
    taskId,
    timeoutMs: z.number().int().positive().max(900_000).default(60_000),
    agentId: id.optional(),
  }),
  output: z.object({
    task: TaskSummarySchema.nullable(),
    error: UserVisibleErrorSchema.nullable(),
  }),
});

export const RemoveKnowledgeSourceRpc = defineRpc({
  name: "knowledge.source.remove",
  input: z.object({ taskId, sourceId }),
  output: z.object({
    task: TaskSummarySchema,
    removed: z.boolean(),
    error: UserVisibleErrorSchema.nullable(),
  }),
});

export const GrantPersonalSourceRpc = defineRpc({
  name: "knowledge.personal.grant",
  input: z.object({
    taskId,
    path: z.string().trim().min(1).max(2_000),
    expiresInSeconds: z.number().positive().finite().max(86_400).optional(),
  }),
  output: z.object({
    task: TaskSummarySchema,
    grantedPathCount: z.number().int().nonnegative(),
    error: UserVisibleErrorSchema.nullable(),
  }),
});

export const RevokePersonalSourceRpc = defineRpc({
  name: "knowledge.personal.revoke",
  input: z.object({ taskId, path: z.string().trim().min(1).max(2_000).optional() }),
  output: z.object({
    task: TaskSummarySchema,
    revoked: z.boolean(),
    grantedPathCount: z.number().int().nonnegative(),
    error: UserVisibleErrorSchema.nullable(),
  }),
});

export const KnowledgeAttachmentSearchRpc = defineRpc({
  name: "knowledge.attachments.search",
  input: z.object({ query: z.string().trim().min(1).max(2_000) }),
  output: z.object({
    items: z.array(z.object({
      id: id,
      identifier: id,
      title: z.string(),
      subtitle: z.string().optional(),
      url: z.string().url(),
      text: z.string(),
      resourceType: z.string(),
    })),
  }),
});

export const KnowledgeAttachmentSource = defineAttachmentSource({
  id: "knowledge",
  title: "Knowledge",
  icon: "BookOpen",
  pickerTitle: "Attach knowledge source",
  searchPlaceholder: "Search the Agent Knowledge Vault",
  search: KnowledgeAttachmentSearchRpc,
});

export type CorePayload = z.infer<typeof CorePayloadSchema>;
export type PluginStatusResponse = z.output<typeof PluginStatusRpc.output>;
export type SearchKnowledgeResponse = z.output<typeof SearchKnowledgeRpc.output>;
export type PrepareKnowledgeTaskResponse = z.output<typeof PrepareKnowledgeTaskRpc.output>;
export type StartKnowledgeTaskResponse = z.output<typeof StartKnowledgeTaskRpc.output>;
export type ReconcileKnowledgeTaskResponse = z.output<typeof ReconcileKnowledgeTaskRpc.output>;
export type WaitKnowledgeTaskResponse = z.output<typeof WaitKnowledgeTaskRpc.output>;
export type RemoveKnowledgeSourceResponse = z.output<typeof RemoveKnowledgeSourceRpc.output>;
export type GrantPersonalSourceResponse = z.output<typeof GrantPersonalSourceRpc.output>;
export type RevokePersonalSourceResponse = z.output<typeof RevokePersonalSourceRpc.output>;
