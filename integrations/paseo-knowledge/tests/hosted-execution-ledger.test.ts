import { createHash } from "node:crypto";
import { chmod, lstat, mkdir, mkdtemp, open, realpath, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FileHostedExecutionLedger, type HostedReservation } from "../server/hosted-execution-ledger.js";

const reservation: HostedReservation = {
  taskId: "task_1", workspaceId: "workspace_1", provider: "codex/gpt-5.4",
  executionId: "execution_1", inputDigest: "a".repeat(64), policyId: "offline", policyVersion: "1", createdAt: 100,
};
let root: string;
let directory: string;
let ledger: FileHostedExecutionLedger;
const marker = (record = reservation) => join(directory, `${createHash("sha256").update(record.taskId).digest("hex")}.json`);
beforeEach(async () => {
  root = await mkdtemp(join(tmpdir(), "hosted-ledger-test-"));
  directory = join(root, "ledger");
  ledger = new FileHostedExecutionLedger(directory);
});
afterEach(async () => { await rm(root, { recursive: true, force: true }); });

describe("durable hosted execution ledger", () => {
  it("grants exactly one concurrent reservation across instances and changed identities", async () => {
    const results = await Promise.all(Array.from({ length: 24 }, (_, i) =>
      new FileHostedExecutionLedger(directory).reserve({ ...reservation, executionId: `exec_${i}`, workspaceId: `workspace_${i}` })));
    expect(results.filter(Boolean)).toHaveLength(1);
    expect(await ledger.reserve(reservation)).toBe(false);
  });

  it("survives restart before and after finish and uses private modes", async () => {
    expect(await ledger.reserve(reservation)).toBe(true);
    expect((await lstat(directory)).mode & 0o777).toBe(0o700);
    expect((await lstat(marker())).mode & 0o777).toBe(0o600);
    expect(await new FileHostedExecutionLedger(directory).reserve(reservation)).toBe(false);
    await new FileHostedExecutionLedger(directory).finish(reservation, { status: "completed", completionId: "completion_1" });
    expect(JSON.parse(await readFile(marker(), "utf8")).outcome).toEqual({ status: "completed", completionId: "completion_1" });
    expect(await new FileHostedExecutionLedger(directory).reserve({ ...reservation, executionId: "other" })).toBe(false);
    for (const name of await readdir(directory)) expect((await lstat(join(directory, name))).mode & 0o777).toBe(0o600);
  });

  it.each(Object.keys(reservation) as (keyof HostedReservation)[])("rejects mismatched %s without modifying the marker", async (key) => {
    await ledger.reserve(reservation);
    const before = await readFile(marker(), "utf8");
    const other = { ...reservation, [key]: key === "createdAt" ? 101 : key === "inputDigest" ? "b".repeat(64) : "other" };
    await expect(ledger.finish(other, { status: "failed" })).rejects.toThrow("Hosted execution ledger operation rejected");
    expect(await readFile(marker(), "utf8")).toBe(before);
    await ledger.finish(reservation, { status: "failed" });
  });

  it.each(["", "{", "null", '{"version":999}', JSON.stringify({ version: 1, reservation, unexpected: "synthetic" })])("denies corrupt markers without overwriting: %s", async (content) => {
    await mkdir(directory, { mode: 0o700 });
    await writeFile(marker(), content, { mode: 0o600 });
    expect(await ledger.reserve(reservation)).toBe(false);
    await expect(ledger.finish(reservation, { status: "failed" })).rejects.toThrow();
    expect(await readFile(marker(), "utf8")).toBe(content);
  });

  it("persists only whitelisted metadata, stripping content and credential fields", async () => {
    const extra = { prompt: "SYNTHETIC_PROMPT", source: "SYNTHETIC_SOURCE", output: "SYNTHETIC_OUTPUT", credential: "SYNTHETIC_CREDENTIAL" };
    await ledger.reserve({ ...reservation, ...extra });
    await ledger.finish(reservation, { status: "capture_incomplete", ...extra });
    expect(JSON.parse(await readFile(marker(), "utf8"))).toEqual({ version: 1, reservation, outcome: { status: "capture_incomplete" } });
    expect((await readdir(directory)).sort()).toEqual([marker().split("/").pop(), `${marker().split("/").pop()}.finish`].sort());
  });

  it("allows only one concurrent terminal update and never rewrites a finished record", async () => {
    await ledger.reserve(reservation);
    const results = await Promise.allSettled(Array.from({ length: 12 }, (_, i) =>
      new FileHostedExecutionLedger(directory).finish(reservation, { status: "failed", code: `failure_${i}` })));
    expect(results.filter(r => r.status === "fulfilled")).toHaveLength(1);
    const before = await readFile(marker(), "utf8");
    await expect(ledger.finish(reservation, { status: "completed" })).rejects.toThrow();
    expect(await readFile(marker(), "utf8")).toBe(before);
  });

  it("preserves a reservation when an exclusive finish claim already exists", async () => {
    await ledger.reserve(reservation);
    const before = await readFile(marker(), "utf8");
    await writeFile(`${marker()}.finish`, "interrupted", { mode: 0o600 });
    await expect(ledger.finish(reservation, { status: "completed" })).rejects.toThrow();
    expect(await readFile(marker(), "utf8")).toBe(before);
    expect(await ledger.reserve(reservation)).toBe(false);
  });

  it("rejects missing and symlinked reservations, leaving targets unchanged", async () => {
    await expect(ledger.finish(reservation, { status: "failed" })).rejects.toThrow();
    const target = join(root, "target");
    await writeFile(target, "untouched", { mode: 0o600 });
    await symlink(target, marker());
    expect(await ledger.reserve(reservation)).toBe(false);
    await expect(ledger.finish(reservation, { status: "failed" })).rejects.toThrow();
    expect(await readFile(target, "utf8")).toBe("untouched");
  });

  it("rejects unsafe directories and metadata before persisting", async () => {
    for (const record of [
      { ...reservation, taskId: "" }, { ...reservation, executionId: "x".repeat(257) },
      { ...reservation, policyId: "line\nbreak" }, { ...reservation, inputDigest: "invalid" },
      { ...reservation, createdAt: -1 }, { ...reservation, createdAt: Infinity },
    ]) await expect(ledger.reserve(record)).rejects.toThrow();
    await mkdir(directory, { mode: 0o700 });
    await chmod(directory, 0o755);
    await expect(ledger.reserve(reservation)).rejects.toThrow();
    await chmod(directory, 0o700);
    expect(await readdir(directory)).toEqual([]);
    await ledger.reserve(reservation);
    await expect(ledger.finish(reservation, { status: "completed", code: "x".repeat(257) })).rejects.toThrow();
    await ledger.finish(reservation, { status: "timeout" });
  });
});

