import { createHash } from "node:crypto";
import {
  chmodSync, fstatSync, fsyncSync, linkSync, mkdirSync, mkdtempSync, readFileSync,
  readdirSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FileHostedDraftSink } from "../server/hosted-draft-sink.js";
import { FileHostedDraftReview } from "../server/hosted-draft-review.js";
import type { HostedDraftCompletion } from "../server/hosted-task.js";

vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs")>();
  return { ...actual, fsyncSync: vi.fn(actual.fsyncSync), writeFileSync: vi.fn(actual.writeFileSync) };
});
const hash = (text: string) => createHash("sha256").update(text).digest("hex");
const roots: string[] = [];
afterEach(() => {
  vi.mocked(fsyncSync).mockReset(); vi.mocked(writeFileSync).mockReset();
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});
function fixture() {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "draft-review-test-")); roots.push(root);
  const runtime = join(root, "runtime"); mkdirSync(runtime, { mode: 0o700 });
  const vault = join(root, "synthetic-vault"); mkdirSync(vault, { mode: 0o700 });
  const source = join(vault, "note.md"); writeFileSync(source, "original synthetic source", { mode: 0o600 });
  const options = { privateRuntimeDir: runtime, forbiddenVaultRoots: [vault] };
  const sink = new FileHostedDraftSink(options);
  const review = new FileHostedDraftReview(options);
  const completion: HostedDraftCompletion = {
    execution: {
      taskId: "task-1", workspaceId: "workspace-1", provider: "offline", executionId: "exec-1",
      inputDigest: hash("input"), policyId: "policy-1", policyVersion: "v1", createdAt: 1,
    },
    completionId: "completion-1", model: "synthetic", output: "private synthetic draft",
    outputDigest: hash("private synthetic draft"),
    sources: [{ sourceId: "source-1", path: "note.md", contentHash: hash("source"), excerptHash: hash("excerpt"), text: "synthetic excerpt not persisted" }],
  };
  const capture = async (executionId = "exec-1", workspaceId = "workspace-1") => {
    const receipt = await sink.capture({ ...completion, execution: { ...completion.execution, executionId, workspaceId } }, new AbortController().signal);
    return { ...receipt, path: join(runtime, "hosted-drafts", `${receipt.draftId}.json`), marker: join(runtime, "hosted-drafts", `${receipt.draftId}.review.json`) };
  };
  return { root, runtime, vault, source, options, sink, review, completion, capture };
}

