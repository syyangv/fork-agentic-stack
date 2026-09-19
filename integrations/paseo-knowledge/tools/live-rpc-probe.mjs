#!/usr/bin/env node
// Live RPC probe for the installed paseo-knowledge plugin.
//
// Invokes the plugin's daemon-side handlers through the official
// @getpaseo/client DaemonClient, which is the same supported
// `plugin.rpc.invoke` path the plugin's UI panels use via useRpc().
// This is a verification tool: it proves live installed-plugin RPC
// dispatch rather than exercising the core through a subprocess.
//
// Usage:
//   node tools/live-rpc-probe.mjs '[["knowledge.status", {}]]'
//   PASEO_DAEMON_URL=ws://127.0.0.1:6767/ws node tools/live-rpc-probe.mjs '[...]'
//
// Output is a single JSON document on stdout.

import { DaemonClient } from "@getpaseo/client/internal/daemon-client";

const DAEMON_URL = process.env.PASEO_DAEMON_URL ?? "ws://127.0.0.1:6767/ws";
const PLUGIN_ID = process.env.PASEO_KNOWLEDGE_PLUGIN_ID ?? "paseo-knowledge";
const CONNECT_TIMEOUT_MS = 15000;

const calls = JSON.parse(process.argv[2] ?? '[["knowledge.status", {}]]');

const client = new DaemonClient({
  url: DAEMON_URL,
  clientId: `paseo-knowledge-live-rpc-probe-${process.pid}`,
  clientType: "cli",
  appVersion: "0.8.0",
  connectTimeoutMs: CONNECT_TIMEOUT_MS,
  reconnect: { enabled: false },
});

const report = {
  daemonUrl: DAEMON_URL,
  pluginId: PLUGIN_ID,
  startedAt: new Date().toISOString(),
  connected: false,
  plugins: null,
  results: [],
};

try {
  await client.connect();
  report.connected = true;
  report.connectionState = client.getConnectionState();
  report.plugins = await client.listPlugins();

  for (const [method, input] of calls) {
    const startedAt = performance.now();
    try {
      const value = await client.invokePluginRpc(PLUGIN_ID, method, input);
      report.results.push({
        method,
        ok: true,
        ms: Number((performance.now() - startedAt).toFixed(3)),
        value,
      });
    } catch (error) {
      report.results.push({
        method,
        ok: false,
        ms: Number((performance.now() - startedAt).toFixed(3)),
        error: String(error?.message ?? error),
      });
    }
  }
} catch (error) {
  report.error = String(error?.stack ?? error);
} finally {
  await client.close().catch(() => {});
}

console.log(JSON.stringify(report, null, 2));
process.exit(report.connected && !report.results.some((r) => !r.ok) ? 0 : 1);
