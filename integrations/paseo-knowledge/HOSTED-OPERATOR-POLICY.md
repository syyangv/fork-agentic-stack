# Hosted execution operator policy

This is a candidate opt-in path. **No live policy file is created by installation.** Do not enable it before the production security and device-validation gates are approved.

## Policy location and safe defaults

The plugin reads `hosted-policy.json` beneath the existing absolute `PASEO_KNOWLEDGE_RUNTIME_DIR`. Missing runtime/policy or `enabled: false` means no hosted application and no credential resolution. Invalid/expired policy fails closed with a generic error code. No command, endpoint, token or API-key value can be supplied through a client RPC or this policy format.

The runtime must be owner-controlled `0700`; the policy must be a regular owner-controlled `0600` file with one hardlink, not a symlink. Trusted runtime aliases are canonicalized before private stores are assembled. Reads are capped at 16KiB. Do not share runtime directories between daemon instances or use untrusted/network filesystems.

`hosted-policy.example.json` is deliberately disabled, with credential approval false, expired timestamps and placeholder fixture IDs/hashes. It is not an enabled configuration. Do not overwrite an existing policy or merely toggle `enabled`.

## Required owner decisions before opting in

- Confirm the single-owner daemon authentication boundary using both authorized access and unauthorized rejection. Workspace existence is not a per-user ACL. Multi-user requirements need platform support beyond this plugin.
- Resolve or explicitly assess the signed host's dependency posture separately from the plugin. The inspected Paseo0.15.4 app contains Zod4.4.3, within the published affected range of GHSA-mrhx-c7xc-m3w5; no runtime exploitability/vendor-backport assessment is claimed here. Latest official release observed was also0.15.4. The plugin's patched Zod does not replace this bundled copy.
- Approve model, endpoint/upstream disclosure, account/credential source, limits and fresh UTC approval/expiry timestamps. Approval lifetime is capped at31days; choose a shorter acceptance window and honor applicable provider-term deadlines. Public privacy claims are not proof of account configuration or independent guarantees.
- For first acceptance, use `mode: synthetic-only`, one selected workspace, a known synthetic fixture path/full-file SHA256 plus `allowedPromptHashes` for the exact UTF-8 prompt, and one request per UTC day. This is exact-fixture allowlisting, not automatic classification of sensitive text. It does not create or alter a formal note.
- `mode: work` explicitly permits prepared work context in the selected workspaces. Omitting `allowedSources` permits any otherwise-valid prepared work source there, and omitting `allowedPromptHashes` permits any otherwise-valid user-approved prompt; a nonempty list further restricts it. An explicitly empty list is invalid, never a wildcard. Personal context and bound-agent reuse remain refused.

## Credentials

The policy contains only `credentialSource` metadata and explicit `credentialReuseApproved: true`, not a secret:

- `environment`: use the existing `OPENCODE_API_KEY` from the daemon environment. Populate it outside chat through an approved secret-management/deployment path; changing an already-running daemon's environment may require its controlled lifecycle procedure.
- `opencode-session`: explicitly reuse a stored OpenCode OAuth credential through the existing supported CLI resolver. It does not fall back to the old Go API key or an environment key. It resolves per execution, checks expiry before and after asynchronous authorization, and does not log token values. Automatic login/OAuth refresh is not implemented. Confirm the intended account in OpenCode; no per-account identity guarantee beyond the current resolver is asserted.

The previous one-shot synthetic smoke authorized only that request. It did **not** approve ongoing credential reuse or real-vault transfers. Do not run another live inference request under that spent authorization.

## Reload, expiry and revocation

After explicit deployment and policy authorization, configure the private file and use the supported scoped `paseo plugin reload paseo-knowledge`. Do not restart the daemon just for this policy, weaken its password, or edit the signed app. The original generic Start remains fail-closed; the separate hosted consent flow is required.

Approval is re-read and fingerprint-checked before credential acquisition, source verification, transport, and capture checks. Any change to parsed/normalized configuration, withdrawal, permissions failure, runtime retarget or expiry denies new outbound work under the loaded policy. Formatting-only rewrites do not revoke because the fingerprint covers parsed/normalized configuration. Reload is required to adopt a changed policy. A provider request already sent cannot be recalled, and a private draft already committed cannot be undone retroactively. Known executions remain cancellable even after policy/workspace availability changes.

## Local request budget

`maxRequestsPerUtcDay` is required (1–100). Exclusive0600 markers in `hosted-provider-attempts` reserve slots before HTTP dispatch and are flushed through canonical ancestors. Partial/failed/uncertain reservations are spent permanently; no automatic refunds/replays. Counters survive process/app recreation and policy ID/version changes, and roll over at UTC midnight, not the Mac's local date.

This limits only this hosted workflow's reserved attempts—not all OpenCode usage, a rolling24h window, or a dollar-denominated account spend ceiling. Other tools/agents/account settings can consume usage independently. No automatic marker deletion or reset is provided. Cancellation or policy withdrawal while a slot is being reserved can spend the slot without sending HTTP; this is conservative by design.

## Still required before live enablement

Independent review; separately approved Git delivery/deployment; production auth and host dependency assessment; explicit policy/account authorization; controlled synthetic end-to-end run; native/iPhone consent, expiry, cancellation, reconnect and unknown-outcome recovery acceptance. No enabled policy file, new model request or production change is part of implementing this code.
