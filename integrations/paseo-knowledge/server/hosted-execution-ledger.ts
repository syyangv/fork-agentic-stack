import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, realpath, rename, unlink } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { z } from "zod";

export interface HostedReservation {
  readonly taskId: string;
  readonly workspaceId: string;
  readonly provider: string;
  readonly executionId: string;
  readonly inputDigest: string;
  readonly policyId: string;
  readonly policyVersion: string;
  readonly createdAt: number;
}

export interface HostedLedgerOutcome {
  readonly status: "completed" | "failed" | "cancelled" | "timeout" | "ambiguous" | "capture_incomplete";
  readonly completionId?: string;
  readonly draftId?: string;
  readonly code?: string;
}

export interface HostedExecutionLedger {
  reserve(record: HostedReservation): Promise<boolean>;
  finish(record: HostedReservation, outcome: HostedLedgerOutcome): Promise<void>;
}

// Identifiers, not free-form messages. Unknown input keys are stripped; disk schemas are strict.
const identifier = z.string().min(1).max(256).regex(/^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$/);
const reservationSchema = z.object({
  taskId: identifier,
  workspaceId: identifier,
  provider: identifier,
  executionId: identifier,
  inputDigest: z.string().regex(/^[a-fA-F0-9]{64}$/),
  policyId: identifier,
  policyVersion: identifier,
  createdAt: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
});
const outcomeSchema = z.object({
  status: z.enum(["completed", "failed", "cancelled", "timeout", "ambiguous", "capture_incomplete"]),
  completionId: identifier.optional(),
  draftId: identifier.optional(),
  code: identifier.optional(),
});
const diskSchema = z.object({
  version: z.literal(1),
  reservation: reservationSchema.strict(),
  outcome: outcomeSchema.strict().optional(),
}).strict();
const failure = () => new Error("Hosted execution ledger operation rejected");
const errorCode = (error: unknown): string | undefined =>
  typeof error === "object" && error !== null && "code" in error ? String(error.code) : undefined;

/** Private local filesystem only: do not share with untrusted writers or use on NFS.
 * Reservations and finish claims are never reclaimed, even after a crash. This
 * deliberately sacrifices availability for no replay. A failed fsync is not success.
 */
export class FileHostedExecutionLedger implements HostedExecutionLedger {
  private readonly directory: string;

  constructor(directory: string) {
    if (typeof directory !== "string" || !directory || directory.includes("\0")) throw failure();
    this.directory = resolve(directory);
  }

  private path(record: HostedReservation): string {
    return join(this.directory, `${createHash("sha256").update(record.taskId).digest("hex")}.json`);
  }

  private async prepare(): Promise<void> {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    const stat = await lstat(this.directory);
    // Refuse insecure existing directories rather than changing another owner's permissions.
    if (!stat.isDirectory() || (stat.mode & 0o777) !== 0o700 || stat.uid !== process.getuid?.()) throw failure();
  }

  private async syncDirectory(path = this.directory): Promise<void> {
    const directory = await open(path, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    try {
      // Unsupported metadata flushes are failures too: never acknowledge a
      // reservation whose directory-entry durability could not be enforced.
      await directory.sync();
    } finally { await directory.close(); }
  }

  private async syncReservationDirectoryChain(): Promise<void> {
    // Flush bottom-up: the leaf contains the marker, its parent contains the
    // leaf's name, and so on. Flush even existing ancestors: another instance
    // may have just created them but not yet made their directory entries durable.
    // Resolve platform aliases (e.g. /var on macOS) before O_NOFOLLOW opens.
    let path = await realpath(this.directory);
    for (;;) {
      await this.syncDirectory(path);
      const parent = dirname(path);
      if (parent === path) return;
      path = parent;
    }
  }

  async reserve(record: HostedReservation): Promise<boolean> {
    try {
      const reservation = reservationSchema.parse(record);
      await this.prepare();
      let file;
      try { file = await open(this.path(reservation), "wx", 0o600); }
      catch (error) { if (errorCode(error) === "EEXIST") return false; throw error; }
      // Never remove a partially written marker: an interrupted reservation is spent.
      try {
        await file.writeFile(JSON.stringify({ version: 1, reservation }));
        await file.sync();
      } finally { await file.close(); }
      await this.syncReservationDirectoryChain();
      return true;
    } catch { throw failure(); }
  }

  async finish(record: HostedReservation, outcome: HostedLedgerOutcome): Promise<void> {
    let temporary: string | undefined;
    try {
      const reservation = reservationSchema.parse(record);
      const terminal = outcomeSchema.parse(outcome);
      await this.prepare();
      const path = this.path(reservation);
      const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      let original;
      try {
        original = await file.stat();
        if (!original.isFile() || original.nlink !== 1 || original.size > 16384 ||
            (original.mode & 0o777) !== 0o600 || original.uid !== process.getuid?.()) throw failure();
        const stored = diskSchema.parse(JSON.parse(await file.readFile("utf8")));
        if (stored.outcome || JSON.stringify(stored.reservation) !== JSON.stringify(reservation)) throw failure();
      } finally { await file.close(); }
      // Permanent exclusive claim serializes finishes across processes/instances.
      // A crash or later failure leaves both reservation and claim in place.
      const claim = await open(`${path}.finish`, "wx", 0o600);
      try { await claim.sync(); } finally { await claim.close(); }
      await this.syncDirectory();
      const candidate = join(this.directory, `.${randomUUID()}.tmp`);
      const replacement = await open(candidate, "wx", 0o600);
      temporary = candidate;
      try {
        await replacement.writeFile(JSON.stringify({ version: 1, reservation, outcome: terminal }));
        await replacement.sync();
      } finally { await replacement.close(); }
      const current = await lstat(path);
      if (!current.isFile() || current.dev !== original.dev || current.ino !== original.ino) throw failure();
      await rename(candidate, path);
      temporary = undefined;
      await this.syncDirectory();
    } catch { throw failure(); }
    finally {
      // Only our exclusively-created temporary file may be removed.
      if (temporary) await unlink(temporary).catch(() => undefined);
    }
  }
}