// Inject failures only on handles in this test's synthetic directory.
describe("flush and interrupted-update safety", () => {
  it("flushes file then directory before acknowledging a reservation", async () => {
    const handle = await open(join(root, "probe"), "wx", 0o600);
    const prototype = Object.getPrototypeOf(handle) as typeof handle;
    const original = prototype.sync;
    const flushed: string[] = [];
    const spy = vi.spyOn(prototype, "sync").mockImplementation(async function (this: typeof handle) {
      flushed.push((await this.stat()).isDirectory() ? "directory" : "file");
      await original.call(this);
    });
    try {
      expect(await ledger.reserve(reservation)).toBe(true);
      const ancestors: string[] = [];
      let path = await realpath(directory);
      for (;;) {
        ancestors.push("directory");
        const parent = dirname(path);
        if (parent === path) break;
        path = parent;
      }
      expect(flushed).toEqual(["file", ...ancestors]);
    } finally { spy.mockRestore(); await handle.close(); }
  });

  it.each(["file", "directory"])("does not acknowledge a failed %s flush and retains the marker", async (kind) => {
    const handle = await open(join(root, "probe"), "wx", 0o600);
    const prototype = Object.getPrototypeOf(handle) as typeof handle;
    const original = prototype.sync;
    const spy = vi.spyOn(prototype, "sync").mockImplementation(async function (this: typeof handle) {
      const type = (await this.stat()).isDirectory() ? "directory" : "file";
      if (type === kind) throw Object.assign(new Error("synthetic I/O failure"), { code: "EIO" });
      await original.call(this);
    });
    try { await expect(ledger.reserve(reservation)).rejects.toThrow(); }
    finally { spy.mockRestore(); await handle.close(); }
    expect(await new FileHostedExecutionLedger(directory).reserve(reservation)).toBe(false);
    expect(JSON.parse(await readFile(marker(), "utf8")).reservation).toEqual(reservation);
  });

  it("preserves the original marker if flushing its replacement fails", async () => {
    await ledger.reserve(reservation);
    const before = await readFile(marker(), "utf8");
    const handle = await open(join(root, "probe"), "wx", 0o600);
    const prototype = Object.getPrototypeOf(handle) as typeof handle;
    const original = prototype.sync;
    const spy = vi.spyOn(prototype, "sync").mockImplementation(async function (this: typeof handle) {
      const stat = await this.stat();
      if (stat.isFile() && stat.size > 0) throw new Error("synthetic replacement failure");
      await original.call(this);
    });
    try { await expect(ledger.finish(reservation, { status: "completed" })).rejects.toThrow(); }
    finally { spy.mockRestore(); await handle.close(); }
    expect(await readFile(marker(), "utf8")).toBe(before);
    expect(await ledger.reserve(reservation)).toBe(false);
    expect((await readdir(directory)).some(name => name.endsWith(".tmp"))).toBe(false);
  });
});

