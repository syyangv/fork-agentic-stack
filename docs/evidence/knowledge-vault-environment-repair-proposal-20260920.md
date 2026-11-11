# Knowledge Vault — Environment Repair Proposal

Date: 2026-09-20
Status: **PROPOSAL ONLY — NOT EXECUTED**
Safety gate: requires explicit coordinator approval after this file and the recovery checkpoint are read back. No daemon restart, desktop quit/relaunch, plugin reload, config write, or source update occurred while preparing this proposal.

## Finding

The supported direct client reaches the daemon and the installed plugin is
`enabled=true/status=running`, but `knowledge.status` returns:

```text
configured=false
Set PASEO_KNOWLEDGE_PYTHON_EXECUTABLE and PASEO_KNOWLEDGE_CLI to absolute paths.
```

The six knowledge variables are present in both:

- `/Users/syang/Library/LaunchAgents/com.syang.paseo.daemon.plist`
- the loaded LaunchAgent job definition from `launchctl print`

They are absent from the actual daemon process. Targeted process inspection found
the current chain is:

```text
/Applications/Paseo.app/Contents/MacOS/Paseo
  -> Paseo Supervisor
    -> Paseo Daemon
```

The LaunchAgent reports `spawn scheduled` and has no active PID. Paseo Desktop's
`daemon-manager.ts` starts its child with `baseEnv: process.env` and only adds
desktop overlays (`PASEO_DESKTOP_MANAGED`, `PASEO_CLI`, and the web-ui setting).
The plugin runtime uses Node `fork()` without an environment override. Paseo
plugin config supports source/path/enabled, not a per-plugin environment map.

Therefore a plugin-only reload cannot repair this mismatch: it creates another
plugin child from the same desktop daemon environment.

## Recommended owner transition

Use the already-configured LaunchAgent as the sole daemon owner. Do not run the
desktop-managed daemon and LaunchAgent daemon concurrently on `127.0.0.1:6767`.

### Preconditions (read-only, immediately before execution)

```sh
id -u                         # must remain 501
launchctl print gui/501/com.syang.paseo.daemon
plutil -p /Users/syang/Library/LaunchAgents/com.syang.paseo.daemon.plist
git -C /Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault rev-parse HEAD
```

Confirm the plist still has only the approved six `PASEO_KNOWLEDGE_*` path
variables plus its existing `PATH`, `HOME`, `PASEO_HOME`, and `XPC_SERVICE_NAME`;
do not print or copy unrelated environment values. Confirm no unrelated active
provider task is running. Save/read back the recovery checkpoint first.

### Exact proposed action (not executed)

1. In Paseo Desktop, use the supported local-host setting:

   `Settings -> local host -> Manage built-in daemon -> Pause built-in daemon`

   The application’s supported toggle persists `manageBuiltInDaemon=false` and
   stops the desktop-managed daemon. This is expected to interrupt agents and
   terminals attached to that built-in daemon, including the coordinating session;
   recovery must resume from the checkpoint.

2. After verifying the desktop-managed daemon no longer owns `127.0.0.1:6767`,
   replace the already-loaded LaunchAgent definition and bootstrap the existing
   plist exactly once:

   ```sh
   if /bin/launchctl print gui/501/com.syang.paseo.daemon >/dev/null 2>&1; then
     /bin/launchctl bootout gui/501/com.syang.paseo.daemon
   fi
   /bin/launchctl bootstrap gui/501 /Users/syang/Library/LaunchAgents/com.syang.paseo.daemon.plist
   ```

   This does not edit `config.json`, change `--listen`, expose a new address,
   install/remove a plugin, or touch either vault. `RunAtLoad=true` and
   `KeepAlive=true` in the existing plist make LaunchAgent ownership persistent.

## Targeted postconditions (read-only)

```sh
launchctl print gui/501/com.syang.paseo.daemon \
  | grep -E 'state =|pid =|PASEO_KNOWLEDGE_'

cd /Users/syang/.agentic-stack-kv-rpc/integrations/paseo-knowledge
node tools/live-rpc-probe.mjs '[["knowledge.status",{}]]'

/usr/bin/curl -fL --max-time 5 -sS -o /dev/null \
  -w 'GET / -> %{http_code} %{content_type} %{size_download} bytes\\n' \
  http://127.0.0.1:6767/
```

Required result:

```text
plugin enabled=true status=running
knowledge.status configured=true coreStatus=ready
GET / -> 200 text/html; charset=utf-8 ... bytes
```

Then verify an ordinary synthetic no-file/no-vault provider session before any
installed-source update or plugin reload. This proposal does not approve a
parked draft or any formal work-vault write.

## Rollback proposal (not executed)

If the LaunchAgent owner fails its targeted checks, do not start a second daemon.
Capture the exact failure, then—only with coordinator approval—stop the LaunchAgent
owner, use Paseo Desktop’s supported `Manage built-in daemon -> Enable` action, and
verify the desktop daemon owns the listener again. The rollback restores ownership,
not the missing knowledge environment; `configured=false` would be expected until
an approved environment-bearing desktop launch is available.

Do not use `killall`, broad process-name termination, `rm`, `git reset`, force push,
or a second `paseo daemon start` while either owner may still be listening.

## Approval gate

Required approval wording:

> I approve exactly one ownership transition for the existing local Paseo daemon:
> pause Paseo Desktop’s built-in daemon management, boot out and bootstrap the
> existing `com.syang.paseo.daemon` LaunchAgent plist at
> `/Users/syang/Library/LaunchAgents/com.syang.paseo.daemon.plist`, and verify
> configured/core-ready over direct loopback RPC. I understand the desktop daemon
> interruption may terminate/reconnect the coordinating agent and its terminals.
> No other config keys, listener addresses, plugins, vaults, credentials, or formal
> notes may be changed. Roll back only through the supported ownership controls if
> postconditions fail.

Until that exact approval is received, this proposal remains unexecuted.
