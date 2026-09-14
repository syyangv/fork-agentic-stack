import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { KnowledgeCliRunner } from "../core.server.js";
import {
  KnowledgeAttachmentSearchRpc,
  KnowledgeAttachmentSource,
  PluginStatusRpc,
  PrepareKnowledgeTaskRpc,
  StartKnowledgeTaskRpc,
} from "../contracts.shared.js";
import { KnowledgeTaskService, resolveProductionTaskStatePath } from "../service.server.js";
import { PersonalGrantStore } from "../task-store.server.js";

const fixture = JSON.parse(
  readFileSync(new URL("./fixtures/context-bundle.json", import.meta.url), "utf8"),
) as Record<string, unknown>;

const sourceRef = {
  source_id: "source_synthetic_p4_001",
  vault_id: "work",
  note_id: "note_synthetic_p4_001",
  path: "10-Projects/paseo.md",
  heading: "Boundary",
  line_start: 1,
  line_end: 4,
  content_hash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
};

const baseSnapshot = (overrides: Record<string, unknown> = {}) => ({
  id: "agent_synthetic_p4_001",
  workspaceId: "workspace_synthetic_p4_001",
  provider: "codex",
  model: "gpt-5.4",
  cwd: "/synthetic/workspace",
  status: "running",
  createdAt: "2026-09-13T00:00:00.000Z",
  updatedAt: "2026-09-13T00:00:00.000Z",
  lastActivityAt: "2026-09-13T00:00:00.000Z",
  title: "Knowledge Task ktask_synthetic_p4_001",
  currentModeId: null,
  thinkingOptionId: null,
  requiresAttention: false,
  attentionReason: null,
  parentAgentId: null,
  labels: { knowledge_task_id: "ktask_synthetic_p4_001", vault_scope: "work" },
  activeTurn: { turnId: "turn_synthetic_p4_001", startedAt: "2026-09-13T00:00:00.000Z" },
  pendingPermissions: [],
  lastError: null,
  ...overrides,
});

function makePaseo(options: {
  create?: (input: unknown) => Promise<unknown>;
  snapshot?: Record<string, unknown>;
  wait?: (timeoutMs?: number) => Promise<unknown>;
  timelineEntries?: Array<Record<string, unknown>>;
} = {}) {
  const snapshot = options.snapshot ?? baseSnapshot();
  const agentId = String(snapshot.id);
  let agentUpdateHandler: ((update: unknown) => void) | undefined;
  let timelineHandler: ((stream: unknown) => void) | undefined;
  const agent = {
    id: agentId,
    workspaceId: snapshot.workspaceId,
    status: snapshot.status,
    activeTurn: snapshot.activeTurn,
    pendingPermissions: snapshot.pendingPermissions,
    send: vi.fn(async (text: string, options?: unknown) => { void text; void options; }),
    refresh: vi.fn(async () => ({ agent: snapshot, project: null })),
    waitForFinish: vi.fn(options.wait ?? (async () => ({ status: "idle", final: snapshot }))),
    subscribe: vi.fn((handler: (update: unknown) => void) => {
      agentUpdateHandler = handler;
      return () => { agentUpdateHandler = undefined; };
    }),
    timeline: {
      refetch: vi.fn(async () => ({ agentId: agent.id, entries: options.timelineEntries ?? [], agent: snapshot, error: null })),
      subscribe: vi.fn((handler: (stream: unknown) => void) => {
        timelineHandler = handler;
        return () => { timelineHandler = undefined; };
      }),
    },
  };
  const create = vi.fn(options.create ?? (async () => agent));
  const workspace = { agents: { create } };
  const paseo = {
    workspaces: { ref: vi.fn(() => workspace) },
    agents: { ref: vi.fn(() => agent) },
    providers: { listAvailable: vi.fn(async () => ({ providers: [] })) },
  };
  return {
    paseo: paseo as never,
    agent,
    create,
    emitAgent: (update: unknown) => agentUpdateHandler?.(update),
    emitTimeline: (stream: unknown) => timelineHandler?.(stream),
  };
}

