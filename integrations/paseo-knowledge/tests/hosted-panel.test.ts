import { createElement } from "react";
import { act, create, type ReactTestRenderer, type ReactTestInstance } from "react-test-renderer";
import type { PluginWorkspacePanelProps } from "@getpaseo/plugin/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HostedKnowledgePanel } from "../client/hosted-panel.js";
import type { HostedConsentPreview } from "../shared/hosted-contracts.js";
const rpc = vi.hoisted(() => ({ availability: vi.fn(), preview: vi.fn(), execute: vi.fn(), cancel: vi.fn(), list: vi.fn(), read: vi.fn(), reject: vi.fn() }));
vi.mock("@getpaseo/plugin/client", () => ({ useRpc: (contract: { name: string }) => ({
  "knowledge.hosted.availability": rpc.availability, "knowledge.hosted.preview": rpc.preview,
  "knowledge.hosted.execute": rpc.execute, "knowledge.hosted.cancel": rpc.cancel,
  "knowledge.hosted.drafts.list": rpc.list, "knowledge.hosted.drafts.read": rpc.read, "knowledge.hosted.drafts.reject": rpc.reject,
})[contract.name] }));
vi.mock("react-native", () => ({ Pressable: "Pressable", Text: "Text", View: "View", ScrollView: "ScrollView" }));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const hash = "a".repeat(64);
const props = { workspaceId: "workspace_original", taskId: "task_original", prompt: "EXACT PROMPT", revision: "revision_1",
  theme: { colors: { foreground: "#111", foregroundMuted: "#777", surface0: "#fff", surface1: "#eee" } } as PluginWorkspacePanelProps["theme"] };
const preview = (): HostedConsentPreview => ({ previewId: "preview_1", challenge: "challenge_1", payloadDigest: hash, expiresAt: Date.now() + 60_000,
  taskId: props.taskId, workspaceId: props.workspaceId, provider: "opencode-go/synthetic-model", prompt: props.prompt,
  policy: { policyId: "policy_1", version: "1", endpoint: "https://opencode.ai/zen/go/v1", model: "synthetic-model", disclosure: "work-context-to-opencode-and-upstream" },
  sources: [{ sourceId: "source_1", path: "10-Projects/synthetic.md", text: "EXACT OUTBOUND SOURCE", contentHash: hash, excerptHash: hash, lineStart: 1, lineEnd: 2 }] });