describe("directory entry durability", () => {
  it.each([false, true])("flushes every named parent bottom-up before success (pre-existing chain: %s)", async (existing) => {
    directory = join(root, "new", "nested", "ledger");
    ledger = new FileHostedExecutionLedger(directory);
    // Simulate a peer that has created the chain but not flushed it yet.
    if (existing) await mkdir(directory, { recursive: true, mode: 0o700 });
    const canonicalRoot = await realpath(root);
    const expected: string[] = [];
    let path = join(canonicalRoot, "new", "nested", "ledger");
    for (;;) {
      expected.push(path);
      const parent = dirname(path);
      if (parent === path) break;
      path = parent;
    }
    const handle = await open(join(root, "probe"), "wx", 0o600);
    const prototype = Object.getPrototypeOf(handle) as typeof handle;
    const original = prototype.sync;
    const flushed: string[] = [];
    const spy = vi.spyOn(prototype, "sync").mockImplementation(async function (this: typeof handle) {
      const stat = await this.stat();
      if (stat.isDirectory()) {
        const matches = await Promise.all(expected.map(async name => {
          const candidate = await lstat(name);
          return candidate.dev === stat.dev && candidate.ino === stat.ino ? name : undefined;
        }));
        const name = matches.find(value => value !== undefined);
        expect(name).toBeDefined();
        flushed.push(name!);
      } else flushed.push("marker");
      await original.call(this);
    });
    try {
      expect(await ledger.reserve(reservation)).toBe(true);
      expect(flushed).toEqual(["marker", ...expected]);
    } finally { spy.mockRestore(); await handle.close(); }
  });

  it("rejects parent-entry fsync failure without acknowledging or removing the marker", async () => {
    directory = join(root, "new", "nested", "ledger");
    ledger = new FileHostedExecutionLedger(directory);
    const handle = await open(join(root, "probe"), "wx", 0o600);
    const prototype = Object.getPrototypeOf(handle) as typeof handle;
    const original = prototype.sync;
    let failedParent = false;
    const spy = vi.spyOn(prototype, "sync").mockImplementation(async function (this: typeof handle) {
      const stat = await this.stat();
      if (stat.isDirectory()) {
        const parent = await lstat(dirname(directory));
        if (stat.dev === parent.dev && stat.ino === parent.ino) {
          failedParent = true;
          throw Object.assign(new Error("synthetic parent fsync failure"), { code: "EIO" });
        }
      }
      await original.call(this);
    });
    try { await expect(ledger.reserve(reservation)).rejects.toThrow(); }
    finally { spy.mockRestore(); await handle.close(); }
    expect(failedParent).toBe(true);
    expect(JSON.parse(await readFile(marker(), "utf8")).reservation).toEqual(reservation);
    expect(await new FileHostedExecutionLedger(directory).reserve(reservation)).toBe(false);
  });
});


describe("unsupported metadata flushes fail closed", () => {
  it.each(["EINVAL", "ENOTSUP", "EOPNOTSUPP"])("retains spent marker and rejects unsupported directory fsync: %s", async (code) => {
    const handle = await open(join(root, "probe"), "wx", 0o600);
    const prototype = Object.getPrototypeOf(handle) as typeof handle;
    const original = prototype.sync;
    const spy = vi.spyOn(prototype, "sync").mockImplementation(async function (this: typeof handle) {
      if ((await this.stat()).isDirectory()) throw Object.assign(new Error("synthetic unsupported metadata flush"), { code });
      await original.call(this);
    });
    try { await expect(ledger.reserve(reservation)).rejects.toThrow("Hosted execution ledger operation rejected"); }
    finally { spy.mockRestore(); await handle.close(); }
    expect(JSON.parse(await readFile(marker(), "utf8")).reservation).toEqual(reservation);
    expect(await new FileHostedExecutionLedger(directory).reserve(reservation)).toBe(false);
  });
});
