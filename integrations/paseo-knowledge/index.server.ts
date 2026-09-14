import type { PluginServerContext } from "@getpaseo/plugin/server";
import {
  GrantPersonalSourceRpc,
  KnowledgeAttachmentSearchRpc,
  PluginStatusRpc,
  PrepareKnowledgeTaskRpc,
  ReconcileKnowledgeTaskRpc,
  RemoveKnowledgeSourceRpc,
  RevokePersonalSourceRpc,
  SearchKnowledgeRpc,
  StartKnowledgeTaskRpc,
  WaitKnowledgeTaskRpc,
} from "./shared/contracts.js";
import { createProductionService } from "./server/service.js";

/** Server entrypoint: RPC handlers and the supervised knowledge service only. */
export default function contribute(server: PluginServerContext) {
  const service = createProductionService();

  server.handle(PluginStatusRpc, (input, context) => service.status(input, context.paseo));
  server.handle(SearchKnowledgeRpc, (input) => service.search(input));
  server.handle(PrepareKnowledgeTaskRpc, (input) => service.prepare(input));
  server.handle(StartKnowledgeTaskRpc, (input, context) => service.start(input, context.paseo));
  server.handle(ReconcileKnowledgeTaskRpc, (input, context) => service.reconcile(input, context.paseo));
  server.handle(WaitKnowledgeTaskRpc, (input, context) => service.wait(input, context.paseo));
  server.handle(RemoveKnowledgeSourceRpc, (input) => service.removeSource(input.taskId, input.sourceId));
  server.handle(GrantPersonalSourceRpc, (input) => service.grantPersonal(input.taskId, input.path, input.expiresInSeconds));
  server.handle(RevokePersonalSourceRpc, (input) => service.revokePersonal(input.taskId, input.path));
  server.handle(KnowledgeAttachmentSearchRpc, (input) => service.attachmentSearch(input.query));

  return () => service.dispose();
}
