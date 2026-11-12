import { createHash } from "node:crypto";
import {
  closeSync, constants, fstatSync, fsyncSync, lstatSync, openSync,
  opendirSync, readSync, realpathSync, writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { z } from "zod";
import {
  HostedDraftDetailsSchema, HostedDraftSummarySchema,
  type HostedDraftDetails, type HostedDraftSummary,
} from "../shared/hosted-contracts.js";

export interface HostedDraftReview {
  list(workspaceId: string): Promise<{ drafts: HostedDraftSummary[]; unreadableCount: number }>;
  read(workspaceId: string, draftId: string): Promise<HostedDraftDetails>;
  reject(workspaceId: string, draftId: string, outputDigest: string): Promise<void>;
}

const MAX_BYTES = 524_288;
const id = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.:@/\-]{0,255}$/);
const digest = z.string().regex(/^[a-f0-9]{64}$/);
const draftIdSchema = z.string().regex(/^draft-[a-f0-9]{64}$/);
const sourceSchema = z.object({
  sourceId: id, path: z.string().min(1).max(1024).regex(/^[^\x00-\x1f]+$/),
  contentHash: digest, excerptHash: digest,
}).strict();
const recordSchema = z.object({
  version: z.literal(1), status: z.literal("pending_review"),
  draftId: draftIdSchema, executionId: id, outputDigest: digest,
  reservation: z.object({
    taskId: id, workspaceId: id, provider: id, executionId: id, inputDigest: digest,
    policyId: id, policyVersion: id, createdAt: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
  }).strict(),
  completionId: id, model: id, output: z.string().min(1).max(65_536),
  sources: z.array(sourceSchema).min(1).max(64),
}).strict();
const markerSchema = z.object({
  version: z.literal(1), status: z.literal("rejected"), draftId: draftIdSchema,
  taskId: id, workspaceId: id, executionId: id, outputDigest: digest,
}).strict();
type DraftRecord = z.infer<typeof recordSchema>;
const hash = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");
const fail = () => new Error("Hosted draft review operation rejected");
const code = (error: unknown) => (error as NodeJS.ErrnoException | null)?.code;
function check(condition: unknown): asserts condition { if (!condition) throw fail(); }
function contains(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel === "" || (!isAbsolute(rel) && rel !== ".." && !rel.startsWith(`..${sep}`));
}
function canonical(path: string): string {
  try { return realpathSync(path); }
  catch (error) {
    if (code(error) !== "ENOENT") throw error;
    // A dangling symlink is not a missing directory component.
    try { lstatSync(path); throw fail(); }
    catch (error) { if (code(error) !== "ENOENT") throw error; }
    const parent = dirname(path);
    check(parent !== path);
    return join(canonical(parent), path.slice(parent.length + (parent.endsWith(sep) ? 0 : 1)));
  }
}
function privateDirectory(path: string): void {
  const stat = lstatSync(path);
  check(stat.isDirectory() && (stat.mode & 0o777) === 0o700 &&
    stat.uid === process.getuid?.() && realpathSync(path) === path);
}
function privateFile(fd: number): ReturnType<typeof fstatSync> {
  const stat = fstatSync(fd);
  check(stat.isFile() && stat.nlink === 1 && stat.uid === process.getuid?.() &&
    (stat.mode & 0o777) === 0o600 && stat.size <= MAX_BYTES);
  return stat;
}
function readJson(path: string): unknown {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = privateFile(fd);
    const buffer = Buffer.alloc(MAX_BYTES);
    let length = 0;
    while (length < MAX_BYTES) {
      const count = readSync(fd, buffer, length, buffer.length - length, null);
      length += count;
      check(length <= MAX_BYTES);
      if (!count) break;
    }
    const after = privateFile(fd);
    check(length === stat.size && after.size === stat.size && after.mtimeMs === stat.mtimeMs && after.ctimeMs === stat.ctimeMs);
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, length)));
  } finally { closeSync(fd); }
}
function rejection(record: DraftRecord): z.infer<typeof markerSchema> {
  return {
    version: 1, status: "rejected", draftId: record.draftId, taskId: record.reservation.taskId,
    workspaceId: record.reservation.workspaceId, executionId: record.executionId, outputDigest: record.outputDigest,
  };
}

/** Private local store, not a sandbox against same-user concurrent filesystem mutation.
 * This surface never applies output or changes/deletes original drafts or sources.
 */
export class FileHostedDraftReview implements HostedDraftReview {
  private readonly runtime: string;
  private readonly directory: string;
  private readonly forbidden: readonly string[];

