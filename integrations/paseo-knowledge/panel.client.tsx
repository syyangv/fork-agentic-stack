import {
  type PluginAgentPanelProps,
  type PluginWorkspacePanelProps,
  useAgent,
  useRpc,
  useWorkspace,
} from "@getpaseo/plugin";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Pressable, ScrollView, Text, TextInput, View } from "react-native";
import {
  GrantPersonalSourceRpc,
  PluginStatusRpc,
  PrepareKnowledgeTaskRpc,
  ReconcileKnowledgeTaskRpc,
  RemoveKnowledgeSourceRpc,
  RevokePersonalSourceRpc,
  SearchKnowledgeRpc,
  StartKnowledgeTaskRpc,
  WaitKnowledgeTaskRpc,
  type GrantPersonalSourceResponse,
  type PluginStatusResponse,
  type PrepareKnowledgeTaskResponse,
  type ReconcileKnowledgeTaskResponse,
  type RemoveKnowledgeSourceResponse,
  type RevokePersonalSourceResponse,
  type SearchKnowledgeResponse,
  type StartKnowledgeTaskResponse,
  type WaitKnowledgeTaskResponse,
  type VaultScope,
} from "./contracts.shared.js";

type WorkspaceProps = PluginWorkspacePanelProps;
type AgentProps = PluginAgentPanelProps;

let taskSequence = 0;

export function KnowledgeWorkspacePanel(props: WorkspaceProps) {
  return <KnowledgeTaskPanel workspaceId={props.workspaceId} theme={props.theme} layout={props.layout} />;
}

export function KnowledgeAgentPanel(props: AgentProps) {
  return <KnowledgeTaskPanel workspaceId={props.workspaceId} agentId={props.agentId} theme={props.theme} layout={props.layout} />;
}

