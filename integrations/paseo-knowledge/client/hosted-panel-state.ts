import type { HostedConsentPreview } from "../shared/hosted-contracts.js";

export interface HostedPanelIdentity {
  workspaceId: string;
  taskId: string;
  prompt: string;
  revision?: string;
}
export interface HostedConsentState {
  readonly identity: string;
  readonly preview: HostedConsentPreview;
  readonly checked: boolean;
}
export const hostedIdentity = (input: HostedPanelIdentity): string =>
  JSON.stringify([input.workspaceId, input.taskId, input.prompt, input.revision ?? null]);

export function reviewDisclosure(preview: HostedConsentPreview, input: HostedPanelIdentity, now: number): HostedConsentState | null {
  if (preview.workspaceId !== input.workspaceId || preview.taskId !== input.taskId || preview.prompt !== input.prompt || preview.expiresAt <= now) return null;
  return { identity: hostedIdentity(input), preview, checked: false };
}
export function disclosureCurrent(state: HostedConsentState | null, input: HostedPanelIdentity, now: number): state is HostedConsentState {
  return state !== null && state.identity === hostedIdentity(input) &&
    state.preview.workspaceId === input.workspaceId && state.preview.taskId === input.taskId &&
    state.preview.prompt === input.prompt && state.preview.expiresAt > now;
}
export function setDisclosureConsent(state: HostedConsentState | null, checked: boolean, input: HostedPanelIdentity, now: number): HostedConsentState | null {
  return state && { ...state, checked: checked && disclosureCurrent(state, input, now) };
}
export function hostedExecutionInput(state: HostedConsentState | null, input: HostedPanelIdentity, now: number, busy: boolean) {
  if (busy || !disclosureCurrent(state, input, now) || !state.checked) return null;
  const { previewId, challenge, payloadDigest, taskId, workspaceId } = state.preview;
  return { previewId, challenge, payloadDigest, taskId, workspaceId };
}

export type HostedSubmittedIdentity = Readonly<Pick<HostedPanelIdentity, "taskId" | "workspaceId">>;
// Copy the approved identity; cancellation must never read edited panel props.
export function submittedHostedIdentity(input: HostedSubmittedIdentity): HostedSubmittedIdentity {
  return Object.freeze({ workspaceId: input.workspaceId, taskId: input.taskId });
}

export interface HostedSubmissionState {
  readonly identity: HostedSubmittedIdentity;
  readonly phase: "running" | "unknown";
}
export const hostedTaskIdentity = (input: HostedSubmittedIdentity): string =>
  JSON.stringify([input.workspaceId, input.taskId]);
export function settleHostedSubmission(state: HostedSubmissionState | null, captured: HostedSubmittedIdentity,
  outcome: "settled" | "unknown"): HostedSubmissionState | null {
  if (state?.identity !== captured) return state;
  return outcome === "unknown" ? { identity: captured, phase: "unknown" } : null;
}
export function hostedOperationAllowed(state: HostedSubmissionState | null, kind: "draft" | "prepare" | "execute",
  input: HostedSubmittedIdentity, acknowledgedUnknown: ReadonlySet<string>): boolean {
  if (state?.phase === "running") return false;
  if (kind === "draft") return true;
  return state === null && !acknowledgedUnknown.has(hostedTaskIdentity(input));
}
