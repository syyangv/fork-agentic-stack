import {chmodSync, linkSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {afterEach, describe, expect, it} from "vitest";
import {readHostedRuntimeConfig, HostedRuntimeConfigError} from "../server/hosted-runtime-config.js";
const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, {recursive: true, force: true}); });
const now = Date.parse("2026-11-15T12:00:00Z");
function fixture() {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "hosted-policy-")); roots.push(root);
  const policy = {policyId: "synthetic-policy", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "synthetic-model",
    vaultScope: "work", tools: "none", network: "opencode-provider-api", disclosure: "work-context-to-opencode-and-upstream",
    maxInputChars: 12_000, maxOutputChars: 1_000, maxTokens: 1_024, timeoutMs: 1_000, freshnessMs: 60_000};
  const config = {version: 1, enabled: true, mode: "synthetic-only", credentialSource: "environment", credentialReuseApproved: true,
    disclosureAccepted: "work-context-to-opencode-and-upstream", approvedAt: "2026-11-15T11:00:00Z", expiresAt: "2026-11-16T11:00:00Z",
    maxRequestsPerUtcDay: 1, allowedPromptHashes: ["b".repeat(64)], allowedWorkspaceIds: ["workspace_1"], allowedSources: [{path: "notes/synthetic.md", contentHash: "A".repeat(64)}], policy};
  const write = (value: unknown = config) => writeFileSync(join(root, "hosted-policy.json"), JSON.stringify(value), {mode: 0o600});
  return {root, config, write};
}
describe("private hosted operator policy", () => {
  it("defaults off for missing or explicitly disabled policy without writing files", () => {
    const f = fixture(); expect(readHostedRuntimeConfig(f.root, now)).toBeNull();
    f.write({version: 1, enabled: false}); expect(readHostedRuntimeConfig(f.root, now)).toBeNull();
  });
  it("ships an explicitly disabled example that cannot silently approve credential reuse", () => {
    const f = fixture(); const example = JSON.parse(readFileSync(new URL("../hosted-policy.example.json", import.meta.url), "utf8"));
    expect(example.enabled).toBe(false); expect(example.credentialReuseApproved).toBe(false);
    f.write(example); expect(readHostedRuntimeConfig(f.root, now)).toBeNull();
    f.write({...example, enabled: true}); expect(() => readHostedRuntimeConfig(f.root, now)).toThrow();
  });
  it("accepts explicit bounded approval and normalizes hash metadata", () => {
    const f = fixture(); f.write(); const loaded = readHostedRuntimeConfig(f.root, now)!;
    expect(loaded.config.allowedSources?.[0]?.contentHash).toBe("a".repeat(64)); expect(loaded.fingerprint).toMatch(/^[a-f0-9]{64}$/);
    expect(loaded.runtime).toBe(realpathSync(f.root));
  });
  it("rejects absent consent, unbounded scopes, missing synthetic fixtures and secret fields", () => {
    const f = fixture();
    for (const changed of [{credentialReuseApproved: false}, {disclosureAccepted: "other"}, {allowedWorkspaceIds: []},
      {allowedWorkspaceIds: ["workspace_1", "workspace_1"]}, {allowedSources: []}, {allowedPromptHashes: []}, {allowedPromptHashes: undefined}, {mode: "work", allowedSources: []}, {maxRequestsPerUtcDay: 0}, {maxRequestsPerUtcDay: 101},
      {apiKey: "THIS IS A FAKE DISALLOWED SECRET FIELD"}, {credentialSource: "arbitrary-command"},
      {policy: {...f.config.policy, tools: "shell"}}, {allowedSources: [{path: "../secret.md", contentHash: "a".repeat(64)}]}]) {
      f.write({...f.config, ...changed}); expect(() => readHostedRuntimeConfig(f.root, now)).toThrow(HostedRuntimeConfigError);
    }
  });
  it("requires explicit UTC expiry with a bounded approval lifetime", () => {
    const f = fixture();
    for (const changed of [{approvedAt: "2026-11-15T13:00:00Z"}, {expiresAt: "2026-11-15T11:00:00Z"},
      {expiresAt: "2027-01-01T00:00:00Z"}, {expiresAt: "2026-11-16 12:00:00"}]) {
      f.write({...f.config, ...changed}); expect(() => readHostedRuntimeConfig(f.root, now)).toThrow(HostedRuntimeConfigError);
    }
  });
  it("rejects nonprivate policy files and unsafe hardlinks/symlinks", () => {
    const f = fixture(); f.write(); chmodSync(join(f.root, "hosted-policy.json"), 0o644);
    expect(() => readHostedRuntimeConfig(f.root, now)).toThrow();
    chmodSync(join(f.root, "hosted-policy.json"), 0o600); linkSync(join(f.root, "hosted-policy.json"), join(f.root, "duplicate"));
    expect(() => readHostedRuntimeConfig(f.root, now)).toThrow();
    const other = fixture(); other.write(); symlinkSync(join(other.root, "hosted-policy.json"), join(f.root, "policy-link"));
    const linkRoot = fixture(); symlinkSync(join(other.root, "hosted-policy.json"), join(linkRoot.root, "hosted-policy.json"));
    expect(() => readHostedRuntimeConfig(linkRoot.root, now)).toThrow();
  });
  it("bounds file size and collection admission independently of parsing", () => {
    const f = fixture(); f.write({...f.config, allowedWorkspaceIds: Array(17).fill("workspace")});
    expect(() => readHostedRuntimeConfig(f.root, now)).toThrow();
    writeFileSync(join(f.root, "hosted-policy.json"), "x".repeat(16_385)); expect(() => readHostedRuntimeConfig(f.root, now)).toThrow();
  });
  it("canonicalizes only the trusted runtime alias and detects policy revision changes", () => {
    const f = fixture(); f.write(); const before = readHostedRuntimeConfig(f.root, now)!;
    const alias = join(f.root, "alias"); symlinkSync(f.root, alias);
    expect(readHostedRuntimeConfig(alias, now)?.runtime).toBe(f.root);
    f.write({...f.config, maxRequestsPerUtcDay: 2}); expect(readHostedRuntimeConfig(f.root, now)?.fingerprint).not.toBe(before.fingerprint);
  });
  it("does not treat formatting-only rewrites as a semantic approval revision", () => {
    const f = fixture(); f.write(); const before = readHostedRuntimeConfig(f.root, now)!;
    writeFileSync(join(f.root, "hosted-policy.json"), JSON.stringify(f.config, null, 2)+"\n");
    expect(readHostedRuntimeConfig(f.root, now)?.fingerprint).toBe(before.fingerprint);
  });
  it("rejects nonprivate runtime permissions", () => {
    const f = fixture(); f.write(); chmodSync(f.root, 0o755); expect(() => readHostedRuntimeConfig(f.root, now)).toThrow();
    chmodSync(f.root, 0o700);
  });
});