function KnowledgeTaskPanel({
  workspaceId,
  agentId,
  theme,
  layout,
}: {
  workspaceId: string;
  agentId?: string;
  theme: WorkspaceProps["theme"];
  layout: WorkspaceProps["layout"];
}) {
  const workspace = useWorkspace(workspaceId, ({ name, status }) => ({ name, status }));
  const agent = useAgent(agentId ?? "", ({ id, status, model, labels, requiresAttention }) => ({ id, status, model, labels, requiresAttention }));
  const callStatus = useRpc(PluginStatusRpc);
  const callSearch = useRpc(SearchKnowledgeRpc);
  const callPrepare = useRpc(PrepareKnowledgeTaskRpc);
  const callStart = useRpc(StartKnowledgeTaskRpc);
  const callReconcile = useRpc(ReconcileKnowledgeTaskRpc);
  const callWait = useRpc(WaitKnowledgeTaskRpc);
  const callRemoveSource = useRpc(RemoveKnowledgeSourceRpc);
  const callGrant = useRpc(GrantPersonalSourceRpc);
  const callRevoke = useRpc(RevokePersonalSourceRpc);

  const [taskId, setTaskId] = useState(() => `ktask_${Date.now()}_${taskSequence++}`);
  const [provider, setProvider] = useState("");
  const [query, setQuery] = useState("");
  const [prompt, setPrompt] = useState("");
  const [scope, setScope] = useState<VaultScope>("work");
  const [project, setProject] = useState("");
  const [grantPath, setGrantPath] = useState("");
  const [response, setResponse] = useState<PanelResponse | null>(null);
  const [searchResults, setSearchResults] = useState<ReadonlyArray<Record<string, unknown>>>([]);
  const [busy, setBusy] = useState(false);

  const agentTaskId = agent?.labels.knowledge_task_id;
  useEffect(() => {
    if (agentTaskId) setTaskId(agentTaskId);
  }, [agentTaskId]);

  const refresh = useCallback(async () => {
    const status = await callStatus({ taskId, agentId, workspaceId });
    setResponse({ kind: "status", value: status });
  }, [agentId, callStatus, taskId, workspaceId]);

  useEffect(() => {
    void refresh().catch(() => undefined);
    if (!agentId) return;
    const timer = setInterval(() => { void refresh().catch(() => undefined); }, 2_000);
    return () => clearInterval(timer);
  }, [agentId, refresh]);

  const run = useCallback(async (operation: () => Promise<PanelResponse>) => {
    setBusy(true);
    try {
      setResponse(await operation());
    } catch (error) {
      setResponse({ kind: "local-error", value: error instanceof Error ? error.message : "Paseo RPC failed." });
    } finally {
      setBusy(false);
    }
  }, []);

  const styles = useMemo(() => makeStyles(theme, layout.compact), [layout.compact, theme]);
  const sourceManifest = response?.kind === "prepare" ? response.value.task.sourceManifest : response?.kind === "status" ? response.value.task?.sourceManifest ?? [] : [];
  const context = response?.kind === "prepare" ? response.value.context : null;
  const contextText = context && typeof context.context === "string" ? context.context : null;

  return (
    <ScrollView contentContainerStyle={styles.screen}>
      <Text style={styles.title}>Knowledge Task</Text>
      <Text style={styles.muted}>{workspace?.name ?? workspaceId}</Text>
      {agent ? <Text style={styles.muted}>Agent {agent.id} · {agent.status}{agent.requiresAttention ? " · attention" : ""}</Text> : null}

      <Text style={styles.label}>Task ID</Text>
      <TextInput value={taskId} onChangeText={setTaskId} editable={!agentTaskId} style={styles.input} accessibilityLabel="Knowledge task ID" />
      <Text style={styles.label}>Provider/model (caller-selected)</Text>
      <TextInput value={provider} onChangeText={setProvider} placeholder="codex/gpt-5.4" placeholderTextColor={theme.colors.foregroundMuted} style={styles.input} accessibilityLabel="Paseo provider and model" />
      <Text style={styles.label}>Knowledge query</Text>
      <TextInput value={query} onChangeText={setQuery} placeholder="What should be retrieved?" placeholderTextColor={theme.colors.foregroundMuted} style={styles.input} accessibilityLabel="Knowledge query" />
      <Text style={styles.label}>Task prompt</Text>
      <TextInput value={prompt} onChangeText={setPrompt} placeholder="What should the Paseo agent do?" placeholderTextColor={theme.colors.foregroundMuted} multiline style={[styles.input, styles.multiline]} accessibilityLabel="Knowledge task prompt" />
      <Text style={styles.label}>Vault scope</Text>
      <TextInput value={scope} onChangeText={(value) => setScope(value as VaultScope)} placeholder="work or personal" placeholderTextColor={theme.colors.foregroundMuted} style={styles.input} accessibilityLabel="Vault scope" />
      {scope === "personal" ? <Text style={styles.warning}>Personal sources require an explicit task-scoped grant and are not persisted.</Text> : null}
      <Text style={styles.label}>Project filter (optional)</Text>
      <TextInput value={project} onChangeText={setProject} style={styles.input} accessibilityLabel="Knowledge project filter" />

      <View style={styles.row}>
        <ActionButton label="Prepare context" disabled={busy || !query || !provider} onPress={() => run(async () => ({ kind: "prepare", value: await callPrepare({ taskId, workspaceId, provider, query, project: project || undefined, vaultScope: scope, includeRelated: false, maxSnippets: 8, maxChars: 12_000 }) }))} styles={styles} />
        <ActionButton label="Start task" disabled={busy || !prompt || !provider} onPress={() => run(async () => ({ kind: "start", value: await callStart({ taskId, workspaceId, provider, prompt }) }))} styles={styles} />
      </View>
      <View style={styles.row}>
        <ActionButton label="Search preview" disabled={busy || !query} onPress={() => run(async () => { const value = await callSearch({ query, taskId: scope === "personal" ? taskId : undefined, project: project || undefined, vaultScope: scope, limit: 8, includeRelated: false }); setSearchResults(value.results); return { kind: "search", value }; })} styles={styles} />
        <ActionButton label="Reconcile" disabled={busy || !agentId} onPress={() => run(async () => ({ kind: "reconcile", value: await callReconcile({ taskId, agentId, workspaceId }) }))} styles={styles} />
        <ActionButton label="Wait 30s" disabled={busy || !agentId} onPress={() => run(async () => ({ kind: "wait", value: await callWait({ taskId, agentId, timeoutMs: 30_000 }) }))} styles={styles} />
      </View>

      <Text style={styles.section}>Personal source grant</Text>
      <TextInput value={grantPath} onChangeText={setGrantPath} placeholder="relative/path.md" placeholderTextColor={theme.colors.foregroundMuted} style={styles.input} accessibilityLabel="Personal source relative path" />
      <View style={styles.row}>
        <ActionButton label="Grant for task" disabled={busy || !grantPath} onPress={() => run(async () => ({ kind: "grant", value: await callGrant({ taskId, path: grantPath }) }))} styles={styles} />
        <ActionButton label="Revoke path" disabled={busy || !grantPath} onPress={() => run(async () => ({ kind: "revoke", value: await callRevoke({ taskId, path: grantPath }) }))} styles={styles} />
        <ActionButton label="Revoke all" disabled={busy} onPress={() => run(async () => ({ kind: "revoke", value: await callRevoke({ taskId }) }))} styles={styles} />
      </View>

      {sourceManifest.length > 0 ? <Text style={styles.section}>Sources in prepared context</Text> : null}
      {sourceManifest.map((source) => (
        <View key={source.source_id} style={styles.sourceRow}>
          <Text style={styles.body}>{source.path}:{source.line_start}-{source.line_end}</Text>
          <ActionButton label="Remove" disabled={busy} onPress={() => run(async () => ({ kind: "remove", value: await callRemoveSource({ taskId, sourceId: source.source_id }) }))} styles={styles} />
        </View>
      ))}
      {contextText ? <><Text style={styles.section}>Untrusted context preview</Text><Text style={styles.context}>{contextText}</Text></> : null}
      {searchResults.length > 0 ? <><Text style={styles.section}>Search results</Text>{searchResults.map((result, index) => <Text key={String(result.source_id ?? index)} style={styles.body}>{String(result.path ?? result.title ?? "Knowledge result")}</Text>)}</> : null}
      <ResponseView response={response} styles={styles} />
    </ScrollView>
  );
}

