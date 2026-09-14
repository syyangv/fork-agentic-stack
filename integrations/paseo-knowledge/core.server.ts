import { spawn } from "node:child_process";
import path from "node:path";
import type { SourceRef } from "./contracts.shared.js";

export interface CoreContextRequest {
  taskId: string;
  query: string;
  project?: string;
  limit: number;
  maxSnippets: number;
  maxChars: number;
  excludeSourceIds: string[];
  includeRelated: boolean;
  vaultScope: "work" | "personal";
  /** A future supervised core may consume these from stdin; never persist them. */
  personalGrantPaths: string[];
}

export interface CoreSearchRequest {
  query: string;
  taskId?: string;
  project?: string;
  limit: number;
  includeRelated: boolean;
  vaultScope: "work" | "personal";
  personalGrantPaths: string[];
}

export interface CoreCaptureRequest {
  taskId: string;
  turnId: string;
  taskStatus: "completed";
  turnStatus: "completed";
  resultText: string;
  sourceManifest: SourceRef[];
  personalContextUsed: boolean;
}

export interface KnowledgeCore {
  status(): Promise<Record<string, unknown>>;
  search(request: CoreSearchRequest): Promise<Record<string, unknown>>;
  buildContext(request: CoreContextRequest): Promise<Record<string, unknown>>;
  proposeNote(request: CoreCaptureRequest): Promise<Record<string, unknown>>;
}

export interface KnowledgeCliRunnerOptions {
  pythonExecutable: string;
  cliPath: string;
  workVaultPath?: string;
  personalVaultPath?: string;
  runtimeDir?: string;
  draftStorePath?: string;
}

export class KnowledgeCoreConfigurationError extends Error {
  public readonly code = "configuration";
}

export class KnowledgeCoreInvocationError extends Error {
  public readonly code = "retrieval_failed";

  public constructor(message: string) {
    super(message);
    this.name = "KnowledgeCoreInvocationError";
  }
}

/**
 * Production boundary to the versioned Python JSON CLI. The child process is
 * intentionally disposable; task grants live in the supervising plugin, not
 * in Python process memory.
 */
export class KnowledgeCliRunner implements KnowledgeCore {
  private readonly options: KnowledgeCliRunnerOptions;

  public constructor(options: KnowledgeCliRunnerOptions) {
    assertAbsolute(options.pythonExecutable, "pythonExecutable");
    assertAbsolute(options.cliPath, "cliPath");
    for (const [name, value] of Object.entries(options)) {
      if (name.endsWith("Path") || name === "runtimeDir") {
        if (value !== undefined) assertAbsolute(value, name);
      }
    }
    this.options = { ...options };
  }

  public static fromEnvironment(env: NodeJS.ProcessEnv = process.env): KnowledgeCliRunner {
    const pythonExecutable = env.PASEO_KNOWLEDGE_PYTHON_EXECUTABLE;
    const cliPath = env.PASEO_KNOWLEDGE_CLI;
    if (!pythonExecutable || !cliPath) {
      throw new KnowledgeCoreConfigurationError(
        "Set PASEO_KNOWLEDGE_PYTHON_EXECUTABLE and PASEO_KNOWLEDGE_CLI to absolute paths.",
      );
    }
    return new KnowledgeCliRunner({
      pythonExecutable,
      cliPath,
      workVaultPath: env.PASEO_KNOWLEDGE_WORK_VAULT,
      personalVaultPath: env.PASEO_KNOWLEDGE_PERSONAL_VAULT,
      runtimeDir: env.PASEO_KNOWLEDGE_RUNTIME_DIR,
      draftStorePath: env.PASEO_KNOWLEDGE_DRAFT_STORE,
    });
  }

  public status(): Promise<Record<string, unknown>> {
    const args = ["status"];
    this.addPathArgs(args);
    return this.invoke(args);
  }

