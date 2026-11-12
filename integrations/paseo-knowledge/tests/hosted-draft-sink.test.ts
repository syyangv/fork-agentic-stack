import { createHash } from "node:crypto";
import { chmodSync, fsyncSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FileHostedDraftSink } from "../server/hosted-draft-sink.js";
import type { HostedDraftCompletion } from "../server/hosted-task.js";
// Mock only the two commit operations; all paths remain synthetic temp directories.
vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs")>();
  return { ...actual, writeFileSync: vi.fn(actual.writeFileSync), fsyncSync: vi.fn(actual.fsyncSync) };
});

const hash = (s: string) => createHash("sha256").update(s).digest("hex");
const roots: string[] = [];
afterEach(() => { vi.mocked(writeFileSync).mockReset(); vi.mocked(fsyncSync).mockReset(); for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });
function fixture() {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "draft-sink-test-")); roots.push(root);
  const runtime = join(root, "runtime"); mkdirSync(runtime, { mode: 0o700 });
  const vault = join(root, "vault"); mkdirSync(vault, { mode: 0o700 });
  const sink = new FileHostedDraftSink({ privateRuntimeDir: runtime, forbiddenVaultRoots: [vault] });
  const candidate = {
    execution: { taskId: "task-1", workspaceId: "ws-1", provider: "offline", executionId: "exec-1", inputDigest: hash("input"), policyId: "policy-1", policyVersion: "v1", createdAt: 1 },
    completionId: "completion-1", model: "synthetic", output: "private draft", outputDigest: hash("private draft"),
    sources: [{ sourceId: "source-1", path: "work/note.md", contentHash: hash("raw source must not persist"), excerptHash: hash("raw source must not persist"), text: "raw source must not persist" }],
    prompt: "prompt must not persist", credentials: "credential must not persist",
  } as unknown as HostedDraftCompletion;
  return { root, runtime, vault, sink, candidate, signal: new AbortController().signal };
}
describe("private hosted draft sink", () => {
  it("persists only explicit private pending-review data and receipt", async () => {
    const f = fixture(); const receipt = await f.sink.capture(f.candidate, f.signal);
    const dir = join(f.runtime, "hosted-drafts"); const path = join(dir, `${receipt.draftId}.json`);
    const raw = readFileSync(path, "utf8"); const record = JSON.parse(raw);
    expect(record.status).toBe("pending_review"); expect(record.output).toBe(f.candidate.output);
    expect(receipt).toEqual({ draftId: expect.stringMatching(/^draft-[a-f0-9]{64}$/), executionId: "exec-1", outputDigest: hash("private draft") });
    expect(statSync(dir).mode & 0o777).toBe(0o700); expect(statSync(path).mode & 0o777).toBe(0o600);
    for (const secret of ["raw source must not persist", "prompt must not persist", "credential must not persist"]) expect(raw).not.toContain(secret);
    expect(record.sources).toEqual([{ sourceId: "source-1", path: "work/note.md", contentHash: hash("raw source must not persist"), excerptHash: hash("raw source must not persist") }]);
  });
  it("aborted capture creates no draft", async () => {
    const f = fixture(); const controller = new AbortController(); controller.abort();
    await expect(f.sink.capture(f.candidate, controller.signal)).rejects.toThrow();
    await new Promise<void>(resolve => setImmediate(resolve));
    expect(readdirSync(f.runtime)).toEqual([]);
    expect(vi.mocked(writeFileSync)).not.toHaveBeenCalled();
    expect(vi.mocked(fsyncSync)).not.toHaveBeenCalled();
  });
  it("is idempotent but never overwrites conflicting or corrupt records", async () => {
    const f = fixture(); const receipt = await f.sink.capture(f.candidate, f.signal);
    expect(await f.sink.capture(f.candidate, f.signal)).toEqual(receipt);
    const path = join(f.runtime, "hosted-drafts", `${receipt.draftId}.json`); const original = readFileSync(path, "utf8");
    await expect(f.sink.capture({ ...f.candidate, completionId: "other" }, f.signal)).rejects.toThrow(); expect(readFileSync(path, "utf8")).toBe(original);
    writeFileSync(path, "partial"); await expect(f.sink.capture(f.candidate, f.signal)).rejects.toThrow(); expect(readFileSync(path, "utf8")).toBe("partial");
  });
  it("rejects output tampering and malformed identities before writes", async () => {
    const f = fixture();
    await expect(f.sink.capture({ ...f.candidate, output: "tampered" }, f.signal)).rejects.toThrow(/digest/);
    await expect(f.sink.capture({ ...f.candidate, execution: { ...f.candidate.execution, executionId: "" } }, f.signal)).rejects.toThrow();
    await expect(f.sink.capture({ ...f.candidate, sources: [{ ...f.candidate.sources[0]!, excerptHash: "invalid" }] }, f.signal)).rejects.toThrow(/digest/);
    expect(readdirSync(f.runtime)).toEqual([]);
  });
  it("refuses nonprivate runtimes, overlapping roots, and symlink destinations", async () => {
    const f = fixture(); chmodSync(f.runtime, 0o755); await expect(f.sink.capture(f.candidate, f.signal)).rejects.toThrow(); chmodSync(f.runtime, 0o700);
    for (const forbidden of [f.root, join(f.runtime, "vault")]) {
      const sink = new FileHostedDraftSink({ privateRuntimeDir: f.runtime, forbiddenVaultRoots: [forbidden] });
      await expect(sink.capture(f.candidate, f.signal)).rejects.toThrow(/Vault/);
    }
    symlinkSync(f.vault, join(f.runtime, "hosted-drafts")); await expect(f.sink.capture(f.candidate, f.signal)).rejects.toThrow(); expect(readdirSync(f.vault)).toEqual([]);
    const alias = join(f.root, "alias"); symlinkSync(f.runtime, alias);
    await expect(new FileHostedDraftSink({ privateRuntimeDir: alias, forbiddenVaultRoots: [] }).capture(f.candidate, f.signal)).rejects.toThrow();
  });
});

