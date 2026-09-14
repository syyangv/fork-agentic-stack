import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import type { PaseoAgent, PaseoAgentHandle, PaseoAgentStream, PaseoApi } from "@getpaseo/client";
import type {
  CaptureStatus,
  SourceRef,
  TaskStatus,
  TaskSummary,
  UserError,
  VaultScope,
} from "./contracts.shared.js";

type CoreContext = Record<string, unknown>;

const DURABLE_TASK_STATE_VERSION = 1 as const;
const MAX_PERSISTED_SOURCE_REFS = 8;
const MAX_PERSISTED_EXCLUSIONS = 64;
const MAX_PERSISTED_ERROR_MESSAGE = 512;
const MAX_PERSISTED_SOURCE_PATH = 512;
const MAX_PERSISTED_RECORD_STRING = 256;
const DURABLE_TASK_STATUSES = new Set([
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
const DURABLE_CAPTURE_STATUSES = new Set([
  "not_attempted",
  "pending",
  "draft_created",
  "duplicate",
  "quarantined",
  "capture_incomplete",
]);
const DURABLE_ERROR_CODES = new Set([
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

export interface TaskState {
  readonly taskId: string;
  workspaceId: string | null;
  provider: string | null;
  vaultScope: VaultScope;
  query: string | null;
  project: string | null;
  status: TaskStatus;
  captureStatus: CaptureStatus;
  agentId: string | null;
  turnId: string | null;
  verifiedCompletedTurnId: string | null;
  sourceManifest: SourceRef[];
  excludedSourceIds: Set<string>;
  personalContextUsed: boolean;
  lastTimelineEvent: string | null;
  error: UserError | null;
  createdAt: string;
  updatedAt: string;
  /** Work context is transient plugin state; personal context is never stored. */
  context: CoreContext | null;
  cleanup: Array<() => void>;
}

interface DurableTaskRecord {
  version: typeof DURABLE_TASK_STATE_VERSION;
  taskId: string;
  workspaceId: string | null;
  provider: string | null;
  vaultScope: VaultScope;
  status: TaskStatus;
  captureStatus: CaptureStatus;
  agentId: string | null;
  turnId: string | null;
  verifiedCompletedTurnId: string | null;
  sourceManifest: SourceRef[];
  excludedSourceIds: string[];
  error: UserError | null;
  createdAt: string;
  updatedAt: string;
}

interface DurableTaskFile {
  version: typeof DURABLE_TASK_STATE_VERSION;
  records: DurableTaskRecord[];
}

/**
 * Stores only bounded reconciliation metadata needed after a plugin restart.
 * Context, grants, lifecycle callbacks, and all session text are intentionally
 * outside this representation.
 */
export class DurableTaskRecordStore {
  public constructor(public readonly filePath: string) {
    if (!path.isAbsolute(filePath)) {
      throw new Error("The private task-state path must be absolute");
    }
  }

  public load(): DurableTaskRecord[] {
    let parsed: unknown;
    try {
      parsed = JSON.parse(fs.readFileSync(this.filePath, "utf8")) as unknown;
    } catch {
      return [];
    }
    if (!isRecord(parsed) || parsed.version !== DURABLE_TASK_STATE_VERSION || !Array.isArray(parsed.records)) {
      return [];
    }
    return parsed.records
      .map((record) => normalizeDurableTaskRecord(record))
      .filter((record): record is DurableTaskRecord => record !== null);
  }

  /** Atomic same-directory replacement with a user-readable-only state file. */
  public replace(records: Iterable<TaskState>): void {
    const parent = path.dirname(this.filePath);
    fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
    const temporaryPath = path.join(
      parent,
      `.${path.basename(this.filePath)}.${process.pid}.${randomUUID()}.tmp`,
    );
    const payload: DurableTaskFile = {
      version: DURABLE_TASK_STATE_VERSION,
      records: [...records].map(toDurableTaskRecord),
    };
    const encoded = `${JSON.stringify(payload)}\n`;
    let descriptor: number | null = null;
    try {
      descriptor = fs.openSync(temporaryPath, "wx", 0o600);
      fs.writeFileSync(descriptor, encoded, { encoding: "utf8" });
      fs.fsyncSync(descriptor);
      fs.closeSync(descriptor);
      descriptor = null;
      fs.renameSync(temporaryPath, this.filePath);
      // Keep the final mode restrictive even if the host umask is unusual.
      fs.chmodSync(this.filePath, 0o600);
    } finally {
      if (descriptor !== null) fs.closeSync(descriptor);
      try {
        fs.unlinkSync(temporaryPath);
      } catch {
        // The temporary file was renamed successfully, or was never created.
      }
    }
  }
}

function nowIso(): string {
  return new Date().toISOString();
}

function copyError(error: UserError | null): UserError | null {
  return error ? { ...error } : null;
}

export function summarizeTask(task: TaskState): TaskSummary {
  return {
    taskId: task.taskId,
    workspaceId: task.workspaceId,
    agentId: task.agentId,
    turnId: task.turnId,
    provider: task.provider,
    vaultScope: task.vaultScope,
    status: task.status,
    captureStatus: task.captureStatus,
    sourceManifest: task.sourceManifest.map((source) => ({ ...source })),
    excludedSourceIds: [...task.excludedSourceIds].sort(),
    personalContextUsed: task.personalContextUsed,
    lastTimelineEvent: task.lastTimelineEvent,
    error: copyError(task.error),
    createdAt: task.createdAt,
    updatedAt: task.updatedAt,
  };
}

export class PersonalGrantStore {
  private readonly grants = new Map<string, Map<string, number | null>>();

  public constructor(private readonly personalVaultRoot: string | null, private readonly clock = Date.now) {}

  public count(taskId: string): number {
    this.expire(taskId);
    return this.grants.get(taskId)?.size ?? 0;
  }

  public grant(taskId: string, requestedPath: string, expiresInSeconds?: number): number {
    if (!this.personalVaultRoot) {
      throw new Error("Personal vault root is not configured for this plugin");
    }
    if (path.isAbsolute(requestedPath)) {
      throw new Error("Personal source grants must use a relative path");
    }
    const relative = this.safeCanonicalRelative(requestedPath);
    this.expire(taskId);
    const taskGrants = this.grants.get(taskId) ?? new Map<string, number | null>();
    const expiresAt = expiresInSeconds === undefined ? null : this.clock() + expiresInSeconds * 1_000;
    taskGrants.set(relative.split(path.sep).join("/"), expiresAt);
    this.grants.set(taskId, taskGrants);
    return taskGrants.size;
  }

  public revoke(taskId: string, requestedPath?: string): boolean {
    this.expire(taskId);
    const taskGrants = this.grants.get(taskId);
    if (!taskGrants) return false;
    if (requestedPath === undefined) {
      const hadGrant = taskGrants.size > 0;
      this.grants.delete(taskId);
      return hadGrant;
    }
    if (!this.personalVaultRoot || path.isAbsolute(requestedPath)) return false;
    let relative: string;
    try {
      relative = this.safeCanonicalRelative(requestedPath);
    } catch {
      return false;
    }
    const revoked = taskGrants.delete(relative);
    if (taskGrants.size === 0) this.grants.delete(taskId);
    return revoked;
  }

  /** Only the supervisor can read the paths, and only for one in-flight call. */
  public pathsFor(taskId: string): string[] {
    this.expire(taskId);
    const taskGrants = this.grants.get(taskId);
    if (!taskGrants) return [];
    for (const sourcePath of [...taskGrants.keys()]) {
      try {
        // Re-check at use time so a later symlink retarget cannot expand a
        // previously granted scope before a supervised core consumes it.
        this.safeCanonicalRelative(sourcePath);
      } catch {
        taskGrants.delete(sourcePath);
      }
    }
    if (taskGrants.size === 0) {
      this.grants.delete(taskId);
      return [];
    }
    return [...taskGrants.keys()].sort();
  }

  public dispose(): void {
    this.grants.clear();
  }

  private expire(taskId: string): void {
    const taskGrants = this.grants.get(taskId);
    if (!taskGrants) return;
    const now = this.clock();
    for (const [sourcePath, expiresAt] of taskGrants) {
      if (expiresAt !== null && expiresAt <= now) taskGrants.delete(sourcePath);
    }
    if (taskGrants.size === 0) this.grants.delete(taskId);
  }

  private safeCanonicalRelative(requestedPath: string): string {
    if (!this.personalVaultRoot) throw new Error("Personal vault root is not configured for this plugin");
    if (path.isAbsolute(requestedPath) || !requestedPath.trim()) {
      throw new Error("Personal source grants must use a non-empty relative path");
    }
    if (requestedPath.split(/[\\/]+/).includes("..")) {
      throw new Error("Personal source grants must not contain parent traversal");
    }
    const configuredRoot = path.resolve(this.personalVaultRoot);
    const root = fs.realpathSync.native(configuredRoot);
    const lexical = path.resolve(configuredRoot, requestedPath);
    const lexicalRelative = path.relative(configuredRoot, lexical);
    if (!lexicalRelative || lexicalRelative === ".." || lexicalRelative.startsWith(`..${path.sep}`) || path.isAbsolute(lexicalRelative)) {
      throw new Error("Personal source grant must remain inside the configured vault");
    }
    const realPath = fs.realpathSync.native(lexical);
    const realRelative = path.relative(root, realPath);
    if (!realRelative || realRelative === ".." || realRelative.startsWith(`..${path.sep}`) || path.isAbsolute(realRelative)) {
      throw new Error("Personal source grant must remain inside the configured vault");
    }
    return realRelative.split(path.sep).join("/");
  }
}

export class TaskStore {
  private readonly tasks = new Map<string, TaskState>();
  private readonly durable: DurableTaskRecordStore | null;

  public constructor(
    public readonly grants: PersonalGrantStore = new PersonalGrantStore(null),
    options: { taskStatePath?: string } = {},
  ) {
    this.durable = options.taskStatePath ? new DurableTaskRecordStore(options.taskStatePath) : null;
    for (const record of this.durable?.load() ?? []) {
      this.tasks.set(record.taskId, taskFromDurableRecord(record));
    }
  }

  public get(taskId: string): TaskState | null {
    return this.tasks.get(taskId) ?? null;
  }

  public ensure(taskId: string): TaskState {
    const existing = this.tasks.get(taskId);
    if (existing) return existing;
    const timestamp = nowIso();
    const task: TaskState = {
      taskId,
      workspaceId: null,
      provider: null,
      vaultScope: "work",
      query: null,
      project: null,
      status: "needs_prepare",
      captureStatus: "not_attempted",
      agentId: null,
      turnId: null,
      verifiedCompletedTurnId: null,
      sourceManifest: [],
      excludedSourceIds: new Set<string>(),
      personalContextUsed: false,
      lastTimelineEvent: null,
      error: null,
      createdAt: timestamp,
      updatedAt: timestamp,
      context: null,
      cleanup: [],
    };
    this.tasks.set(taskId, task);
    this.flush();
    return task;
  }

  public configure(
    taskId: string,
    options: { workspaceId: string; provider: string; vaultScope: VaultScope; query: string; project?: string },
  ): TaskState {
    const task = this.ensure(taskId);
    if (
      task.agentId &&
      (task.workspaceId !== options.workspaceId || task.provider !== options.provider || task.vaultScope !== options.vaultScope)
    ) {
      throw new Error("A task identity cannot be rebound after an agent has been created");
    }
    task.workspaceId = options.workspaceId;
    task.provider = options.provider;
    task.vaultScope = options.vaultScope;
    task.query = options.query;
    task.project = options.project ?? null;
    task.updatedAt = nowIso();
    this.flush();
    return task;
  }

  public prepared(task: TaskState, context: CoreContext): void {
    task.sourceManifest = Array.isArray(context.source_manifest)
      ? context.source_manifest.filter((source): source is SourceRef => isSourceRef(source))
      : [];
    task.personalContextUsed = context.personal_context_used === true;
    task.status = "prepared";
    task.error = null;
    // Personal excerpts are never retained in the task store.
    task.context = task.personalContextUsed ? null : context;
    task.updatedAt = nowIso();
    this.flush();
  }

  public clearContext(task: TaskState): void {
    task.context = null;
    task.sourceManifest = [];
    task.personalContextUsed = false;
    task.verifiedCompletedTurnId = null;
    task.updatedAt = nowIso();
    this.flush();
  }

  public invalidateContext(task: TaskState): void {
    this.clearContext(task);
    task.status = "needs_prepare";
    task.updatedAt = nowIso();
    this.flush();
  }

  public setStatus(task: TaskState, status: TaskStatus, error: UserError | null = null): void {
    task.status = status;
    task.error = error;
    task.updatedAt = nowIso();
    this.flush();
  }

  public setCaptureStatus(task: TaskState, captureStatus: CaptureStatus): void {
    task.captureStatus = captureStatus;
    task.updatedAt = nowIso();
    this.flush();
  }

  public hasVerifiedCompletion(task: TaskState): boolean {
    return task.verifiedCompletedTurnId !== null && task.verifiedCompletedTurnId === task.turnId;
  }

  public markCaptureIncomplete(task: TaskState, message: string): void {
    this.setCaptureStatus(task, "capture_incomplete");
    task.error = {
      code: "capture_incomplete",
      message,
      retryable: true,
    };
    task.updatedAt = nowIso();
    this.flush();
  }

  public markCompletionUnknown(task: TaskState, message: string): void {
    this.setCaptureStatus(task, "capture_incomplete");
    this.setStatus(task, "unknown", {
      code: "capture_incomplete",
      message,
      retryable: true,
    });
  }

  public all(): TaskSummary[] {
    return [...this.tasks.values()].map(summarizeTask);
  }

  public bindAgent(task: TaskState, agentId: string): void {
    task.agentId = agentId;
    task.updatedAt = nowIso();
    this.flush();
  }

  public bindTurn(task: TaskState, turnId: string | null): void {
    if (turnId) task.turnId = turnId;
    task.updatedAt = nowIso();
    this.flush();
  }

  public recordAgent(task: TaskState, agent: PaseoAgent): void {
    if (agent.id !== task.agentId && task.agentId !== null) return;
    if (agent.workspaceId && task.workspaceId && agent.workspaceId !== task.workspaceId) {
      this.setCaptureStatus(task, "capture_incomplete");
      this.setStatus(task, "unknown", {
        code: "task_identity_conflict",
        message: "The saved agent belongs to a different Paseo workspace; manual resolution is required.",
        retryable: false,
      });
      return;
    }
    const activeTurn = agent.activeTurn?.turnId;
    if (activeTurn) task.turnId = activeTurn;
    if (Array.isArray(agent.pendingPermissions) && agent.pendingPermissions.length > 0) {
      this.setStatus(task, "awaiting_approval", {
        code: "awaiting_approval",
        message: "Paseo is waiting for an approval or permission decision.",
        retryable: false,
      });
      return;
    }
    switch (agent.status) {
      case "initializing":
        if (!this.hasVerifiedCompletion(task)) this.setStatus(task, "starting");
        break;
      case "running":
        if (!this.hasVerifiedCompletion(task)) this.setStatus(task, "running");
        break;
      case "error":
        this.setStatus(task, "failed", {
          code: "agent_failed",
          message: "The Paseo agent reported an execution error.",
          retryable: false,
        });
        break;
      case "closed":
        if (this.hasVerifiedCompletion(task)) {
          if (task.status !== "completed") this.setStatus(task, "completed");
        } else if (task.status !== "failed" && task.status !== "timed_out") {
          this.markCompletionUnknown(task, "Paseo closed the agent without a verified turn_completed event; manual reconciliation is required.");
        }
        break;
      case "idle":
        if (this.hasVerifiedCompletion(task)) {
          if (task.status !== "completed") this.setStatus(task, "completed");
        } else if (task.status === "starting" || task.status === "submitted" || task.status === "running" || task.status === "completed") {
          this.markCompletionUnknown(task, "Paseo is idle without a verified turn_completed event; manual reconciliation is required.");
        }
        break;
    }
    task.updatedAt = nowIso();
    this.flush();
  }

  public recordStream(task: TaskState, stream: PaseoAgentStream): void {
    if (!task.agentId || stream.agentId !== task.agentId) return;
    const event = stream.event;
    task.lastTimelineEvent = event.type;
    const eventTurnId = "turnId" in event && typeof event.turnId === "string" ? event.turnId : null;
    if (
      eventTurnId && task.turnId && eventTurnId !== task.turnId &&
      (event.type === "turn_started" || event.type === "turn_completed")
    ) {
      this.markCompletionUnknown(task, "Paseo reported a completion event for a different turn than the saved task identity; manual reconciliation is required.");
      return;
    }
    if (eventTurnId) task.turnId = eventTurnId;
    switch (event.type) {
      case "turn_started":
        if (eventTurnId) task.turnId = eventTurnId;
        this.setStatus(task, "running");
        break;
      case "turn_completed":
        if (!eventTurnId) {
          this.markCompletionUnknown(task, "Paseo reported turn completion without a turn ID; result capture is incomplete.");
          break;
        }
        task.verifiedCompletedTurnId = eventTurnId;
        this.setCaptureStatus(task, "pending");
        this.setStatus(task, "completed");
        break;
      case "permission_requested":
        this.setStatus(task, "awaiting_approval", {
          code: "awaiting_approval",
          message: "Paseo is waiting for an approval or permission decision.",
          retryable: false,
        });
        break;
      case "turn_failed":
        this.setStatus(task, "failed", {
          code: "agent_failed",
          message: "The Paseo agent turn failed.",
          retryable: false,
        });
        break;
      case "turn_canceled":
        this.setStatus(task, "failed", {
          code: "agent_failed",
          message: "The Paseo agent turn was canceled by Paseo.",
          retryable: false,
        });
        break;
      case "attention_required":
        if (event.reason === "permission") {
          this.setStatus(task, "awaiting_approval", {
            code: "awaiting_approval",
            message: "Paseo is waiting for an approval or permission decision.",
            retryable: false,
          });
        } else if (event.reason === "error") {
          this.setStatus(task, "failed", {
            code: "agent_failed",
            message: "The Paseo agent requires attention because of an error.",
            retryable: false,
          });
        } else if (!this.hasVerifiedCompletion(task)) {
          this.markCompletionUnknown(task, "Paseo requested attention as finished without a verified turn_completed event; manual reconciliation is required.");
        }
        break;
      default:
        task.updatedAt = nowIso();
        this.flush();
    }
  }

  public attachLifecycle(
    task: TaskState,
    paseo: PaseoApi,
    onStream?: (stream: PaseoAgentStream, agent: PaseoAgentHandle) => void,
  ): PaseoAgentHandle | null {
    if (!task.agentId) return null;
    this.detachLifecycle(task);
    const agent = paseo.agents.ref(task.agentId);
    task.cleanup.push(agent.subscribe((update) => {
      if (update.kind === "upsert" && update.agent.id === task.agentId) this.recordAgent(task, update.agent);
    }));
    task.cleanup.push(agent.timeline.subscribe((stream) => {
      if (stream.agentId === task.agentId) {
        this.recordStream(task, stream);
        onStream?.(stream, agent);
      }
    }));
    return agent;
  }

  public detachLifecycle(task: TaskState): void {
    for (const cleanup of task.cleanup.splice(0)) cleanup();
  }

  public dispose(): void {
    // Persist before lifecycle callbacks and in-memory identities are cleared.
    this.flush();
    for (const task of this.tasks.values()) this.detachLifecycle(task);
    this.tasks.clear();
    this.grants.dispose();
  }

  /** Flushes the allowlisted task projection; grants and context are omitted. */
  public flush(): void {
    this.durable?.replace(this.tasks.values());
  }
}

function toDurableTaskRecord(task: TaskState): DurableTaskRecord {
  return {
    version: DURABLE_TASK_STATE_VERSION,
    taskId: boundedRequiredString(task.taskId, MAX_PERSISTED_RECORD_STRING) ?? "invalid-task",
    workspaceId: boundedNullableString(task.workspaceId, MAX_PERSISTED_RECORD_STRING),
    provider: boundedNullableString(task.provider, MAX_PERSISTED_RECORD_STRING),
    vaultScope: task.vaultScope,
    status: task.status,
    captureStatus: task.captureStatus,
    agentId: boundedNullableString(task.agentId, MAX_PERSISTED_RECORD_STRING),
    turnId: boundedNullableString(task.turnId, MAX_PERSISTED_RECORD_STRING),
    verifiedCompletedTurnId: boundedNullableString(task.verifiedCompletedTurnId, MAX_PERSISTED_RECORD_STRING),
    // Provenance is durable only for work-vault tasks. Personal source paths
    // and excerpts never cross this allowlist.
    sourceManifest: task.vaultScope === "work"
      ? task.sourceManifest
        .map(copyBoundedSourceRef)
        .filter((source): source is SourceRef => source !== null)
        .slice(0, MAX_PERSISTED_SOURCE_REFS)
      : [],
    excludedSourceIds: [...task.excludedSourceIds]
      .filter((sourceId) => sourceId.length <= MAX_PERSISTED_RECORD_STRING)
      .slice(0, MAX_PERSISTED_EXCLUSIONS)
      .sort(),
    error: copyBoundedError(task.error),
    createdAt: boundedRequiredString(task.createdAt, MAX_PERSISTED_RECORD_STRING) ?? nowIso(),
    updatedAt: boundedRequiredString(task.updatedAt, MAX_PERSISTED_RECORD_STRING) ?? nowIso(),
  };
}

function normalizeDurableTaskRecord(value: unknown): DurableTaskRecord | null {
  if (!isRecord(value)) return null;
  const taskId = boundedRequiredString(value.taskId, MAX_PERSISTED_RECORD_STRING);
  const vaultScope = value.vaultScope === "personal" ? "personal" : value.vaultScope === "work" ? "work" : null;
  const status = typeof value.status === "string" && DURABLE_TASK_STATUSES.has(value.status)
    ? value.status as TaskStatus
    : null;
  const captureStatus = typeof value.captureStatus === "string" && DURABLE_CAPTURE_STATUSES.has(value.captureStatus)
    ? value.captureStatus as CaptureStatus
    : null;
  const createdAt = boundedRequiredString(value.createdAt, MAX_PERSISTED_RECORD_STRING);
  const updatedAt = boundedRequiredString(value.updatedAt, MAX_PERSISTED_RECORD_STRING);
  if (!taskId || !vaultScope || !status || !captureStatus || !createdAt || !updatedAt) return null;
  const sourceManifest = vaultScope === "work" && Array.isArray(value.sourceManifest)
    ? value.sourceManifest
      .map(copyBoundedSourceRef)
      .filter((source): source is SourceRef => source !== null)
      .slice(0, MAX_PERSISTED_SOURCE_REFS)
    : [];
  const excludedSourceIds = Array.isArray(value.excludedSourceIds)
    ? value.excludedSourceIds
      .filter((sourceId): sourceId is string => typeof sourceId === "string" && sourceId.length > 0 && sourceId.length <= MAX_PERSISTED_RECORD_STRING)
      .slice(0, MAX_PERSISTED_EXCLUSIONS)
      .sort()
    : [];
  return {
    version: DURABLE_TASK_STATE_VERSION,
    taskId,
    workspaceId: boundedNullableString(value.workspaceId, MAX_PERSISTED_RECORD_STRING),
    provider: boundedNullableString(value.provider, MAX_PERSISTED_RECORD_STRING),
    vaultScope,
    status,
    captureStatus,
    agentId: boundedNullableString(value.agentId, MAX_PERSISTED_RECORD_STRING),
    turnId: boundedNullableString(value.turnId, MAX_PERSISTED_RECORD_STRING),
    verifiedCompletedTurnId: boundedNullableString(value.verifiedCompletedTurnId, MAX_PERSISTED_RECORD_STRING),
    sourceManifest,
    excludedSourceIds,
    error: normalizeDurableError(value.error),
    createdAt,
    updatedAt,
  };
}

function taskFromDurableRecord(record: DurableTaskRecord): TaskState {
  return {
    taskId: record.taskId,
    workspaceId: record.workspaceId,
    provider: record.provider,
    vaultScope: record.vaultScope,
    query: null,
    project: null,
    status: record.status,
    captureStatus: record.captureStatus,
    agentId: record.agentId,
    turnId: record.turnId,
    verifiedCompletedTurnId: record.verifiedCompletedTurnId,
    sourceManifest: record.vaultScope === "work"
      ? record.sourceManifest.map(copyBoundedSourceRef).filter((source): source is SourceRef => source !== null)
      : [],
    excludedSourceIds: new Set(record.excludedSourceIds),
    personalContextUsed: false,
    lastTimelineEvent: null,
    error: record.error ? { ...record.error } : null,
    createdAt: record.createdAt,
    updatedAt: record.updatedAt,
    context: null,
    cleanup: [],
  };
}

function copyBoundedSourceRef(value: unknown): SourceRef | null {
  if (!isDurableWorkSourceRef(value)) return null;
  const source = value;
  return {
    source_id: source.source_id,
    vault_id: source.vault_id,
    note_id: source.note_id,
    path: source.path,
    ...(source.heading !== undefined ? { heading: source.heading } : {}),
    line_start: source.line_start,
    line_end: source.line_end,
    content_hash: source.content_hash,
  };
}

function isDurableWorkSourceRef(value: unknown): value is SourceRef {
  if (!isSourceRef(value)) return false;
  const source = value;
  return (
    source.vault_id === "work" &&
    isBoundedRequiredString(source.source_id, MAX_PERSISTED_RECORD_STRING) &&
    isBoundedRequiredString(source.note_id, MAX_PERSISTED_RECORD_STRING) &&
    isSafeRelativePosixPath(source.path, MAX_PERSISTED_SOURCE_PATH) &&
    (source.heading === undefined || source.heading === null || (
    typeof source.heading === "string" &&
    !source.heading.includes("\0") &&
    source.heading.length <= MAX_PERSISTED_RECORD_STRING
    )) &&
    Number.isSafeInteger(source.line_start) && source.line_start > 0 &&
    Number.isSafeInteger(source.line_end) && source.line_end >= source.line_start &&
    /^[0-9a-f]{64}$/i.test(source.content_hash)
  );
}

function isBoundedRequiredString(value: string, limit: number): boolean {
  return value.length > 0 && value.length <= limit && !value.includes("\0");
}

function isSafeRelativePosixPath(value: string, limit: number): boolean {
  if (
    !value ||
    value.length > limit ||
    value.includes("\0") ||
    value.startsWith("/") ||
    value.startsWith("~") ||
    value.includes("\\")
  ) {
    return false;
  }
  const components = value.split("/");
  return components.every((component) => component.length > 0 && component !== "." && component !== "..");
}

function copyBoundedError(error: UserError | null): UserError | null {
  if (!error || !DURABLE_ERROR_CODES.has(error.code)) return null;
  return {
    code: error.code,
    message: error.message.slice(0, MAX_PERSISTED_ERROR_MESSAGE),
    retryable: error.retryable,
  };
}

function normalizeDurableError(value: unknown): UserError | null {
  if (
    !isRecord(value) ||
    typeof value.code !== "string" ||
    !DURABLE_ERROR_CODES.has(value.code) ||
    typeof value.message !== "string" ||
    typeof value.retryable !== "boolean"
  ) return null;
  return {
    code: value.code as UserError["code"],
    message: value.message.slice(0, MAX_PERSISTED_ERROR_MESSAGE),
    retryable: value.retryable,
  };
}

function boundedRequiredString(value: unknown, limit: number): string | null {
  return typeof value === "string" && value.length > 0 && value.length <= limit ? value : null;
}

function boundedNullableString(value: unknown, limit: number): string | null {
  return value === null || value === undefined ? null : boundedRequiredString(value, limit);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSourceRef(value: unknown): value is SourceRef {
  if (!value || typeof value !== "object") return false;
  const source = value as Record<string, unknown>;
  return (
    typeof source.source_id === "string" &&
    typeof source.vault_id === "string" &&
    typeof source.note_id === "string" &&
    typeof source.path === "string" &&
    typeof source.line_start === "number" &&
    typeof source.line_end === "number" &&
    typeof source.content_hash === "string"
  );
}
