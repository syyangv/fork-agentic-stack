import { createHash } from "node:crypto";
import {
  closeSync, constants, existsSync, fstatSync, fsyncSync, lstatSync, mkdirSync,
  openSync, readFileSync, realpathSync, writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import type { HostedDraftCompletion, HostedDraftSink } from "./hosted-task.js";

const hash = (value: string): string => createHash("sha256").update(value, "utf8").digest("hex");
function requireValue(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
function identifier(value: unknown): asserts value is string {
  requireValue(typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$/.test(value), "Invalid identity");
}
function digest(value: unknown): asserts value is string {
  requireValue(typeof value === "string" && /^[a-f0-9]{64}$/.test(value), "Invalid digest");
}
function contains(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel === "" || (!isAbsolute(rel) && rel !== ".." && !rel.startsWith(`..${sep}`));
}
// Resolve even not-yet-created forbidden roots through their existing ancestors.
function canonical(path: string): string {
  if (existsSync(path)) return realpathSync(path);
  const parent = dirname(path);
  requireValue(parent !== path, "Unresolvable destination");
  return join(canonical(parent), path.slice(parent.length + (parent.endsWith(sep) ? 0 : 1)));
}
function privateDirectory(path: string): void {
  const stat = lstatSync(path);
  requireValue(stat.isDirectory() && !stat.isSymbolicLink() && (stat.mode & 0o777) === 0o700 &&
    stat.uid === process.getuid?.() && realpathSync(path) === path, "Unsafe or nonprivate runtime directory");
}

/** Private review drafts only; filesystem checks are not an OS sandbox. */
export class FileHostedDraftSink implements HostedDraftSink {
  private readonly runtime: string;
  private readonly forbidden: readonly string[];
  constructor(options: { privateRuntimeDir: string; forbiddenVaultRoots: readonly string[] }) {
    requireValue(isAbsolute(options.privateRuntimeDir), "Runtime must be absolute");
    requireValue(Array.isArray(options.forbiddenVaultRoots) && options.forbiddenVaultRoots.every(isAbsolute), "Forbidden roots must be absolute");
    this.runtime = resolve(options.privateRuntimeDir);
    this.forbidden = [...options.forbiddenVaultRoots];
  }

  public async capture(completion: HostedDraftCompletion, signal: AbortSignal): Promise<{
    draftId: string; executionId: string; outputDigest: string;
  }> {
    if (signal.aborted) throw new Error("Draft capture cancelled");
    requireValue(completion !== null && typeof completion === "object", "Invalid completion");
    const execution = completion.execution;
    requireValue(execution !== null && typeof execution === "object", "Invalid reservation");
    for (const key of ["taskId", "workspaceId", "provider", "executionId", "policyId", "policyVersion"] as const) identifier(execution[key]);
    digest(execution.inputDigest);
    requireValue(Number.isSafeInteger(execution.createdAt) && execution.createdAt >= 0, "Invalid reservation time");
    identifier(completion.completionId);
    identifier(completion.model);
    requireValue(typeof completion.output === "string" && completion.output.length > 0 && completion.output.length <= 65_536, "Invalid output limit");
    digest(completion.outputDigest);
    requireValue(hash(completion.output) === completion.outputDigest, "Output digest mismatch");
    requireValue(Array.isArray(completion.sources) && completion.sources.length > 0 && completion.sources.length <= 64, "Invalid sources limit");
    const seen = new Set<string>();
    const sources = completion.sources.map(source => {
      requireValue(source !== null && typeof source === "object", "Invalid source");
      identifier(source.sourceId);
      requireValue(!seen.has(source.sourceId), "Duplicate source identity");
      seen.add(source.sourceId);
      requireValue(typeof source.path === "string" && source.path.length > 0 && source.path.length <= 1_024 && !/[\x00-\x1f]/.test(source.path), "Invalid source path");
      digest(source.contentHash);
      digest(source.excerptHash);
      return { sourceId: source.sourceId, path: source.path, contentHash: source.contentHash, excerptHash: source.excerptHash };
    });
    const draftId = `draft-${hash(execution.executionId)}`;
    const receipt = { draftId, executionId: execution.executionId, outputDigest: completion.outputDigest };
    const record = {
      version: 1, status: "pending_review", ...receipt,
      reservation: {
        taskId: execution.taskId, workspaceId: execution.workspaceId, provider: execution.provider,
        executionId: execution.executionId, inputDigest: execution.inputDigest, policyId: execution.policyId,
        policyVersion: execution.policyVersion, createdAt: execution.createdAt,
      },
      completionId: completion.completionId, model: completion.model, output: completion.output, sources,
    };
    const serialized = `${JSON.stringify(record)}\n`;
    const directory = join(this.runtime, "hosted-drafts");
    const target = join(directory, `${draftId}.json`);
    privateDirectory(this.runtime);
    const destination = canonical(directory);
    for (const root of this.forbidden) {
      const forbidden = canonical(resolve(root));
      requireValue(!contains(forbidden, destination) && !contains(destination, forbidden) &&
        !contains(forbidden, this.runtime) && !contains(this.runtime, forbidden), "Vault-overlapping destination refused");
    }
    // Everything below is synchronous: no asynchronous write can outlive cancellation.
    if (signal.aborted) throw new Error("Draft capture cancelled");
    if (!existsSync(directory)) {
      mkdirSync(directory, { mode: 0o700 });
      const parent = openSync(this.runtime, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try { fsyncSync(parent); } finally { closeSync(parent); }
    }
    privateDirectory(directory);
    let fd: number;
    try {
      fd = openSync(target, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      const existing = openSync(target, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        const stat = fstatSync(existing);
        requireValue(stat.isFile() && stat.nlink === 1 && stat.uid === process.getuid?.() &&
          (stat.mode & 0o777) === 0o600 && stat.size === Buffer.byteLength(serialized), "Unsafe or corrupt existing draft");
        requireValue(readFileSync(existing, "utf8") === serialized, "Conflicting or corrupt existing draft");
        fsyncSync(existing);
      } finally { closeSync(existing); }
      const dir = openSync(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try { fsyncSync(dir); } finally { closeSync(dir); }
      return receipt;
    }
    // Failure deliberately leaves the exclusive file as a partial marker, never replaced.
    try { writeFileSync(fd, serialized, "utf8"); fsyncSync(fd); } finally { closeSync(fd); }
    const dir = openSync(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    try { fsyncSync(dir); } finally { closeSync(dir); }
    return receipt;
  }
}
