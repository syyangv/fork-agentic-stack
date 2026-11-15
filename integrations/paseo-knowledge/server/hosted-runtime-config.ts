import {constants, lstatSync, openSync, fstatSync, readSync, closeSync, realpathSync} from "node:fs";
import {isAbsolute, join, posix, win32} from "node:path";
import {z} from "zod";
import {digest, HostedDisclosurePolicySchema} from "./hosted-task-policy.js";

const MAX_CONFIG_BYTES = 16_384;
const MAX_WORKSPACES = 16;
const MAX_SOURCES = 8;
const MAX_PROMPTS = 8;
const MAX_APPROVAL_MS = 31 * 24 * 60 * 60 * 1_000;
const id = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/);
const relativeSource = z.string().min(1).max(1_024).refine(value => !/[\x00-\x1f]/.test(value)
  && !posix.isAbsolute(value) && !win32.isAbsolute(value) && !value.includes("\\")
  && posix.normalize(value) === value && !value.split("/").includes("..") && value !== ".");
const enabledSchema = z.object({
  version: z.literal(1), enabled: z.literal(true), mode: z.enum(["synthetic-only", "work"]),
  credentialSource: z.enum(["environment", "opencode-session"]), credentialReuseApproved: z.literal(true),
  disclosureAccepted: z.literal("work-context-to-opencode-and-upstream"),
  approvedAt: z.string().datetime(), expiresAt: z.string().datetime(),
  maxRequestsPerUtcDay: z.number().int().min(1).max(100),
  allowedWorkspaceIds: z.array(id).min(1).max(MAX_WORKSPACES),
  allowedSources: z.array(z.object({ path: relativeSource, contentHash: z.string().regex(/^[a-f0-9]{64}$/i).transform(v => v.toLowerCase()) }).strict()).min(1).max(MAX_SOURCES).optional(),
  allowedPromptHashes: z.array(z.string().regex(/^[a-f0-9]{64}$/i).transform(v => v.toLowerCase())).min(1).max(MAX_PROMPTS).optional(),
  policy: HostedDisclosurePolicySchema,
}).strict();
const disabledSchema = enabledSchema.partial().extend({version: z.literal(1), enabled: z.literal(false), credentialReuseApproved: z.boolean().optional()}).strict();
const configSchema = z.union([enabledSchema, disabledSchema]);
export type EnabledHostedRuntimeConfig = z.infer<typeof enabledSchema>;
export interface LoadedHostedRuntimeConfig {
  readonly config: EnabledHostedRuntimeConfig;
  readonly fingerprint: string;
  readonly runtime: string;
}
export class HostedRuntimeConfigError extends Error {
  public constructor(public readonly code: "hosted_policy_invalid" | "hosted_policy_expired" | "hosted_runtime_unavailable") {
    super("Hosted operator policy is unavailable or invalid.");
  }
}
function rejected(code: HostedRuntimeConfigError["code"] = "hosted_policy_invalid"): never { throw new HostedRuntimeConfigError(code); }

/** Owner-managed local policy, not a client-supplied attestation or per-user ACL. */
export function readHostedRuntimeConfig(directory: string, now = Date.now()): LoadedHostedRuntimeConfig | null {
  if (!isAbsolute(directory)) rejected("hosted_runtime_unavailable");
  let runtime: string;
  try { runtime = realpathSync(directory); } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    return rejected("hosted_runtime_unavailable");
  }
  const stat = lstatSync(runtime);
  if (!stat.isDirectory() || (stat.mode & 0o777) !== 0o700 || stat.uid !== process.getuid?.()) rejected("hosted_runtime_unavailable");
  let file: number;
  try { file = openSync(join(runtime, "hosted-policy.json"), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return null; return rejected(); }
  let raw: unknown;
  try {
    const info = fstatSync(file);
    if (!info.isFile() || info.nlink !== 1 || info.uid !== process.getuid?.() || (info.mode & 0o777) !== 0o600 || info.size > MAX_CONFIG_BYTES) rejected();
    const buffer = Buffer.alloc(MAX_CONFIG_BYTES + 1);
    let length = 0;
    while (length < buffer.length) {
      const count = readSync(file, buffer, length, buffer.length - length, null);
      if (!count) break;
      length += count;
    }
    if (length > MAX_CONFIG_BYTES) rejected();
    raw = JSON.parse(buffer.subarray(0, length).toString("utf8"));
  } catch { return rejected(); } finally { closeSync(file); }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) rejected();
  const record = raw as Record<string, unknown>;
  for (const [field, limit] of [["allowedWorkspaceIds", MAX_WORKSPACES], ["allowedSources", MAX_SOURCES], ["allowedPromptHashes", MAX_PROMPTS]] as const) {
    const values = record[field];
    if (values !== undefined && (!Array.isArray(values) || values.length > limit)) rejected();
  }
  const parsed = configSchema.safeParse(raw);
  if (!parsed.success) rejected();
  if (!parsed.data.enabled) return null;
  const config = parsed.data;
  const approved = Date.parse(config.approvedAt), expires = Date.parse(config.expiresAt);
  if (!Number.isSafeInteger(now) || approved > now || expires <= approved || expires - approved > MAX_APPROVAL_MS) rejected();
  if (expires <= now) rejected("hosted_policy_expired");
  if (new Set(config.allowedWorkspaceIds).size !== config.allowedWorkspaceIds.length) rejected();
  if (config.mode === "synthetic-only" && (!config.allowedSources?.length || !config.allowedPromptHashes?.length)) rejected();
  if (config.allowedSources && new Set(config.allowedSources.map(s => s.path)).size !== config.allowedSources.length) rejected();
  if (config.allowedPromptHashes && new Set(config.allowedPromptHashes).size !== config.allowedPromptHashes.length) rejected();
  return {config, runtime, fingerprint: digest(JSON.stringify(config))};
}
