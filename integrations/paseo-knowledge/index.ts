import type { PluginContext } from "@getpaseo/plugin";
import {
  GrantPersonalSourceRpc,
  KnowledgeAttachmentSearchRpc,
  KnowledgeAttachmentSource,
  PluginStatusRpc,
  PrepareKnowledgeTaskRpc,
  ReconcileKnowledgeTaskRpc,
  RemoveKnowledgeSourceRpc,
  RevokePersonalSourceRpc,
  SearchKnowledgeRpc,
  StartKnowledgeTaskRpc,
  WaitKnowledgeTaskRpc,
} from "./contracts.shared.js";
import { KnowledgeAgentPanel, KnowledgeWorkspacePanel } from "./panel.client.js";
import { createProductionService } from "./service.server.js";

/** Paseo plugin entrypoint. No live installation or daemon connection is made here. */
export default function contribute(plugin: PluginContext) {
  const service = createProductionService();

  plugin.handle(PluginStatusRpc, (input, context) => service.status(input, context.paseo));
  plugin.handle(SearchKnowledgeRpc, (input) => service.search(input));
  plugin.handle(PrepareKnowledgeTaskRpc, (input) => service.prepare(input));
  plugin.handle(StartKnowledgeTaskRpc, (input, context) => service.start(input, context.paseo));
  plugin.handle(ReconcileKnowledgeTaskRpc, (input, context) => service.reconcile(input, context.paseo));
  plugin.handle(WaitKnowledgeTaskRpc, (input, context) => service.wait(input, context.paseo));
  plugin.handle(RemoveKnowledgeSourceRpc, (input) => service.removeSource(input.taskId, input.sourceId));
  plugin.handle(GrantPersonalSourceRpc, (input) => service.grantPersonal(input.taskId, input.path, input.expiresInSeconds));
  plugin.handle(RevokePersonalSourceRpc, (input) => service.revokePersonal(input.taskId, input.path));
  plugin.handle(KnowledgeAttachmentSearchRpc, (input) => service.attachmentSearch(input.query));
  plugin.addAttachmentSource(KnowledgeAttachmentSource);

  plugin.addWorkspacePanel({
    id: "knowledge-workspace",
    title: "Knowledge Task",
    icon: "BookOpen",
    context: "workspace",
    locations: ["workspace", "explorer"],
    Component: KnowledgeWorkspacePanel,
  });
  plugin.addWorkspacePanel({
    id: "knowledge-agent",
    title: "Knowledge Task",
    icon: "BookOpen",
    context: "agent",
    locations: ["workspace", "explorer"],
    Component: KnowledgeAgentPanel,
  });

  return () => service.dispose();
}