type PanelResponse =
  | { kind: "status"; value: PluginStatusResponse }
  | { kind: "prepare"; value: PrepareKnowledgeTaskResponse }
  | { kind: "start"; value: StartKnowledgeTaskResponse }
  | { kind: "reconcile"; value: ReconcileKnowledgeTaskResponse }
  | { kind: "wait"; value: WaitKnowledgeTaskResponse }
  | { kind: "search"; value: SearchKnowledgeResponse }
  | { kind: "grant"; value: GrantPersonalSourceResponse }
  | { kind: "revoke"; value: RevokePersonalSourceResponse }
  | { kind: "remove"; value: RemoveKnowledgeSourceResponse }
  | { kind: "local-error"; value: string };

function ActionButton({ label, disabled, onPress, styles }: { label: string; disabled: boolean; onPress: () => void; styles: ReturnType<typeof makeStyles> }) {
  return <Pressable accessibilityRole="button" accessibilityLabel={label} disabled={disabled} onPress={onPress} style={[styles.button, disabled && styles.disabled]}><Text style={styles.buttonText}>{label}</Text></Pressable>;
}

function ResponseView({ response, styles }: { response: PanelResponse | null; styles: ReturnType<typeof makeStyles> }) {
  if (!response) return null;
  if (response.kind === "local-error") return <Text style={styles.error}>{response.value}</Text>;
  const value = response.value as { error?: { message: string } | null };
  return value.error ? <Text style={styles.error}>{value.error.message}</Text> : <Text style={styles.success}>Last action: {response.kind}</Text>;
}

function makeStyles(theme: WorkspaceProps["theme"], compact: boolean) {
  return {
    screen: { padding: compact ? 16 : 24, gap: 8, backgroundColor: theme.colors.surface0 },
    title: { color: theme.colors.foreground, fontSize: 20, fontWeight: "600" as const },
    label: { color: theme.colors.foregroundMuted, fontSize: 12, marginTop: 4 },
    muted: { color: theme.colors.foregroundMuted, fontSize: 12 },
    body: { color: theme.colors.foreground, fontSize: 14 },
    context: { color: theme.colors.foreground, fontSize: 13, backgroundColor: theme.colors.surface1, padding: 10 },
    input: { color: theme.colors.foreground, backgroundColor: theme.colors.surface1, borderColor: theme.colors.border, borderWidth: 1, borderRadius: 6, paddingHorizontal: 10, paddingVertical: 8 },
    multiline: { minHeight: 72, textAlignVertical: "top" as const },
    row: { flexDirection: "row" as const, flexWrap: "wrap" as const, gap: 8, marginTop: 4 },
    button: { backgroundColor: theme.colors.accent, borderRadius: 6, paddingHorizontal: 10, paddingVertical: 8 },
    buttonText: { color: theme.colors.accentForeground, fontSize: 12, fontWeight: "600" as const },
    disabled: { opacity: 0.45 },
    section: { color: theme.colors.foreground, fontSize: 15, fontWeight: "600" as const, marginTop: 12 },
    sourceRow: { flexDirection: "row" as const, alignItems: "center" as const, justifyContent: "space-between" as const, gap: 8 },
    warning: { color: theme.colors.statusWarning, fontSize: 12 },
    error: { color: theme.colors.statusDanger, fontSize: 13, marginTop: 8 },
    success: { color: theme.colors.statusSuccess, fontSize: 13, marginTop: 8 },
  };
}
