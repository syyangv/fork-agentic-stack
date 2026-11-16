import {chmodSync, mkdtempSync, readdirSync, realpathSync, rmSync, statSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {dirname, join} from "node:path";
import {open} from "node:fs/promises";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {HostedRequestBudget} from "../server/hosted-request-budget.js";
const faults = vi.hoisted(() => ({kind: "none", directory: "", syncs: [] as string[]}));
vi.mock("node:fs/promises", async importOriginal => {
  const actual = await importOriginal<typeof import("node:fs/promises")>();
  return {...actual, open: vi.fn(async (...args: Parameters<typeof actual.open>) => {
    const handle = await actual.open(...args);
    const name = String(args[0]), file = args[1] === "wx";
    return new Proxy(handle, {get(target, key) {
      if (key === "sync") return async () => {
        faults.syncs.push(file ? "file" : name);
        if ((faults.kind === "file" && file) || (faults.kind === "directory" && name === faults.directory)
          || (faults.kind === "parent" && name === dirname(faults.directory))) {
          throw Object.assign(new Error("synthetic budget flush failure"), {code: "ENOTSUP"});
        }
        return target.sync();
      };
      const value = Reflect.get(target, key, target);
      return typeof value === "function" ? value.bind(target) : value;
    }});
  })};
});
beforeEach(() => { faults.kind = "none"; faults.directory = ""; faults.syncs = []; vi.clearAllMocks(); });
const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, {recursive: true, force: true}); });
function root() { const path = mkdtempSync(join(realpathSync(tmpdir()), "hosted-budget-")); roots.push(path); return path; }
describe("durable UTC provider attempt budget", () => {
  it("atomically caps concurrent callers and preserves spent slots across instances", async () => {
    const directory = root(); const a = new HostedRequestBudget(directory, 2), b = new HostedRequestBudget(directory, 2);
    const results = await Promise.all([a.take(), b.take(), a.take(), b.take()]);
    expect(results.filter(Boolean)).toHaveLength(2); expect(await new HostedRequestBudget(directory, 2).take()).toBe(false);
    for (const file of readdirSync(directory)) expect(statSync(join(directory, file)).mode & 0o777).toBe(0o600);
  });
  it("rolls over by UTC, not the Mac or New York local date", async () => {
    let time = Date.parse("2026-11-15T23:59:59.999Z"); const directory = root(); const budget = new HostedRequestBudget(directory, 1, () => time);
    expect(await budget.take()).toBe(true); expect(await budget.take()).toBe(false); time++;
    expect(await budget.take()).toBe(true); expect(readdirSync(directory).sort()).toEqual(["2026-11-15-0.json", "2026-11-16-0.json"]);
  });
  it("treats corrupt or partial markers as spent rather than reclaiming them", async () => {
    const directory = root(); writeFileSync(join(directory, "2026-11-15-0.json"), "{partial", {mode: 0o600});
    expect(await new HostedRequestBudget(directory, 1, () => Date.parse("2026-11-15T12:00:00Z")).take()).toBe(false);
  });
  it("fails closed on nonprivate storage and invalid limits", async () => {
    const directory = root(); chmodSync(directory, 0o755); await expect(new HostedRequestBudget(directory, 1).take()).rejects.toThrow();
    chmodSync(directory, 0o700);
    for (const limit of [0,101,NaN,Infinity,1.1]) expect(() => new HostedRequestBudget(directory, limit)).toThrow();
  });
  it("acknowledges only after flushing file and all canonical ancestor entries", async () => {
    const directory = root();
    expect(await new HostedRequestBudget(directory, 1).take()).toBe(true);
    const expected = ["file"]; let current = realpathSync(directory);
    for (;;) { expected.push(current); const parent = dirname(current); if (parent === current) break; current = parent; }
    expect(faults.syncs).toEqual(expected); expect(open).toHaveBeenCalled();
  });
  it.each(["file", "directory", "parent"])("fails closed on unsupported %s flush and never reclaims its marker", async kind => {
    const directory = root(); faults.directory = directory; faults.kind = kind;
    const budget = new HostedRequestBudget(directory, 1);
    await expect(budget.take()).rejects.toThrow("synthetic budget flush failure");
    expect(readdirSync(directory)).toHaveLength(1); faults.kind = "none";
    expect(await budget.take()).toBe(false);
  });

});