describe("private draft commit failures", () => {
  it("leaves a partial exclusive marker on write failure and never writes it again", async () => {
    const f = fixture();
    const actual = await vi.importActual<typeof import("node:fs")>("node:fs");
    const writes = vi.mocked(writeFileSync);
    writes.mockImplementationOnce((file) => {
      actual.writeFileSync(file, "{partial");
      throw Object.assign(new Error("synthetic write failure"), { code: "EIO" });
    });
    await expect(f.sink.capture(f.candidate, f.signal)).rejects.toThrow("synthetic write failure");
    const dir = join(f.runtime, "hosted-drafts");
    expect(readdirSync(dir)).toHaveLength(1);
    const path = join(dir, readdirSync(dir)[0]!);
    const marker = statSync(path);
    expect(marker.mode & 0o777).toBe(0o600);
    expect(readFileSync(path, "utf8")).toBe("{partial");
    await expect(f.sink.capture(f.candidate, f.signal)).rejects.toThrow(/corrupt/);
    expect(writes).toHaveBeenCalledTimes(1);
    expect(readFileSync(path, "utf8")).toBe("{partial");
    expect(statSync(path).ino).toBe(marker.ino);
  });

  it("rejects without a receipt when file fsync fails", async () => {
    const f = fixture();
    mkdirSync(join(f.runtime, "hosted-drafts"), { mode: 0o700 });
    vi.mocked(fsyncSync).mockImplementationOnce(() => { throw new Error("synthetic file fsync failure"); });
    await expect(f.sink.capture(f.candidate, f.signal)).rejects.toThrow("synthetic file fsync failure");
    const dir = join(f.runtime, "hosted-drafts");
    expect(readdirSync(dir)).toHaveLength(1);
    expect(JSON.parse(readFileSync(join(dir, readdirSync(dir)[0]!), "utf8")).status).toBe("pending_review");
    expect(vi.mocked(writeFileSync)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(fsyncSync)).toHaveBeenCalledTimes(1);
  });

  it("rejects without a receipt when directory fsync fails after the file was synced", async () => {
    const f = fixture();
    mkdirSync(join(f.runtime, "hosted-drafts"), { mode: 0o700 });
    const actual = await vi.importActual<typeof import("node:fs")>("node:fs");
    vi.mocked(fsyncSync)
      .mockImplementationOnce(actual.fsyncSync)
      .mockImplementationOnce(() => { throw new Error("synthetic directory fsync failure"); });
    await expect(f.sink.capture(f.candidate, f.signal)).rejects.toThrow("synthetic directory fsync failure");
    expect(vi.mocked(fsyncSync)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(writeFileSync)).toHaveBeenCalledTimes(1);
    expect(readdirSync(join(f.runtime, "hosted-drafts"))).toHaveLength(1);
  });
});