let renderer: ReactTestRenderer | undefined;
const buttons = (prefix: string): ReactTestInstance[] => renderer!.root.findAll((node) => String(node.type) === "Pressable" && String(node.props.accessibilityLabel ?? "").startsWith(prefix));
async function press(prefix: string) {
  const target = buttons(prefix)[0]; expect(target, prefix).toBeDefined(); expect(target!.props.disabled).not.toBe(true);
  await act(async () => { target!.props.onPress(); });
}
async function mount(value: Parameters<typeof HostedKnowledgePanel>[0] = props) { await act(async () => { renderer = create(createElement(HostedKnowledgePanel, value)); }); }
async function consent() {
  await press("Review hosted disclosure");
  const checkbox = renderer!.root.findAll((node) => String(node.type) === "Pressable" && node.props.accessibilityRole === "checkbox")[0]!;
  expect(checkbox.props.accessibilityState.checked).toBe(false);
  await act(async () => { checkbox.props.onPress(); });
}
beforeEach(() => {
  vi.clearAllMocks();
  rpc.availability.mockResolvedValue({ available: true, error: null });
  rpc.preview.mockImplementation(async () => ({ preview: preview(), error: null }));
  rpc.execute.mockResolvedValue({ result: { status: "completed", executionId: "execution_1", completionId: "completion_1", draftId: "draft_1" }, error: null });
  rpc.cancel.mockResolvedValue({ cancelled: true, error: null });
  rpc.list.mockResolvedValue({ drafts: [], unreadableCount: 0, error: null });
  rpc.read.mockResolvedValue({ draft: null, error: { code: "draft_unavailable", retryable: false } });
  rpc.reject.mockResolvedValue({ rejected: true, error: null });
});
afterEach(async () => { if (renderer) await act(async () => { renderer!.unmount(); }); renderer = undefined; });
describe("actual hosted consent component lifecycle", () => {
  it("retains original cancellation after props change and transport failure, allowing read-only recovery without retries", async () => {
    let reject!: (error: Error) => void;
    rpc.execute.mockImplementation(() => new Promise((_, fail) => { reject = fail; }));
    await mount(); await consent();
    expect(JSON.stringify(renderer!.toJSON())).toContain("EXACT OUTBOUND SOURCE");
    await press("Approve and run hosted task"); expect(rpc.execute).toHaveBeenCalledOnce();
    await act(async () => { renderer!.update(createElement(HostedKnowledgePanel, { ...props, workspaceId: "workspace_new", taskId: "task_new", prompt: "NEW PROMPT", revision: "revision_2" })); });
    await act(async () => { reject(new Error("synthetic execute transport loss")); });
    expect(buttons("Cancel hosted task")).toHaveLength(1);
    expect(JSON.stringify(renderer!.toJSON())).not.toContain("EXACT OUTBOUND SOURCE");
    expect(JSON.stringify(renderer!.toJSON())).toContain("unknown");
    await press("Cancel hosted task");
    expect(rpc.cancel).toHaveBeenCalledWith({ workspaceId: props.workspaceId, taskId: props.taskId });
    await press("Refresh draft list");
    expect(rpc.list).toHaveBeenCalledWith({ workspaceId: "workspace_new" });
    expect(rpc.execute).toHaveBeenCalledOnce(); expect(rpc.preview).toHaveBeenCalledOnce();
  });
  it("clears cancellation on a known response and does not auto-preview or execute", async () => {
    await mount(); expect(rpc.preview).not.toHaveBeenCalled(); expect(rpc.execute).not.toHaveBeenCalled();
    await consent(); await press("Approve and run hosted task");
    expect(buttons("Cancel hosted task")).toHaveLength(0);
    expect(rpc.execute).toHaveBeenCalledOnce();
  });
  it("does not restore stale consent after A-to-B-to-A edits while preview is pending", async () => {
    let resolve!: (value: { preview: HostedConsentPreview; error: null }) => void;
    rpc.preview.mockImplementation(() => new Promise((done) => { resolve = done; }));
    await mount(); await press("Review hosted disclosure");
    await act(async () => { renderer!.update(createElement(HostedKnowledgePanel, { ...props, prompt: "EDITED", revision: "revision_2" })); });
    await act(async () => { renderer!.update(createElement(HostedKnowledgePanel, props)); });
    await act(async () => { resolve({ preview: preview(), error: null }); });
    expect(renderer!.root.findAll((node) => String(node.type) === "Pressable" && node.props.accessibilityRole === "checkbox")).toHaveLength(0);
    expect(JSON.stringify(renderer!.toJSON())).not.toContain("EXACT OUTBOUND SOURCE"); expect(rpc.execute).not.toHaveBeenCalled();
  });
  it("disables hosted preparation outside work workspace mode and requires an explicit confirmation before rejection", async () => {
    await mount({ ...props, allowPrepare: false });
    expect(buttons("Review hosted disclosure")[0]?.props.disabled).toBe(true); expect(rpc.preview).not.toHaveBeenCalled();
    await act(async () => { renderer!.update(createElement(HostedKnowledgePanel, props)); });
    const summary = { draftId: "draft_1", taskId: props.taskId, workspaceId: props.workspaceId, model: "synthetic-model", completionId: "completion_1", outputDigest: hash, status: "pending_review" };
    rpc.list.mockResolvedValue({ drafts: [summary], unreadableCount: 0, error: null });
    const output = "<script>Never execute this untrusted draft</script>";
    rpc.read.mockResolvedValue({ draft: { ...summary, output, sources: [] }, error: null });
    await press("Refresh draft list"); await press("Read draft draft_1");
    expect(renderer!.root.findAll((node) => String(node.type) === "Text" && node.children.includes(output))).not.toHaveLength(0);
    await press("Reject this draft"); expect(rpc.reject).not.toHaveBeenCalled();
    await press("Confirm reject draft");
    expect(rpc.reject).toHaveBeenCalledWith({ workspaceId: props.workspaceId, draftId: "draft_1", outputDigest: hash });
    expect(rpc.execute).not.toHaveBeenCalled();
  });
});