describe("private hosted draft review integration", () => {
  it("lists missing draft directory without creating any file", async () => {
    const f = fixture();
    expect(await f.review.list("workspace-1")).toEqual({ drafts: [], unreadableCount: 0 });
    expect(readdirSync(f.runtime)).toEqual([]);
  });

  it("lists and reads only matching workspace; rejection preserves original and source", async () => {
    const f = fixture(); const draft = await f.capture(); await f.capture("other-exec", "workspace-2");
    const original = readFileSync(draft.path); const inode = statSync(draft.path).ino;
    const source = readFileSync(f.source);
    const result = await f.review.list("workspace-1");
    expect(result.unreadableCount).toBe(0); expect(result.drafts).toHaveLength(1);
    expect(result.drafts[0]).toEqual({
      draftId: draft.draftId, taskId: "task-1", workspaceId: "workspace-1", model: "synthetic",
      completionId: "completion-1", outputDigest: draft.outputDigest, status: "pending_review",
    });
    expect(result.drafts[0]).not.toHaveProperty("output");
    expect(await f.review.read("workspace-1", draft.draftId)).toMatchObject({ output: f.completion.output, sources: f.completion.sources.map(({ sourceId, path, contentHash, excerptHash }) => ({ sourceId, path, contentHash, excerptHash })) });
    await expect(f.review.read("workspace-2", draft.draftId)).rejects.toThrow();
    await expect(f.review.reject("workspace-2", draft.draftId, draft.outputDigest)).rejects.toThrow();
    await expect(f.review.reject("workspace-1", draft.draftId, hash("wrong"))).rejects.toThrow();
    await f.review.reject("workspace-1", draft.draftId, draft.outputDigest);
    const marker = readFileSync(draft.marker); const markerInode = statSync(draft.marker).ino;
    await new FileHostedDraftReview(f.options).reject("workspace-1", draft.draftId, draft.outputDigest);
    expect(readFileSync(draft.marker)).toEqual(marker); expect(statSync(draft.marker).ino).toBe(markerInode);
    expect(statSync(draft.marker).mode & 0o777).toBe(0o600);
    expect(JSON.parse(marker.toString())).toEqual({
      version: 1, status: "rejected", draftId: draft.draftId, taskId: "task-1", workspaceId: "workspace-1",
      executionId: "exec-1", outputDigest: draft.outputDigest,
    });
    expect((await f.review.read("workspace-1", draft.draftId)).status).toBe("rejected");
    expect((await f.review.list("workspace-1")).drafts[0].status).toBe("rejected");
    expect(readFileSync(draft.path)).toEqual(original); expect(statSync(draft.path).ino).toBe(inode);
    expect(readFileSync(f.source)).toEqual(source); expect(readdirSync(f.vault)).toEqual(["note.md"]);
  });

  it("makes concurrent equal rejections exclusive and idempotent", async () => {
    const f = fixture(); const draft = await f.capture();
    await Promise.all(Array.from({ length: 12 }, () => new FileHostedDraftReview(f.options).reject("workspace-1", draft.draftId, draft.outputDigest)));
    expect(readdirSync(join(f.runtime, "hosted-drafts")).sort()).toEqual([`${draft.draftId}.json`, `${draft.draftId}.review.json`].sort());
  });

  it.each(["output", "draftId", "executionId", "reservation", "sources", "duplicateSources", "extra"])("denies tampered %s and reports unreadable draft", async (field) => {
    const f = fixture(); const draft = await f.capture();
    const record = JSON.parse(readFileSync(draft.path, "utf8"));
    if (field === "output") record.output = "tampered";
    if (field === "draftId") record.draftId = `draft-${hash("wrong")}`;
    if (field === "executionId") { record.executionId = "other"; record.reservation.executionId = "other"; }
    if (field === "reservation") record.reservation.executionId = "other";
    if (field === "sources") record.sources[0].contentHash = "not-a-hash";
    if (field === "duplicateSources") record.sources.push(record.sources[0]);
    if (field === "extra") record.sourceText = "not permitted";
    writeFileSync(draft.path, JSON.stringify(record));
    const before = readFileSync(draft.path);
    await expect(f.review.read("workspace-1", draft.draftId)).rejects.toThrow("Hosted draft review operation rejected");
    await expect(f.review.reject("workspace-1", draft.draftId, draft.outputDigest)).rejects.toThrow();
    expect(await f.review.list("workspace-1")).toEqual({ drafts: [], unreadableCount: 1 });
    expect(readFileSync(draft.path)).toEqual(before);
  });

  it("rejects oversized files, symlinks, hardlinks and nonprivate files", async () => {
    const f = fixture(); const draft = await f.capture(); const original = readFileSync(draft.path);
    writeFileSync(draft.path, Buffer.alloc(524_289, 32));
    await expect(f.review.read("workspace-1", draft.draftId)).rejects.toThrow();
    expect((await f.review.list("workspace-1")).unreadableCount).toBe(1);
    writeFileSync(draft.path, original); chmodSync(draft.path, 0o644);
    await expect(f.review.read("workspace-1", draft.draftId)).rejects.toThrow();
    chmodSync(draft.path, 0o600);
    linkSync(draft.path, join(f.root, "hardlink"));
    await expect(f.review.read("workspace-1", draft.draftId)).rejects.toThrow();
    rmSync(draft.path); symlinkSync(f.source, draft.path);
    await expect(f.review.read("workspace-1", draft.draftId)).rejects.toThrow();
    await expect(f.review.reject("workspace-1", draft.draftId, draft.outputDigest)).rejects.toThrow();
    expect(readFileSync(f.source, "utf8")).toBe("original synthetic source");
  });

  it.each(["partial", "conflict", "extra", "symlink", "permissions"])("fails closed on %s review markers", async (kind) => {
    const f = fixture(); const draft = await f.capture();
    await f.review.reject("workspace-1", draft.draftId, draft.outputDigest);
    if (kind === "partial") writeFileSync(draft.marker, "{");
    if (kind === "conflict" || kind === "extra") {
      const marker = JSON.parse(readFileSync(draft.marker, "utf8"));
      if (kind === "conflict") marker.outputDigest = hash("other"); else marker.output = "forbidden";
      writeFileSync(draft.marker, JSON.stringify(marker));
    }
    if (kind === "symlink") { rmSync(draft.marker); symlinkSync(f.source, draft.marker); }
    if (kind === "permissions") chmodSync(draft.marker, 0o644);
    const original = readFileSync(draft.path); const marker = readFileSync(draft.marker);
    await expect(f.review.reject("workspace-1", draft.draftId, draft.outputDigest)).rejects.toThrow();
    await expect(f.review.read("workspace-1", draft.draftId)).rejects.toThrow();
    expect((await f.review.list("workspace-1")).unreadableCount).toBe(1);
    expect(readFileSync(draft.path)).toEqual(original); expect(readFileSync(draft.marker)).toEqual(marker);
  });

  it("bounds listing at fifty and inventory at 256; counts corrupt/orphan entries", async () => {
    const f = fixture();
    for (let i = 0; i < 51; i++) await f.capture(`exec-${i}`);
    const directory = join(f.runtime, "hosted-drafts");
    writeFileSync(join(directory, `draft-${hash("corrupt")}.json`), "{", { mode: 0o600 });
    writeFileSync(join(directory, `draft-${hash("orphan")}.review.json`), "{}", { mode: 0o600 });
    expect((await f.review.list("workspace-1")).drafts).toHaveLength(50);
    expect((await f.review.list("workspace-1")).unreadableCount).toBe(2);
    for (let i = 53; i < 257; i++) writeFileSync(join(directory, `junk-${i}`), "", { mode: 0o600 });
    await expect(f.review.list("workspace-1")).rejects.toThrow();
  });

  it("rejects path traversal, unsafe private stores, aliases and vault overlap", async () => {
    const f = fixture(); const draft = await f.capture();
    await expect(f.review.read("workspace-1", "../note.md")).rejects.toThrow();
    await expect(f.review.list("")).rejects.toThrow();
    for (const path of [f.runtime, join(f.runtime, "hosted-drafts")]) {
      chmodSync(path, 0o755); await expect(f.review.list("workspace-1")).rejects.toThrow(); chmodSync(path, 0o700);
    }
    const alias = join(f.root, "alias"); symlinkSync(f.runtime, alias);
    await expect(new FileHostedDraftReview({ ...f.options, privateRuntimeDir: alias }).list("workspace-1")).rejects.toThrow();
    for (const root of [f.root, f.runtime, join(f.runtime, "missing-vault")])
      await expect(new FileHostedDraftReview({ ...f.options, forbiddenVaultRoots: [root] }).list("workspace-1")).rejects.toThrow();
    expect((await f.review.read("workspace-1", draft.draftId)).status).toBe("pending_review");
  });

  it.each(["file", "directory"])("fails closed on %s fsync failure, retains marker and originals", async (kind) => {
    const f = fixture(); const draft = await f.capture(); const original = readFileSync(draft.path);
    const actual = await vi.importActual<typeof import("node:fs")>("node:fs");
    const events: string[] = [];
    vi.mocked(fsyncSync).mockImplementation(fd => {
      const type = fstatSync(fd).isDirectory() ? "directory" : "file";
      events.push(type);
      if (type === kind) throw Object.assign(new Error("synthetic fsync failure"), { code: "EINVAL" });
      actual.fsyncSync(fd);
    });
    await expect(f.review.reject("workspace-1", draft.draftId, draft.outputDigest)).rejects.toThrow();
    expect(events).toEqual(kind === "file" ? ["file"] : ["file", "directory"]);
    expect(readFileSync(draft.path)).toEqual(original);
    expect(JSON.parse(readFileSync(draft.marker, "utf8")).status).toBe("rejected");
    vi.mocked(fsyncSync).mockImplementation(actual.fsyncSync);
    await f.review.reject("workspace-1", draft.draftId, draft.outputDigest);
  });

  it("leaves a partial marker on write failure and never overwrites it", async () => {
    const f = fixture(); const draft = await f.capture(); const original = readFileSync(draft.path);
    const actual = await vi.importActual<typeof import("node:fs")>("node:fs");
    vi.mocked(writeFileSync).mockImplementationOnce(file => { actual.writeFileSync(file, "{"); throw new Error("synthetic partial write"); });
    await expect(f.review.reject("workspace-1", draft.draftId, draft.outputDigest)).rejects.toThrow();
    const inode = statSync(draft.marker).ino;
    await expect(f.review.reject("workspace-1", draft.draftId, draft.outputDigest)).rejects.toThrow();
    expect(readFileSync(draft.marker, "utf8")).toBe("{"); expect(statSync(draft.marker).ino).toBe(inode);
    expect(readFileSync(draft.path)).toEqual(original);
  });
});


