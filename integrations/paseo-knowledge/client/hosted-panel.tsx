import { type PluginWorkspacePanelProps, useRpc } from "@getpaseo/plugin/client";
import { useEffect, useRef, useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";
import {
  HostedAvailabilityRpc, HostedPreviewRpc, HostedExecuteRpc, HostedCancelRpc,
  HostedDraftListRpc, HostedDraftReadRpc, HostedDraftRejectRpc,
  type HostedDraftDetails, type HostedDraftSummary,
} from "../shared/hosted-contracts.js";
import {
  hostedIdentity, reviewDisclosure, disclosureCurrent, setDisclosureConsent, hostedExecutionInput,
  submittedHostedIdentity, settleHostedSubmission, hostedOperationAllowed, hostedTaskIdentity,
  type HostedSubmissionState, type HostedSubmittedIdentity, type HostedPanelIdentity, type HostedConsentState,
} from "./hosted-panel-state.js";

type Props = HostedPanelIdentity & { allowPrepare?: boolean; theme: PluginWorkspacePanelProps["theme"] };

export function HostedKnowledgePanel({ allowPrepare = true, ...props }: Props) {
  const key = JSON.stringify([hostedIdentity(props), allowPrepare]);
  const scope = useRef({ key });
  // Replace the token even for A -> B -> A edits. Old asynchronous results stay stale.
  if (scope.current.key !== key) scope.current = { key };
  const token = scope.current;
  const cancelRpc = useRpc(HostedCancelRpc);
  const submitted = useRef<HostedSubmissionState | null>(null);
  const [submission, setSubmission] = useState<HostedSubmissionState | null>(null);
  const acknowledgedUnknown = useRef(new Set<string>());
  const [recoveryNotice, setRecoveryNotice] = useState("");
  const cancelLocked = useRef(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelMessage, setCancelMessage] = useState("");
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const canOperate = (kind: "draft" | "prepare" | "execute") => !cancelLocked.current &&
    hostedOperationAllowed(submitted.current, kind, props, acknowledgedUnknown.current);
  function beginExecution(input: HostedSubmittedIdentity) {
    if (!alive.current || !canOperate("execute")) return null;
    const captured = submittedHostedIdentity(input);
    const next: HostedSubmissionState = { identity: captured, phase: "running" };
    submitted.current = next;
    setSubmission(next);
    setRecoveryNotice("");
    setCancelMessage("");
    return captured;
  }
  function endExecution(captured: HostedSubmittedIdentity, outcome: "settled" | "unknown") {
    const next = settleHostedSubmission(submitted.current, captured, outcome);
    if (next === submitted.current) return;
    submitted.current = next;
    if (alive.current) setSubmission(next);
  }
  const text = { color: props.theme.colors.foreground, fontSize: 13 };
  return <View style={{ gap: 8 }}>
    <HostedSession key={key} {...props} allowPrepare={allowPrepare} current={() => scope.current === token}
      externalBusy={submission?.phase === "running" || cancelling} executionBlocked={!canOperate("execute")} canOperate={canOperate}
      beginExecution={beginExecution} endExecution={endExecution} />
    {submission ? <View style={{ padding: 12, gap: 6 }}>
      <Text style={text}>Submitted task: {submission.identity.taskId} — workspace: {submission.identity.workspaceId}</Text>
      {submission.phase === "unknown" ? <Text style={text} accessibilityLiveRegion="polite">Remote outcome unknown: execution transport failed. Cancel remains available for the original submission. Refresh and read private drafts explicitly; do not retry this task.</Text> : null}
      <Text style={text}>Cancellation after submission may leave the remote outcome unknown. It does not guarantee remote execution stopped.</Text>
      <Pressable accessibilityRole="button" accessibilityLabel="Cancel hosted task"
        accessibilityState={{ disabled: cancelling }} disabled={cancelling} style={{ padding: 10, opacity: cancelling ? 0.45 : 1 }}
        onPress={() => {
          const captured = submitted.current?.identity;
          if (!alive.current || !captured || cancelLocked.current) return;
          // This independent call uses only the immutable approved submission, not props.
          cancelLocked.current = true;
          setCancelling(true);
          setCancelMessage("");
          void cancelRpc({ workspaceId: captured.workspaceId, taskId: captured.taskId }).then(response => {
            if (!alive.current || submitted.current?.identity !== captured) return;
            setCancelMessage(response.error ? response.error.code : response.cancelled
              ? "Cancellation requested; remote outcome may remain unknown."
              : "No active task cancelled; remote outcome may remain unknown.");
          }).catch(() => {
            if (alive.current && submitted.current?.identity === captured) setCancelMessage("hosted_cancel_rpc_failed — no automatic retry; remote outcome may remain unknown.");
          }).finally(() => {
            cancelLocked.current = false;
            if (alive.current) setCancelling(false);
          });
        }}><Text style={text}>Cancel hosted task</Text></Pressable>
      {cancelMessage ? <Text style={text} accessibilityLiveRegion="polite">{cancelMessage}</Text> : null}
      {submission.phase === "unknown" ? <View>
        <Text style={text}>Acknowledgement dismisses this cancellation control, not the remote task. The remote outcome remains unknown. Do not retry the same task; inspect private drafts before using a new task identity.</Text>
        <Pressable accessibilityRole="button" accessibilityLabel="Acknowledge unknown outcome and dismiss cancellation"
          accessibilityState={{ disabled: cancelling }} disabled={cancelling} style={{ padding: 10 }} onPress={() => {
            const pending = submitted.current;
            if (!alive.current || pending?.phase !== "unknown" || cancelLocked.current) return;
            acknowledgedUnknown.current.add(hostedTaskIdentity(pending.identity));
            setRecoveryNotice(`Unknown outcome acknowledged for task ${pending.identity.taskId} in workspace ${pending.identity.workspaceId}. Do not retry the same task.`);
            submitted.current = null;
            setSubmission(null);
          }}><Text style={text}>Acknowledge unknown outcome and dismiss cancellation</Text></Pressable>
      </View> : null}
    </View> : null}
    {recoveryNotice ? <Text style={text} accessibilityLiveRegion="polite">{recoveryNotice}</Text> : null}
  </View>;
}

type SessionProps = Props & {
  current: () => boolean;
  externalBusy: boolean;
  executionBlocked: boolean;
  canOperate: (kind: "draft" | "prepare" | "execute") => boolean;
  beginExecution: (input: HostedSubmittedIdentity) => HostedSubmittedIdentity | null;
  endExecution: (input: HostedSubmittedIdentity, outcome: "settled" | "unknown") => void;
};
function HostedSession({ current, externalBusy, executionBlocked, canOperate, beginExecution, endExecution, allowPrepare = true, ...props }: SessionProps) {
  const availabilityRpc = useRpc(HostedAvailabilityRpc);
  const previewRpc = useRpc(HostedPreviewRpc);
  const executeRpc = useRpc(HostedExecuteRpc);
  const listRpc = useRpc(HostedDraftListRpc);
  const readRpc = useRpc(HostedDraftReadRpc);
  const rejectRpc = useRpc(HostedDraftRejectRpc);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const actionBusy = busy || externalBusy;
  const alive = useRef(true);
  const [disclosure, setDisclosure] = useState<HostedConsentState | null>(null);
  const [, tick] = useState(0);
  const [message, setMessage] = useState("");
  const [drafts, setDrafts] = useState<HostedDraftSummary[] | null>(null);
  const [draft, setDraft] = useState<HostedDraftDetails | null>(null);
  const [confirmReject, setConfirmReject] = useState(false);
  const active = () => alive.current && current();

  useEffect(() => {
    alive.current = true;
    let disposed = false;
    // Availability is the only automatic RPC. No preview, execution, or draft reads.
    void availabilityRpc({ workspaceId: props.workspaceId }).then(value => {
      if (!disposed && active()) setAvailable(value.available && value.error === null);
    }).catch(() => { if (!disposed && active()) setAvailable(false); });
    return () => { disposed = true; alive.current = false; };
    // A keyed session fixes the input identity for its entire lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const preview = disclosure?.preview;
  useEffect(() => {
    if (!preview) return;
    const timer = setTimeout(() => {
      if (!active()) return;
      setDisclosure(value => value?.preview === preview ? { ...value, checked: false } : value);
      tick(value => value + 1);
    }, Math.max(0, Math.min(preview.expiresAt - Date.now(), 2_147_483_647)));
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preview]);

  async function request<T>(operation: () => Promise<T>, apply: (response: T) => void, execution?: HostedSubmittedIdentity, kind: "draft" | "prepare" = "draft") {
    if (!active() || locked.current || !canOperate(execution ? "execute" : kind) || available !== true || (execution && !allowPrepare)) return;
    const captured = execution ? beginExecution(execution) : null;
    if (execution && !captured) return;
    locked.current = true;
    setBusy(true);
    setMessage("");
    let outcome: "settled" | "unknown" = "unknown";
    try {
      const response = await operation();
      outcome = "settled";
      if (active()) apply(response);
    } catch {
      outcome = "unknown";
      if (active()) setMessage(captured ? "Remote outcome unknown — execution transport failed; no automatic retry." : "hosted_rpc_failed — no automatic retry");
    } finally {
      locked.current = false;
      // Transport rejection preserves the original cancellation identity across prop edits.
      if (captured) endExecution(captured, outcome);
      if (active()) setBusy(false);
    }
  }

  const colors = props.theme.colors;
  const text = { color: colors.foreground, fontSize: 13 };
  const muted = { color: colors.foregroundMuted, fontSize: 12 };
  const card = { backgroundColor: colors.surface1, padding: 10, gap: 6 };
  function button(label: string, onPress: () => void, disabled = actionBusy) {
    return <Pressable accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ disabled }}
      disabled={disabled} onPress={onPress} style={{ padding: 10, opacity: disabled ? 0.45 : 1 }}>
      <Text style={text}>{label}</Text>
    </Pressable>;
  }
  if (available !== true) return <Text style={muted} accessibilityLabel="Hosted knowledge availability">
    {available === null ? "Checking hosted availability…" : "Hosted knowledge unavailable"}
  </Text>;

  const valid = disclosureCurrent(disclosure, props, Date.now());
  const execution = allowPrepare && !executionBlocked ? hostedExecutionInput(disclosure, props, Date.now(), actionBusy) : null;
  return <ScrollView contentContainerStyle={{ padding: 12, gap: 10, backgroundColor: colors.surface0 }}>
    <Text style={text} accessibilityRole="header">Hosted knowledge — private drafts only</Text>
    <Text style={muted}>Prepare work context with the configured opencode-go provider and model first. The model must match the configured policy. Hosted execution is disabled by default on the server.</Text>
    {!allowPrepare ? <Text style={muted}>Hosted preparation and execution are available only in a work workspace panel, not personal scope or an agent panel. Any already submitted task remains independently cancellable.</Text> : null}
    {button("Review hosted disclosure", () => {
      if (!allowPrepare || !active()) return;
      setDisclosure(null);
      setConfirmReject(false);
      void request(() => previewRpc({ taskId: props.taskId, workspaceId: props.workspaceId, prompt: props.prompt }), response => {
        if (response.error || !response.preview) {
          setMessage(response.error?.code ?? "hosted_preview_missing");
          return;
        }
        const next = reviewDisclosure(response.preview, props, Date.now());
        setDisclosure(next);
        if (!next) setMessage("hosted_preview_stale_or_expired");
      }, undefined, "prepare");
    }, actionBusy || !allowPrepare || executionBlocked)}
    {preview ? <View style={card}>
      <Text style={text}>Data below leaves this Mac and is sent to OpenCode and its upstream model provider.</Text>
      <Text style={text}>Endpoint: {preview.policy.endpoint}</Text>
      <Text style={text}>Model: {preview.policy.model}</Text>
      <Text style={muted}>Policy: {preview.policy.policyId} / {preview.policy.version}</Text>
      <Text style={muted}>Payload digest: {preview.payloadDigest}</Text>
      <Text style={muted}>Expires: {new Date(preview.expiresAt).toISOString()}</Text>
      <Text style={text}>Exact prompt</Text>
      <Text selectable style={text}>{preview.prompt}</Text>
      <Text style={text}>All disclosed source excerpts (untrusted text)</Text>
      {preview.sources.map((source, index) => <View key={`${index}:${source.sourceId}`} style={card}>
        <Text selectable style={text}>{source.sourceId}: {source.path}</Text>
        <Text style={muted}>Lines {source.lineStart}–{source.lineEnd}</Text>
        <Text selectable style={muted}>Original file SHA-256: {source.contentHash}</Text>
        <Text selectable style={muted}>Exact excerpt SHA-256: {source.excerptHash}</Text>
        <Text selectable style={text}>{source.text}</Text>
      </View>)}
      {!valid ? <Text style={muted}>Disclosure expired. Review a new disclosure before approving.</Text> : null}
      <Pressable accessibilityRole="checkbox" accessibilityLabel="Consent to send the exact disclosed data to OpenCode and upstream"
        accessibilityState={{ checked: disclosure?.checked === true, disabled: actionBusy || executionBlocked || !valid }} disabled={actionBusy || executionBlocked || !valid}
        onPress={() => setDisclosure(value => setDisclosureConsent(value, !value?.checked, props, Date.now()))}
        style={{ padding: 10 }}>
        <Text style={text}>{disclosure?.checked ? "[x]" : "[ ]"} I consent to sending this exact prompt and all excerpts to OpenCode and upstream.</Text>
      </Pressable>
      {button("Approve and run hosted task", () => {
        const input = allowPrepare && !executionBlocked ? hostedExecutionInput(disclosure, props, Date.now(), locked.current) : null;
        if (!input || !active()) return;
        // Consume consent immediately; an ambiguous failure never triggers a retry.
        setDisclosure(null);
        void request(() => executeRpc(input), response => {
          if (response.error) { setMessage(response.error.code); return; }
          const result = response.result;
          if (!result) { setMessage("hosted_result_missing"); return; }
          setMessage(result.status === "completed"
            ? `completed — draftId: ${result.draftId} — executionId: ${result.executionId}`
            : `${result.status} — ${result.error.code}`);
        }, input);
      }, !execution)}
    </View> : null}
    {message ? <Text selectable style={text} accessibilityLiveRegion="polite">{message}</Text> : null}
    <Text style={text} accessibilityRole="header">Private draft review</Text>
    {button("Refresh draft list", () => {
      setConfirmReject(false);
      setDraft(null);
      void request(() => listRpc({ workspaceId: props.workspaceId }), response => {
        if (response.error) { setMessage(response.error.code); return; }
        setDrafts(response.drafts);
        setMessage(`Unreadable drafts: ${response.unreadableCount}`);
      });
    })}
    {drafts?.length === 0 ? <Text style={muted}>No private drafts found.</Text> : null}
    {drafts?.map(summary => <View key={summary.draftId} style={card}>
      <Text selectable style={text}>{summary.draftId} — {summary.status} — task: {summary.taskId}</Text>
      {button(`Read draft ${summary.draftId}`, () => {
        setDraft(null);
        setConfirmReject(false);
        void request(() => readRpc({ workspaceId: props.workspaceId, draftId: summary.draftId }), response => {
          if (response.error) { setMessage(response.error.code); return; }
          if (!response.draft || response.draft.workspaceId !== props.workspaceId || response.draft.draftId !== summary.draftId) {
            setMessage("hosted_draft_missing_or_mismatched"); return;
          }
          setDraft(response.draft);
        });
      })}
    </View>)}
    {draft ? <View style={card}>
      <Text selectable style={text}>Draft: {draft.draftId} — {draft.status}</Text>
      <Text style={text}>Untrusted model output — plain text only. Nothing is applied.</Text>
      <Text selectable style={text}>{draft.output}</Text>
      <Text selectable style={muted}>Output SHA-256: {draft.outputDigest}</Text>
      {draft.sources.map((source, index) => <Text selectable key={`${index}:${source.sourceId}`} style={muted}>
        {source.sourceId}: {source.path}{"\n"}File: {source.contentHash}{"\n"}Excerpt: {source.excerptHash}
      </Text>)}
      {draft.status === "pending_review" && !confirmReject ? button("Reject this draft", () => setConfirmReject(true)) : null}
      {confirmReject ? <View>
        <Text style={text}>Confirm rejection of draft {draft.draftId}? No output will be applied.</Text>
        {button("Confirm reject draft", () => {
          const input = { workspaceId: props.workspaceId, draftId: draft.draftId, outputDigest: draft.outputDigest };
          setConfirmReject(false);
          void request(() => rejectRpc(input), response => {
            if (response.error || !response.rejected) { setMessage(response.error?.code ?? "hosted_reject_failed"); return; }
            setDraft(value => value?.draftId === input.draftId ? { ...value, status: "rejected" } : value);
            setDrafts(values => values?.map(value => value.draftId === input.draftId ? { ...value, status: "rejected" } : value) ?? null);
            setMessage(`rejected — draftId: ${input.draftId}`);
          });
        })}
        {button("Cancel draft rejection", () => setConfirmReject(false))}
      </View> : null}
    </View> : null}
  </ScrollView>;
}
