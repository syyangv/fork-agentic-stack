import type { PluginServerContext } from "@getpaseo/plugin/server";
import { HostedAvailabilityRpc, HostedPreviewRpc, HostedExecuteRpc, HostedCancelRpc,
  HostedDraftListRpc, HostedDraftReadRpc, HostedDraftRejectRpc } from "../shared/hosted-contracts.js";
import type { HostedKnowledgeApplication } from "./hosted-application.js";
const unavailable = { code: "hosted_disabled", retryable: false as const };

/** No application is provided by the production entrypoint: hosted execution stays disabled. */
export function registerHostedHandlers(server: PluginServerContext, application?: HostedKnowledgeApplication): () => void {
  server.handle(HostedAvailabilityRpc, (input, context) => application?.availability(input.workspaceId, context.paseo) ?? { available: false, error: unavailable });
  server.handle(HostedPreviewRpc, (input, context) => application?.preview(input, context.paseo) ?? { preview: null, error: unavailable });
  server.handle(HostedExecuteRpc, (input, context) => application?.execute(input, context.paseo) ?? { result: null, error: unavailable });
  server.handle(HostedCancelRpc, (input, context) => application?.cancel(input, context.paseo) ?? { cancelled: false, error: unavailable });
  server.handle(HostedDraftListRpc, (input, context) => application?.list(input.workspaceId, context.paseo) ?? { drafts: [], unreadableCount: 0, error: unavailable });
  server.handle(HostedDraftReadRpc, (input, context) => application?.read(input, context.paseo) ?? { draft: null, error: unavailable });
  server.handle(HostedDraftRejectRpc, (input, context) => application?.reject(input, context.paseo) ?? { rejected: false, error: unavailable });
  return () => application?.dispose();
}