  constructor(options: { privateRuntimeDir: string; forbiddenVaultRoots: readonly string[] }) {
    check(typeof options.privateRuntimeDir === "string" && isAbsolute(options.privateRuntimeDir) &&
      !options.privateRuntimeDir.includes("\0"));
    check(Array.isArray(options.forbiddenVaultRoots) && options.forbiddenVaultRoots.every(
      root => typeof root === "string" && isAbsolute(root) && !root.includes("\0")));
    this.runtime = resolve(options.privateRuntimeDir);
    this.directory = join(this.runtime, "hosted-drafts");
    this.forbidden = [...options.forbiddenVaultRoots];
  }

  private store(): boolean {
    privateDirectory(this.runtime);
    for (const root of this.forbidden) {
      const forbidden = canonical(resolve(root));
      check(!contains(forbidden, this.runtime) && !contains(this.runtime, forbidden));
    }
    try { privateDirectory(this.directory); return true; }
    catch (error) { if (code(error) === "ENOENT") return false; throw error; }
  }

  private load(draftId: string): DraftRecord {
    const record = recordSchema.parse(readJson(join(this.directory, `${draftId}.json`)));
    check(record.draftId === draftId && record.executionId === record.reservation.executionId &&
      record.draftId === `draft-${hash(record.executionId)}` && hash(record.output) === record.outputDigest &&
      new Set(record.sources.map(source => source.sourceId)).size === record.sources.length);
    return record;
  }

  private status(record: DraftRecord): "pending_review" | "rejected" {
    try {
      const marker = markerSchema.parse(readJson(join(this.directory, `${record.draftId}.review.json`)));
      check(JSON.stringify(marker) === JSON.stringify(rejection(record)));
      return "rejected";
    } catch (error) { if (code(error) === "ENOENT") return "pending_review"; throw error; }
  }

  private details(record: DraftRecord): HostedDraftDetails {
    return HostedDraftDetailsSchema.parse({
      draftId: record.draftId, taskId: record.reservation.taskId, workspaceId: record.reservation.workspaceId,
      model: record.model, completionId: record.completionId, outputDigest: record.outputDigest,
      status: this.status(record), output: record.output, sources: record.sources,
    });
  }

  async list(workspaceId: string): Promise<{ drafts: HostedDraftSummary[]; unreadableCount: number }> {
    try {
      id.parse(workspaceId);
      if (!this.store()) return { drafts: [], unreadableCount: 0 };
      const inventory: string[] = [];
      const dir = opendirSync(this.directory);
      try {
        for (let entry = dir.readSync(); entry; entry = dir.readSync()) {
          check(inventory.length < 256);
          inventory.push(entry.name);
        }
      } finally { dir.closeSync(); }
      const drafts: HostedDraftSummary[] = [];
      let unreadableCount = 0;
      const names = new Set(inventory);
      for (const name of inventory.sort()) {
        if (/^draft-[a-f0-9]{64}\.review\.json$/.test(name) && names.has(name.replace(".review.json", ".json"))) continue;
        if (!/^draft-[a-f0-9]{64}\.json$/.test(name)) { unreadableCount++; continue; }
        try {
          const record = this.load(name.slice(0, -5));
          if (record.reservation.workspaceId !== workspaceId) continue;
          const details = this.details(record);
          if (drafts.length < 50) drafts.push(HostedDraftSummarySchema.parse(details));
        } catch { unreadableCount++; }
      }
      return { drafts, unreadableCount };
    } catch { throw fail(); }
  }

  async read(workspaceId: string, draftId: string): Promise<HostedDraftDetails> {
    try {
      id.parse(workspaceId); draftIdSchema.parse(draftId);
      check(this.store());
      const record = this.load(draftId);
      check(record.reservation.workspaceId === workspaceId);
      return this.details(record);
    } catch { throw fail(); }
  }

  async reject(workspaceId: string, draftId: string, outputDigest: string): Promise<void> {
    try {
      id.parse(workspaceId); draftIdSchema.parse(draftId); digest.parse(outputDigest);
      check(this.store());
      const record = this.load(draftId);
      check(record.reservation.workspaceId === workspaceId && record.outputDigest === outputDigest);
      // Validate any existing marker before attempting creation. No unknown marker is replaced.
      this.status(record);
      const path = join(this.directory, `${draftId}.review.json`);
      let fd: number;
      try { fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600); }
      catch (error) {
        if (code(error) !== "EEXIST") throw error;
        check(this.status(record) === "rejected");
        fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        try { privateFile(fd); fsyncSync(fd); } finally { closeSync(fd); }
        this.syncStore();
        return;
      }
      // On interruption retain a partial exclusive marker; all future access fails closed.
      try { writeFileSync(fd, `${JSON.stringify(rejection(record))}\n`, "utf8"); fsyncSync(fd); }
      finally { closeSync(fd); }
      this.syncStore();
    } catch { throw fail(); }
  }

  private syncStore(): void {
    const fd = openSync(this.directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    try { fsyncSync(fd); } finally { closeSync(fd); }
  }
}