function makeCore(context: Record<string, unknown> = fixture) {
  return {
    status: vi.fn(async () => ({ status: "ready" })),
    search: vi.fn(async () => ({ status: "ok", results: [{ ...sourceRef, source_ref: sourceRef, snippet: "synthetic result" }] })),
    buildContext: vi.fn(async () => context),
    proposeNote: vi.fn(async () => ({ capture_status: "draft_created" })),
  };
}

const prepareInput = {
  taskId: "ktask_synthetic_p4_001",
  workspaceId: "workspace_synthetic_p4_001",
  provider: "codex/gpt-5.4",
  query: "Paseo plugin boundary",
  vaultScope: "work" as const,
  includeRelated: false,
  maxSnippets: 8,
  maxChars: 12_000,
};

const startInput = {
  taskId: prepareInput.taskId,
  workspaceId: prepareInput.workspaceId,
  provider: prepareInput.provider,
  prompt: "Summarize the verified plugin boundary.",
};

describe("Paseo Knowledge P4 synthetic contracts", () => {
  const tempDirs: string[] = [];

  afterEach(() => {
    for (const directory of tempDirs.splice(0)) rmSync(directory, { recursive: true, force: true });
  });

  it("uses an absolute Python executable, argv, and shell:false without shell interpolation", async () => {
    const directory = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-cli-"));
    tempDirs.push(directory);
    const shim = path.join(directory, "core-shim.py");
    writeFileSync(shim, "import json, sys\nprint(json.dumps({'status': 'ready', 'argv': sys.argv[1:]}))\n", "utf8");
    chmodSync(shim, 0o600);
    const literal = "/synthetic/work;this-must-stay-one-argv-value";
    const runner = new KnowledgeCliRunner({
      pythonExecutable: "/Users/syang/miniconda3/bin/python3",
      cliPath: shim,
      workVaultPath: literal,
    });

    await expect(runner.status()).resolves.toMatchObject({
      status: "ready",
      argv: ["status", "--work-vault", literal],
    });
  });

  it("passes the completed result to propose_note over argv plus JSON stdin", async () => {
    const directory = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-capture-cli-"));
    tempDirs.push(directory);
    const shim = path.join(directory, "capture-shim.py");
    writeFileSync(shim, "import json, sys\nprint(json.dumps({'argv': sys.argv[1:], 'input': json.load(sys.stdin)}))\n", "utf8");
    chmodSync(shim, 0o600);
    const draftStore = path.join(directory, "drafts.sqlite3");
    const resultText = "candidate $(must-not-be-shell-expanded)";
    const runner = new KnowledgeCliRunner({
      pythonExecutable: "/Users/syang/miniconda3/bin/python3",
      cliPath: shim,
      draftStorePath: draftStore,
    });

    await expect(runner.proposeNote({
      taskId: "task_capture_synthetic",
      turnId: "turn_capture_synthetic",
      taskStatus: "completed",
      turnStatus: "completed",
      resultText,
      sourceManifest: [sourceRef],
      personalContextUsed: true,
    })).resolves.toEqual({
      argv: [
        "propose_note",
        "--task-id",
        "task_capture_synthetic",
        "--turn-id",
        "turn_capture_synthetic",
        "--task-status",
        "completed",
        "--draft-store",
        draftStore,
      ],
      input: {
        result: resultText,
        turn_status: "completed",
        source_manifest: [sourceRef],
        personal_context_used: true,
      },
    });
  });

  it("keeps personal grants task-scoped, relative, expiring, and non-persistent", () => {
    let now = 1_000;
    const root = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-personal-"));
    tempDirs.push(root);
    mkdirSync(path.join(root, "Journal"));
    writeFileSync(path.join(root, "Journal", "note.md"), "synthetic private note\n", "utf8");
    const grants = new PersonalGrantStore(root, () => now);
    expect(grants.grant("task-a", "Journal/note.md", 10)).toBe(1);
    expect(grants.pathsFor("task-a")).toEqual(["Journal/note.md"]);
    expect(grants.pathsFor("task-b")).toEqual([]);
    expect(() => grants.grant("task-a", "../outside.md")).toThrow();
    now += 11_000;
    expect(grants.pathsFor("task-a")).toEqual([]);
    grants.dispose();
    expect(grants).not.toHaveProperty("database");
    expect(grants).not.toHaveProperty("sqlite");
  });

  it("rejects personal grants whose real path escapes through a symlink", () => {
    const root = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-personal-"));
    const outside = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-outside-"));
    tempDirs.push(root, outside);
    mkdirSync(path.join(outside, "private"));
    symlinkSync(outside, path.join(root, "escape"), "dir");

    const grants = new PersonalGrantStore(root);
    expect(() => grants.grant("task-symlink", "escape/private")).toThrow(/inside|symlink|path/i);
    expect(grants.pathsFor("task-symlink")).toEqual([]);
  });

  it("prepares through the core boundary, creates once, and sends explicitly with stable identities", async () => {
    const core = makeCore();
    const fake = makePaseo();
    const personalRoot = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-personal-"));
    tempDirs.push(personalRoot);
    mkdirSync(path.join(personalRoot, "Journal"));
    writeFileSync(path.join(personalRoot, "Journal", "note.md"), "synthetic private note\n", "utf8");
    const service = new KnowledgeTaskService({ core, personalVaultRoot: personalRoot });
    await expect(service.prepare(prepareInput)).resolves.toMatchObject({ error: null });
    await expect(service.start(startInput, fake.paseo)).resolves.toMatchObject({ task: { status: "running", agentId: fake.agent.id } });
    await service.start(startInput, fake.paseo);

    expect(fake.create).toHaveBeenCalledTimes(1);
    expect(fake.create).toHaveBeenCalledWith(expect.objectContaining({
      config: { provider: "codex/gpt-5.4" },
      clientMessageId: "knowledge-task:ktask_synthetic_p4_001",
      labels: { knowledge_task_id: prepareInput.taskId, vault_scope: "work" },
    }));
    expect(fake.create.mock.calls[0]?.[0]).not.toHaveProperty("prompt");
    expect(fake.agent.send).toHaveBeenCalledTimes(1);
    expect(fake.agent.send.mock.calls[0]?.[1]).toEqual({ messageId: "knowledge-turn:ktask_synthetic_p4_001" });
  });

  it("clears prior context and source manifest for every failed preparation", async () => {
    const core = makeCore();
    const service = new KnowledgeTaskService({ core });
    await service.prepare(prepareInput);

    for (const status of ["stale_sources", "source_error", "access_denied"]) {
      core.buildContext.mockResolvedValueOnce({ ...fixture, status, source_manifest: [sourceRef], context: "stale synthetic excerpt" });
      const failed = await service.prepare(prepareInput);
      expect(failed.context).toBeNull();
      expect(failed.task.sourceManifest).toEqual([]);
      expect(service.store.get(prepareInput.taskId)?.context).toBeNull();
    }

    core.buildContext.mockRejectedValueOnce(new Error("synthetic core failure"));
    const thrown = await service.prepare(prepareInput);
    expect(thrown.context).toBeNull();
    expect(thrown.task.sourceManifest).toEqual([]);
    expect(service.store.get(prepareInput.taskId)?.context).toBeNull();

    const fake = makePaseo();
    const started = await service.start(startInput, fake.paseo);
    expect(started.error?.code).toBe("needs_prepare");
    expect(fake.create).not.toHaveBeenCalled();
    expect(fake.agent.send).not.toHaveBeenCalled();
  });

  it("does not resend when create or send has an ambiguous outcome", async () => {
    const createCore = makeCore();
    const createFake = makePaseo({ create: vi.fn(async () => { throw new Error("transport timeout"); }) });
    const createService = new KnowledgeTaskService({ core: createCore });
    await createService.prepare(prepareInput);
    const firstCreate = await createService.start(startInput, createFake.paseo);
    const secondCreate = await createService.start(startInput, createFake.paseo);
    expect(firstCreate.task.status).toBe("unknown");
    expect(firstCreate.error?.code).toBe("agent_create_unknown");
    expect(secondCreate.error?.code).toBe("agent_create_unknown");
    expect(createFake.create).toHaveBeenCalledTimes(1);

    const sendFake = makePaseo();
    sendFake.agent.send.mockRejectedValueOnce(new Error("connection lost after accept"));
    const sendService = new KnowledgeTaskService({ core: makeCore() });
    await sendService.prepare(prepareInput);
    const firstSend = await sendService.start(startInput, sendFake.paseo);
    await sendService.start(startInput, sendFake.paseo);
    expect(firstSend.task).toMatchObject({ status: "unknown", agentId: sendFake.agent.id });
    expect(firstSend.error?.code).toBe("agent_send_unknown");
    expect(sendFake.create).toHaveBeenCalledTimes(1);
    expect(sendFake.agent.send).toHaveBeenCalledTimes(1);
  });

  it("maps provider availability, approval lifecycle, and timeline events without a write surface", async () => {
    const fake = makePaseo();
    (fake.paseo as any).providers.listAvailable.mockResolvedValue({ providers: [{ provider: "codex", available: false }] });
    const service = new KnowledgeTaskService({ core: makeCore() });
    await service.prepare(prepareInput);
    const unavailable = await service.start(startInput, fake.paseo);
    expect(unavailable.error?.code).toBe("provider_unavailable");
    expect(fake.create).not.toHaveBeenCalled();

    const lifecycleFake = makePaseo();
    const lifecycleService = new KnowledgeTaskService({ core: makeCore() });
    await lifecycleService.prepare(prepareInput);
    await lifecycleService.start(startInput, lifecycleFake.paseo);
    lifecycleFake.emitTimeline({
      agentId: lifecycleFake.agent.id,
      event: { type: "permission_requested", provider: "codex", request: { id: "permission-1", provider: "codex", name: "approval", kind: "plan" }, turnId: "turn_synthetic_p4_001" },
    });
    expect(lifecycleService.store.get(prepareInput.taskId)?.status).toBe("awaiting_approval");
    lifecycleFake.emitTimeline({ agentId: lifecycleFake.agent.id, event: { type: "turn_completed", provider: "codex", turnId: "turn_synthetic_p4_001" } });
    expect(lifecycleService.store.get(prepareInput.taskId)?.status).toBe("completed");
  });

  it("captures only a verified completed turn from its matching assistant timeline item", async () => {
    const candidate = "<knowledge-candidate>{\"summary\":\"verified synthetic result\"}</knowledge-candidate>";
    const core = makeCore();
    const fake = makePaseo({
      timelineEntries: [{ turnId: "turn_synthetic_p4_001", item: { type: "assistant_message", text: candidate } }],
    });
    const service = new KnowledgeTaskService({ core });
    await service.prepare(prepareInput);
    await service.start(startInput, fake.paseo);

    fake.emitTimeline({
      agentId: fake.agent.id,
      event: { type: "turn_completed", provider: "codex", turnId: "turn_synthetic_p4_001" },
    });
    await vi.waitFor(() => expect(core.proposeNote).toHaveBeenCalledTimes(1));

    expect(core.proposeNote).toHaveBeenCalledWith({
      taskId: prepareInput.taskId,
      turnId: "turn_synthetic_p4_001",
      taskStatus: "completed",
      turnStatus: "completed",
      resultText: candidate,
      sourceManifest: [sourceRef],
      personalContextUsed: false,
    });
    const firstCaptureInput = (core.proposeNote.mock.calls as unknown[][])[0]?.[0];
    expect(firstCaptureInput).not.toHaveProperty("final");
    expect(service.store.get(prepareInput.taskId)).toMatchObject({ status: "completed", captureStatus: "draft_created" });
  });

  it("rejects a turn_completed event whose turn identity does not match the task", async () => {
    const core = makeCore();
    const fake = makePaseo({
      timelineEntries: [{ turnId: "turn_synthetic_other", item: { type: "assistant_message", text: "wrong synthetic result" } }],
    });
    const service = new KnowledgeTaskService({ core });
    await service.prepare(prepareInput);
    await service.start(startInput, fake.paseo);

    fake.emitTimeline({
      agentId: fake.agent.id,
      event: { type: "turn_completed", provider: "codex", turnId: "turn_synthetic_other" },
    });

    await vi.waitFor(() => expect(service.store.get(prepareInput.taskId)?.captureStatus).toBe("capture_incomplete"));
    expect(service.store.get(prepareInput.taskId)).toMatchObject({ status: "unknown", turnId: "turn_synthetic_p4_001" });
    expect(core.proposeNote).not.toHaveBeenCalled();
  });

  it("marks capture incomplete when completion is verified but the timeline has no safe result text", async () => {
    const core = makeCore();
    const fake = makePaseo({ timelineEntries: [{ turnId: "turn_synthetic_p4_001", item: { type: "reasoning", text: "not an answer" } }] });
    const service = new KnowledgeTaskService({ core });
    await service.prepare(prepareInput);
    await service.start(startInput, fake.paseo);
    fake.emitTimeline({
      agentId: fake.agent.id,
      event: { type: "turn_completed", provider: "codex", turnId: "turn_synthetic_p4_001" },
    });

    await vi.waitFor(() => expect(service.store.get(prepareInput.taskId)?.captureStatus).toBe("capture_incomplete"));
    expect(core.proposeNote).not.toHaveBeenCalled();
    expect(service.store.get(prepareInput.taskId)).toMatchObject({ status: "completed", captureStatus: "capture_incomplete" });
  });

  it("does not infer success from a closed agent without a verified completed-turn event", async () => {
    const fake = makePaseo({ snapshot: baseSnapshot({ status: "closed", activeTurn: null }) });
    const service = new KnowledgeTaskService({ core: makeCore() });
    await service.prepare(prepareInput);
    const result = await service.start(startInput, fake.paseo);

    expect(result.task).toMatchObject({ status: "unknown", agentId: fake.agent.id, captureStatus: "capture_incomplete" });
    expect(result.error?.code).toBe("capture_incomplete");
  });

  it("does not infer success from an idle wait result without a verified completed-turn event", async () => {
    const fake = makePaseo({ wait: async () => ({ status: "idle", final: baseSnapshot({ status: "idle" }), error: null, lastMessage: "looks done" }) });
    const service = new KnowledgeTaskService({ core: makeCore() });
    await service.prepare(prepareInput);
    await service.start(startInput, fake.paseo);
    const turnBeforeWait = service.store.get(prepareInput.taskId)?.turnId;
    const waited = await service.wait({ taskId: prepareInput.taskId, agentId: fake.agent.id, timeoutMs: 1_000 }, fake.paseo);

    expect(waited.task).toMatchObject({ status: "unknown", agentId: fake.agent.id, turnId: turnBeforeWait, captureStatus: "capture_incomplete" });
    expect(waited.error?.code).toBe("capture_incomplete");
  });

  it("does not infer success from matching timeline text without turn completion proof", async () => {
    const fake = makePaseo({ timelineEntries: [{ turnId: "turn_synthetic_p4_001", item: { type: "assistant_message", text: "done" } }] });
    const service = new KnowledgeTaskService({ core: makeCore() });
    const reconciled = await service.reconcile({ taskId: prepareInput.taskId, agentId: fake.agent.id, workspaceId: prepareInput.workspaceId, turnId: "turn_synthetic_p4_001" }, fake.paseo);
    expect(reconciled).toMatchObject({ task: { agentId: fake.agent.id, turnId: "turn_synthetic_p4_001", status: "running", captureStatus: "capture_incomplete" }, timeline: { entryCount: 1, lastTurnId: "turn_synthetic_p4_001" } });
    expect(reconciled.error?.code).toBe("capture_incomplete");
    expect(service.store.get(prepareInput.taskId)?.verifiedCompletedTurnId).toBeNull();
    expect(service.store.get(prepareInput.taskId)?.error?.code).toBe("capture_incomplete");
    expect((service.store.get(prepareInput.taskId)?.context)).toBeNull();
    expect((service.store.get(prepareInput.taskId)?.captureStatus)).toBe("capture_incomplete");

    const mismatchFake = makePaseo({ timelineEntries: [{ turnId: "turn_synthetic_p4_001", item: { type: "assistant_message", text: "done" } }] });
    const mismatchService = new KnowledgeTaskService({ core: makeCore() });
    const mismatch = await mismatchService.reconcile({ taskId: prepareInput.taskId, agentId: mismatchFake.agent.id, workspaceId: prepareInput.workspaceId, turnId: "turn_saved_before_restart" }, mismatchFake.paseo);
    expect(mismatch).toMatchObject({ task: { status: "unknown", captureStatus: "capture_incomplete" }, timeline: null });
    expect(mismatch.error?.code).toBe("task_identity_conflict");
    expect(mismatchFake.agent.timeline.refetch).not.toHaveBeenCalled();

  });

  it("reconciles by saved agent/turn identity and retains the turn on timeout", async () => {
    const timeoutFake = makePaseo({ wait: async () => ({ status: "timeout" }) });
    const timeoutService = new KnowledgeTaskService({ core: makeCore() });
    await timeoutService.prepare(prepareInput);
    await timeoutService.start(startInput, timeoutFake.paseo);
    const turnBeforeWait = timeoutService.store.get(prepareInput.taskId)?.turnId;
    const waited = await timeoutService.wait({ taskId: prepareInput.taskId, agentId: timeoutFake.agent.id, timeoutMs: 1_000 }, timeoutFake.paseo);
    expect(waited.error?.code).toBe("timeout");
    expect(waited.task).toMatchObject({ status: "timed_out", agentId: timeoutFake.agent.id, turnId: turnBeforeWait });
  });

  it("replays missed capture only after a verified completion and matching timeline identity", async () => {
    const core = makeCore();
    const timelineEntries: Array<Record<string, unknown>> = [];
    const fake = makePaseo({ timelineEntries });
    const service = new KnowledgeTaskService({ core });
    await service.prepare(prepareInput);
    await service.start(startInput, fake.paseo);

    fake.emitTimeline({
      agentId: fake.agent.id,
      event: { type: "turn_completed", provider: "codex", turnId: "turn_synthetic_p4_001" },
    });
    await vi.waitFor(() => expect(service.store.get(prepareInput.taskId)?.captureStatus).toBe("capture_incomplete"));
    expect(core.proposeNote).not.toHaveBeenCalled();

    timelineEntries.push({ turnId: "turn_synthetic_p4_001", item: { type: "assistant_message", text: "recovered synthetic result" } });
    const reconciled = await service.reconcile({
      taskId: prepareInput.taskId,
      agentId: fake.agent.id,
      workspaceId: prepareInput.workspaceId,
      turnId: "turn_synthetic_p4_001",
    }, fake.paseo);

    expect(core.proposeNote).toHaveBeenCalledWith(expect.objectContaining({
      taskId: prepareInput.taskId,
      turnId: "turn_synthetic_p4_001",
      resultText: "recovered synthetic result",
    }));
    expect(reconciled.task).toMatchObject({ status: "completed", captureStatus: "draft_created" });
  });

  it("never persists personal excerpts and exposes only a manual grant seam", async () => {
    const personalContext = { ...fixture, personal_context_used: true, source_manifest: [sourceRef], context: "private synthetic excerpt" };
    const core = makeCore(personalContext);
    const personalRoot = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-personal-"));
    tempDirs.push(personalRoot);
    mkdirSync(path.join(personalRoot, "Journal"));
    writeFileSync(path.join(personalRoot, "Journal", "note.md"), "synthetic private note\n", "utf8");
    const service = new KnowledgeTaskService({ core, personalVaultRoot: personalRoot });
    const grant = service.grantPersonal(prepareInput.taskId, "Journal/note.md");
    expect(grant).toMatchObject({ grantedPathCount: 1, task: { taskId: prepareInput.taskId } });
    expect(grant.task).not.toHaveProperty("path");
    const prepared = await service.prepare({ ...prepareInput, vaultScope: "personal" });
    expect(prepared.context).toEqual(personalContext);
    expect(service.store.get(prepareInput.taskId)?.context).toBeNull();
    const start = await service.start(startInput, makePaseo().paseo);
    expect(start.error?.code).toBe("needs_prepare");
    expect(service.revokePersonal(prepareInput.taskId).grantedPathCount).toBe(0);
  });

  it("hydrates task and agent/turn identity after plugin restart and captures a verified timeline once", async () => {
    const directory = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-task-state-"));
    tempDirs.push(directory);
    const taskStatePath = path.join(directory, "private", "task-state.json");
    const timelineEntries: Array<Record<string, unknown>> = [];
    const fake = makePaseo({ timelineEntries });
    const firstCore = makeCore();
    const firstService = new KnowledgeTaskService({ core: firstCore, taskStatePath });

    await firstService.prepare(prepareInput);
    await firstService.start(startInput, fake.paseo);
    fake.emitTimeline({
      agentId: fake.agent.id,
      event: { type: "turn_completed", provider: "codex", turnId: "turn_synthetic_p4_001" },
    });
    await vi.waitFor(() => expect(firstService.store.get(prepareInput.taskId)?.captureStatus).toBe("capture_incomplete"));
    expect(firstCore.proposeNote).not.toHaveBeenCalled();

    const savedBeforeRestart = JSON.parse(readFileSync(taskStatePath, "utf8")) as {
      records: Array<Record<string, unknown>>;
    };
    expect(savedBeforeRestart.records[0]).toMatchObject({
      taskId: prepareInput.taskId,
      workspaceId: prepareInput.workspaceId,
      agentId: fake.agent.id,
      turnId: "turn_synthetic_p4_001",
      verifiedCompletedTurnId: "turn_synthetic_p4_001",
      provider: prepareInput.provider,
      status: "completed",
      captureStatus: "capture_incomplete",
    });
    expect(statSync(taskStatePath).mode & 0o777).toBe(0o600);
    firstService.dispose();

    timelineEntries.push({
      turnId: "turn_synthetic_p4_001",
      item: { type: "assistant_message", text: "recovered synthetic result" },
    });
    const secondCore = makeCore();
    const reopenedService = new KnowledgeTaskService({ core: secondCore, taskStatePath });
    expect(reopenedService.store.get(prepareInput.taskId)).toMatchObject({
      agentId: fake.agent.id,
      turnId: "turn_synthetic_p4_001",
      verifiedCompletedTurnId: "turn_synthetic_p4_001",
      context: null,
    });

    const reconciled = await reopenedService.reconcile({
      taskId: prepareInput.taskId,
      agentId: fake.agent.id,
      workspaceId: prepareInput.workspaceId,
      turnId: "turn_synthetic_p4_001",
    }, fake.paseo);
    expect(reconciled.task).toMatchObject({ status: "completed", captureStatus: "draft_created" });
    expect(secondCore.proposeNote).toHaveBeenCalledTimes(1);
    await reopenedService.reconcile({
      taskId: prepareInput.taskId,
      agentId: fake.agent.id,
      workspaceId: prepareInput.workspaceId,
      turnId: "turn_synthetic_p4_001",
    }, fake.paseo);
    expect(secondCore.proposeNote).toHaveBeenCalledTimes(1);
    reopenedService.dispose();
  });

  it("drops malformed personal, absolute, and traversal refs from durable work task state", async () => {
    const directory = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-unsafe-state-"));
    tempDirs.push(directory);
    const taskStatePath = path.join(directory, "task-state.json");
    const nulPathSource = { ...sourceRef, source_id: "nul-path", path: "10-Projects/a\0.md" };
    const unsafeRefs = [
      { ...sourceRef, vault_id: "personal", path: "Journal/note.md" },
      { ...sourceRef, source_id: "absolute", path: "/Users/example/private.md" },
      { ...sourceRef, source_id: "tilde", path: "~/private.md" },
      { ...sourceRef, source_id: "traversal", path: "../outside.md" },
      { ...sourceRef, source_id: "nested-traversal", path: "notes/../outside.md" },
      { ...sourceRef, source_id: "dot", path: "./inside.md" },
      { ...sourceRef, source_id: "backslash", path: "private\\note.md" },
      { ...sourceRef, source_id: "bad-hash", content_hash: "not-a-content-hash" },
    { ...sourceRef, source_id: "bad-lines", line_start: Number.MAX_SAFE_INTEGER + 1 },
    { ...sourceRef, source_id: "bad-heading", heading: "h".repeat(257) },
      nulPathSource,
    ];
    const core = makeCore({ ...fixture, source_manifest: [sourceRef, ...unsafeRefs] });
    const service = new KnowledgeTaskService({ core, taskStatePath });

    await service.prepare(prepareInput);

    const durableText = readFileSync(taskStatePath, "utf8");
    const record = (JSON.parse(durableText) as { records: Array<{ sourceManifest: unknown }> }).records[0];
    expect(record.sourceManifest).toEqual([sourceRef]);
    expect(record.sourceManifest).not.toContainEqual(nulPathSource);
    expect(durableText).not.toContain("Journal/note.md");
    expect(durableText).not.toContain("/Users/example/private.md");
    expect(durableText).not.toContain("~/private.md");
    expect(durableText).not.toContain("../outside.md");
    expect(durableText).not.toContain("notes/../outside.md");
    expect(durableText).not.toContain("./inside.md");
    expect(durableText).not.toContain("private\\\\note.md");
    expect(durableText).not.toContain("not-a-content-hash");
    expect(durableText).not.toContain("bad-lines");
    expect(durableText).not.toContain("bad-heading");
    expect(durableText).not.toContain("10-Projects/a\\u0000.md");

    service.dispose();
    const reopened = new KnowledgeTaskService({ core: makeCore(), taskStatePath });
    expect(reopened.store.get(prepareInput.taskId)?.sourceManifest).toEqual([sourceRef]);
    expect(reopened.store.get(prepareInput.taskId)?.sourceManifest).not.toContainEqual(nulPathSource);
    reopened.dispose();
  });

  it("persists no context, session text, or personal grant paths and does not initialize state without an explicit path", async () => {
    const directory = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-private-state-"));
    const personalRoot = mkdtempSync(path.join(os.tmpdir(), "paseo-knowledge-personal-"));
    tempDirs.push(directory, personalRoot);
    mkdirSync(path.join(personalRoot, "Journal"));
    writeFileSync(path.join(personalRoot, "Journal", "note.md"), "private synthetic note\n", "utf8");
    const taskStatePath = path.join(directory, "task-state.json");
    const personalPath = path.join(personalRoot, "Journal", "note.md");
    const privateExcerpt = "PRIVATE_EXCERPT_MUST_NOT_BE_DURABLE";
    const core = makeCore({
      ...fixture,
      personal_context_used: true,
      source_manifest: [{ ...sourceRef, vault_id: "personal", path: "Journal/note.md" }],
      context: privateExcerpt,
      session_output: "SESSION_OUTPUT_MUST_NOT_BE_DURABLE",
    });
    const service = new KnowledgeTaskService({ core, personalVaultRoot: personalRoot, taskStatePath });
    expect(service.store.get(prepareInput.taskId)).toBeNull();
    service.grantPersonal(prepareInput.taskId, "Journal/note.md");
    await service.prepare({ ...prepareInput, vaultScope: "personal" });

    const durableText = readFileSync(taskStatePath, "utf8");
    expect(durableText).not.toContain(personalPath);
    expect(durableText).not.toContain("Journal/note.md");
    expect(durableText).not.toContain(privateExcerpt);
    expect(durableText).not.toContain("SESSION_OUTPUT_MUST_NOT_BE_DURABLE");
    const record = (JSON.parse(durableText) as { records: Array<Record<string, unknown>> }).records[0];
    expect(record).toMatchObject({ vaultScope: "personal", sourceManifest: [] });
    expect(record).not.toHaveProperty("query");
    expect(record).not.toHaveProperty("project");
    expect(record).not.toHaveProperty("context");
    expect(service.store.get(prepareInput.taskId)?.context).toBeNull();
    service.dispose();

    const reopened = new KnowledgeTaskService({ core, personalVaultRoot: personalRoot, taskStatePath });
    expect(reopened.store.grants.count(prepareInput.taskId)).toBe(0);
    expect(reopened.store.get(prepareInput.taskId)?.sourceManifest).toEqual([]);
    reopened.dispose();
    expect(resolveProductionTaskStatePath({ PASEO_KNOWLEDGE_RUNTIME_DIR: "/tmp/paseo-knowledge-runtime" })).toBe("/tmp/paseo-knowledge-runtime/task-state.json");
  });

  it("defines typed status/search/attachment seams and no formal-write contribution", () => {
    expect(PluginStatusRpc.name).toBe("knowledge.status");
    expect(PrepareKnowledgeTaskRpc.name).toBe("knowledge.task.prepare");
    expect(StartKnowledgeTaskRpc.name).toBe("knowledge.task.start");
    expect(KnowledgeAttachmentSource.search).toBe(KnowledgeAttachmentSearchRpc);
    expect(KnowledgeAttachmentSearchRpc.output.parse({ items: [] })).toEqual({ items: [] });
    const contributionSource = readFileSync(new URL("../index.ts", import.meta.url), "utf8");
    expect(contributionSource).not.toMatch(/write|review_apply|formal/i);
    expect(contributionSource).not.toContain("addClientSide");
    expect(contributionSource).not.toContain("addCommandCenterItem");
  });
});
