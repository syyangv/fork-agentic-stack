import { describe, expect, it } from "vitest";
import type { HostedConsentPreview } from "../shared/hosted-contracts.js";
import { settleHostedSubmission, hostedOperationAllowed, hostedTaskIdentity, submittedHostedIdentity, hostedIdentity, reviewDisclosure, disclosureCurrent, setDisclosureConsent, hostedExecutionInput } from "../client/hosted-panel-state.js";
const identity = { workspaceId: "workspace", taskId: "task", prompt: "exact prompt", revision: "v1" };
function preview(overrides: Partial<HostedConsentPreview> = {}): HostedConsentPreview {
  return {
    ...identity, previewId: "server-preview", challenge: "server-challenge", payloadDigest: "a".repeat(64),
    provider: "opencode", expiresAt: 200,
    policy: { policyId: "policy", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "test", disclosure: "work-context-to-opencode-and-upstream" },
    sources: [{ sourceId: "source", path: "work.md", text: "exact excerpt", lineStart: 1, lineEnd: 2, contentHash: "b".repeat(64), excerptHash: "c".repeat(64) }],
    ...overrides,
  };
}
describe("hosted disclosure consent", () => {
  it("starts unchecked and requires an explicit choice", () => {
    const state = reviewDisclosure(preview(), identity, 100);
    expect(state?.checked).toBe(false);
    expect(hostedExecutionInput(state, identity, 100, false)).toBeNull();
    const consent = setDisclosureConsent(state, true, identity, 100);
    expect(hostedExecutionInput(consent, identity, 100, false)).not.toBeNull();
    expect(hostedExecutionInput(setDisclosureConsent(consent, false, identity, 100), identity, 100, false)).toBeNull();
  });
  it.each(["workspaceId", "taskId", "prompt", "revision"] as const)("invalidates consent after %s edits", field => {
    const state = setDisclosureConsent(reviewDisclosure(preview(), identity, 100), true, identity, 100);
    const edited = { ...identity, [field]: "changed" };
    expect(hostedIdentity(edited)).not.toBe(hostedIdentity(identity));
    expect(disclosureCurrent(state, edited, 100)).toBe(false);
    expect(hostedExecutionInput(state, edited, 100, false)).toBeNull();
    expect(setDisclosureConsent(state, true, edited, 100)?.checked).toBe(false);
  });
  it("resets consent for every new preview, even if the payload is unchanged", () => {
    const first = setDisclosureConsent(reviewDisclosure(preview(), identity, 100), true, identity, 100);
    expect(first?.checked).toBe(true);
    const second = reviewDisclosure(preview({ previewId: "new-preview" }), identity, 101);
    expect(second?.checked).toBe(false);
    expect(reviewDisclosure(first!.preview, identity, 101)?.checked).toBe(false);
    expect(hostedExecutionInput(second, identity, 101, false)).toBeNull();
  });
  it("expires at the exact boundary and cannot regain consent after expiry", () => {
    const state = setDisclosureConsent(reviewDisclosure(preview(), identity, 100), true, identity, 100);
    expect(hostedExecutionInput(state, identity, 199, false)).not.toBeNull();
    expect(hostedExecutionInput(state, identity, 200, false)).toBeNull();
    expect(setDisclosureConsent(state, true, identity, 201)?.checked).toBe(false);
    expect(reviewDisclosure(preview(), identity, 200)).toBeNull();
  });
  it("refuses stale or mismatched server disclosures", () => {
    for (const field of ["workspaceId", "taskId", "prompt"] as const) {
      expect(reviewDisclosure(preview({ [field]: "other" }), identity, 100)).toBeNull();
    }
  });
  it("blocks execution while busy and sends only the server-bound approval fields", () => {
    const state = setDisclosureConsent(reviewDisclosure(preview(), identity, 100), true, identity, 100);
    expect(hostedExecutionInput(state, identity, 100, true)).toBeNull();
    expect(hostedExecutionInput(state, identity, 100, false)).toEqual({
      previewId: "server-preview", challenge: "server-challenge", payloadDigest: "a".repeat(64), taskId: "task", workspaceId: "workspace",
    });
  });
});

describe("submitted hosted cancellation identity", () => {
  it("keeps cancellation bound to the approved task and workspace after parent edits", () => {
    const mutableInput = { ...identity, previewId: "approved-preview" };
    const submission = submittedHostedIdentity(mutableInput);
    mutableInput.taskId = "edited-task";
    mutableInput.workspaceId = "other-workspace";
    mutableInput.prompt = "edited prompt";
    expect(submission).toEqual({ workspaceId: "workspace", taskId: "task" });
    expect(Object.isFrozen(submission)).toBe(true);
    expect(submittedHostedIdentity(mutableInput)).toEqual({ workspaceId: "other-workspace", taskId: "edited-task" });
  });
  it("uses a distinct submission token for a later execution of the same task", () => {
    expect(submittedHostedIdentity(identity)).not.toBe(submittedHostedIdentity(identity));
  });
});

describe("hosted execution transport recovery", () => {
  it("retains the exact cancellation token and original identity when transport rejects", () => {
    const captured = submittedHostedIdentity(identity);
    const unknown = settleHostedSubmission({ identity: captured, phase: "running" }, captured, "unknown");
    expect(unknown?.phase).toBe("unknown");
    expect(unknown?.identity).toBe(captured);
    expect(unknown?.identity).toEqual({ taskId: "task", workspaceId: "workspace" });
  });
  it("only a settled response releases the matching submission", () => {
    const captured = submittedHostedIdentity(identity);
    expect(settleHostedSubmission({ identity: captured, phase: "running" }, captured, "settled")).toBeNull();
  });
  it("ignores stale callbacks even when a newer submission has the same task identity", () => {
    const old = submittedHostedIdentity(identity);
    const current = { identity: submittedHostedIdentity(identity), phase: "running" as const };
    expect(settleHostedSubmission(current, old, "settled")).toBe(current);
    expect(settleHostedSubmission(current, old, "unknown")).toBe(current);
  });
  it("blocks every ordinary action while running, but permits draft recovery when unknown", () => {
    const captured = submittedHostedIdentity(identity);
    const acknowledged = new Set<string>();
    const running = { identity: captured, phase: "running" as const };
    const unknown = settleHostedSubmission(running, captured, "unknown");
    for (const kind of ["draft", "prepare", "execute"] as const) {
      expect(hostedOperationAllowed(running, kind, identity, acknowledged)).toBe(false);
      expect(hostedOperationAllowed(unknown, kind, identity, acknowledged)).toBe(kind === "draft");
    }
    const edited = { workspaceId: "other-workspace", taskId: "edited-task" };
    expect(hostedOperationAllowed(unknown, "execute", edited, acknowledged)).toBe(false);
    expect(hostedOperationAllowed(unknown, "draft", edited, acknowledged)).toBe(true);
  });
  it("acknowledgement never authorizes retrying the same task, even with edited prompt/revision", () => {
    const acknowledged = new Set([hostedTaskIdentity(identity)]);
    const edited = { ...identity, prompt: "new prompt", revision: "v2" };
    expect(hostedOperationAllowed(null, "draft", edited, acknowledged)).toBe(true);
    expect(hostedOperationAllowed(null, "prepare", edited, acknowledged)).toBe(false);
    expect(hostedOperationAllowed(null, "execute", edited, acknowledged)).toBe(false);
    expect(hostedOperationAllowed(null, "execute", { ...edited, taskId: "new-task" }, acknowledged)).toBe(true);
  });
});