describe("review store isolation", () => {
  it("rejects a symlinked draft directory and canonical forbidden-root aliases without writes", async () => {
    const f = fixture();
    symlinkSync(f.vault, join(f.runtime, "hosted-drafts"));
    await expect(f.review.list("workspace-1")).rejects.toThrow();
    expect(readdirSync(f.vault)).toEqual(["note.md"]);
    const g = fixture(); await g.capture();
    const alias = join(g.root, "runtime-alias"); symlinkSync(g.runtime, alias);
    const review = new FileHostedDraftReview({ ...g.options, forbiddenVaultRoots: [join(alias, "future-vault")] });
    await expect(review.list("workspace-1")).rejects.toThrow();
  });

  it("flushes marker then directory on both new and idempotent rejection", async () => {
    const f = fixture(); const draft = await f.capture();
    const actual = await vi.importActual<typeof import("node:fs")>("node:fs");
    const events: string[] = [];
    vi.mocked(fsyncSync).mockImplementation(fd => {
      events.push(fstatSync(fd).isDirectory() ? "directory" : "marker");
      actual.fsyncSync(fd);
    });
    await f.review.reject("workspace-1", draft.draftId, draft.outputDigest);
    await f.review.reject("workspace-1", draft.draftId, draft.outputDigest);
    expect(events).toEqual(["marker", "directory", "marker", "directory"]);
  });
});


describe("exact review bounds", () => {
  it("accepts exactly 524288 bytes and rejects a single excess byte", async () => {
    const f = fixture(); const draft = await f.capture();
    const text = readFileSync(draft.path, "utf8");
    const bounded = text + " ".repeat(524_288 - Buffer.byteLength(text));
    writeFileSync(draft.path, bounded);
    expect((await f.review.read("workspace-1", draft.draftId)).output).toBe(f.completion.output);
    writeFileSync(draft.path, bounded + " ");
    await expect(f.review.read("workspace-1", draft.draftId)).rejects.toThrow();
  });

  it("accepts exactly 256 inventory entries and reports all invalid entries", async () => {
    const f = fixture(); await f.capture();
    const directory = join(f.runtime, "hosted-drafts");
    for (let i = 0; i < 255; i++) writeFileSync(join(directory, `invalid-${i}`), "", { mode: 0o600 });
    const listing = await f.review.list("workspace-1");
    expect(listing.drafts).toHaveLength(1); expect(listing.unreadableCount).toBe(255);
  });
});
