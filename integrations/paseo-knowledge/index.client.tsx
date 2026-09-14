import type { PluginClientContext } from "@getpaseo/plugin/client";
import { KnowledgeAgentPanel, KnowledgeWorkspacePanel } from "./client/panel.js";
import { KnowledgeAttachmentSource } from "./shared/contracts.js";

/** Client entrypoint: UI registrations and the governed attachment source only. */
export default function contribute(client: PluginClientContext) {
  client.addAttachmentSource(KnowledgeAttachmentSource);

  client.addWorkspacePanel({
    id: "knowledge-workspace",
    title: "Knowledge Task",
    icon: "BookOpen",
    context: "workspace",
    locations: ["workspace", "explorer"],
    Component: KnowledgeWorkspacePanel,
  });
  client.addWorkspacePanel({
    id: "knowledge-agent",
    title: "Knowledge Task",
    icon: "BookOpen",
    context: "agent",
    locations: ["workspace", "explorer"],
    Component: KnowledgeAgentPanel,
  });
}