  public search(request: CoreSearchRequest): Promise<Record<string, unknown>> {
    if (request.vaultScope === "personal") {
      // The checked-in CLI has no personal-grant transport. Do not silently
      // fall back to an unscoped personal read.
      throw new KnowledgeCoreInvocationError(
        "Personal-source retrieval requires a supervised in-process core seam; the current CLI cannot accept grants.",
      );
    }
    const args = ["search", request.query, "--limit", String(request.limit)];
    if (request.taskId) args.push("--task-id", request.taskId);
    if (request.project) args.push("--project", request.project);
    if (request.includeRelated) args.push("--include-related");
    this.addPathArgs(args);
    return this.invoke(args);
  }

  public buildContext(request: CoreContextRequest): Promise<Record<string, unknown>> {
    if (request.vaultScope === "personal") {
      throw new KnowledgeCoreInvocationError(
        "Personal-source context requires a supervised in-process core seam; the current CLI cannot accept grants.",
      );
    }
    const args = [
      "build_context",
      "--task-id",
      request.taskId,
      "--query",
      request.query,
      "--limit",
      String(request.limit),
      "--max-snippets",
      String(request.maxSnippets),
      "--max-chars",
      String(request.maxChars),
    ];
    if (request.project) args.push("--project", request.project);
    if (request.includeRelated) args.push("--include-related");
    for (const sourceId of request.excludeSourceIds) args.push("--exclude-source-id", sourceId);
    this.addPathArgs(args);
    return this.invoke(args);
  }

  public proposeNote(request: CoreCaptureRequest): Promise<Record<string, unknown>> {
    const draftStorePath = this.options.draftStorePath ?? (
      this.options.runtimeDir ? path.join(this.options.runtimeDir, "drafts.sqlite3") : undefined
    );
    if (!draftStorePath) {
      throw new KnowledgeCoreInvocationError(
        "Knowledge result capture is unavailable until an explicit private draft store path is configured.",
      );
    }
    const args = [
      "propose_note",
      "--task-id",
      request.taskId,
      "--turn-id",
      request.turnId,
      "--task-status",
      request.taskStatus,
      "--draft-store",
      draftStorePath,
      "--allow-protected-runtime",
    ];
    return this.invoke(args, {
      result: request.resultText,
      turn_status: request.turnStatus,
      source_manifest: request.sourceManifest,
      personal_context_used: request.personalContextUsed,
    });
  }

  private addPathArgs(args: string[]): void {
    const { workVaultPath, personalVaultPath, runtimeDir } = this.options;
    if (workVaultPath) args.push("--work-vault", workVaultPath);
    if (personalVaultPath) args.push("--personal-vault", personalVaultPath);
    if (runtimeDir) args.push("--runtime-dir", runtimeDir);
  }

  private invoke(args: string[], stdinPayload?: Record<string, unknown>): Promise<Record<string, unknown>> {
    const input = stdinPayload === undefined ? undefined : `${JSON.stringify(stdinPayload)}\n`;
    return new Promise((resolve, reject) => {
      const child = spawn(this.options.pythonExecutable, [this.options.cliPath, ...args], {
        shell: false,
        stdio: ["pipe", "pipe", "pipe"],
      });
      let stdout = "";
      let stderr = "";
      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk: string) => { stdout += chunk; });
      child.stderr.on("data", (chunk: string) => { stderr += chunk; });
      child.once("error", () => {
        reject(new KnowledgeCoreInvocationError("Knowledge core process could not be started."));
      });
      child.once("close", (exitCode) => {
        let parsed: unknown;
        try {
          parsed = JSON.parse(stdout);
        } catch {
          reject(new KnowledgeCoreInvocationError(
            exitCode === 0
              ? "Knowledge core returned invalid JSON."
              : "Knowledge core failed without a versioned JSON response.",
          ));
          return;
        }
        if (!isRecord(parsed)) {
          reject(new KnowledgeCoreInvocationError("Knowledge core returned a non-object JSON response."));
          return;
        }
        // Core status/search/context failures are structured on stdout even
        // when the CLI exits non-zero. Stderr remains diagnostics only.
        void stderr;
        resolve(parsed);
      });
      if (input !== undefined) child.stdin.end(input);
      else child.stdin.end();
    });
  }
}

function assertAbsolute(value: string, name: string): void {
  if (!path.isAbsolute(value)) {
    throw new KnowledgeCoreConfigurationError(`${name} must be an absolute path; PATH lookup is not allowed.`);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
