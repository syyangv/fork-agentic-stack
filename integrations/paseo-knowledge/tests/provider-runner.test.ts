import { createServer, type IncomingMessage } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, expect, it } from "vitest";
import {
  ProviderRunner,
  loadOpenCodeCredential,
  parseOpenCodeCredential,
  type ProviderRunnerConfig,
  type ProviderSource,
} from "../server/provider-runner.js";

interface RecordedRequest {
  readonly headers: IncomingMessage["headers"];
  readonly body: Record<string, unknown> | null;
}

interface ServerReply {
  readonly status: number;
  readonly body: unknown;
  readonly headers?: Readonly<Record<string, string>>;
  readonly rawBody?: string;
  readonly bodyDelayMs?: number;
}

function startServer(handler: (requestCount: number) => Promise<ServerReply> | ServerReply) {
  const requests: RecordedRequest[] = [];
  let notifyStarted: (() => void) | null = null;
  const started = new Promise<void>((resolve) => {
    notifyStarted = resolve;
  });
  const server = createServer((request, response) => {
    let raw = "";
    request.on("data", (chunk) => {
      raw += chunk;
    });
    request.on("end", () => {
      requests.push({
        headers: request.headers,
        body: raw ? JSON.parse(raw) as Record<string, unknown> : null,
      });
      notifyStarted?.();
      void (async () => {
        try {
          const reply = await handler(requests.length);
          response.writeHead(reply.status, { "Content-Type": "application/json", ...reply.headers });
          if (reply.bodyDelayMs) {
          response.flushHeaders();
          await new Promise((resolve) => setTimeout(resolve, reply.bodyDelayMs));
        }
        response.end(reply.rawBody ?? JSON.stringify(reply.body));
        } catch {
          response.destroy();
        }
      })();
    });
  });
  return new Promise<{
    url: string;
    requests: RecordedRequest[];
    started: Promise<void>;
    close: () => Promise<void>;
  }>((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const address = server.address() as AddressInfo;
      resolve({
        url: `http://127.0.0.1:${address.port}`,
        requests,
        started,
        close: () => new Promise<void>((done) => server.close(() => done())),
      });
    });
  });
}

const source: ProviderSource = {
  path: "10-Projects/synthetic.md",
  text: "UNTRUSTED SOURCE TEXT",
};

function config(endpoint: string, overrides: Partial<ProviderRunnerConfig> = {}): ProviderRunnerConfig {
  return {
    endpoint,
    model: "deepseek-v4.1-flash",
    apiKey: "test-key",
    maxInputChars: 10_000,
    maxOutputChars: 100,
    timeoutMs: 1_000,
    ...overrides,
  };
}

function runner(localEndpoint: string, overrides: Partial<ProviderRunnerConfig> = {}) {
  return new ProviderRunner(config("https://opencode.ai/zen/go/v1", overrides), (url, init) =>
    fetch(`${localEndpoint}${new URL(String(url)).pathname}`, init));
}

const request = {
  prompt: "Summarize the note.",
  sources: [source],
};

describe("hosted provider runner prototype", () => {
  it("sends a tool-free request and returns text", async () => {
    const server = await startServer(() => ({ status: 200, body: { choices: [{ message: { content: "answer" } }] } }));
    try {
      const result = await runner(server.url).run(request);
      expect(result).toEqual({ status: "completed", output: "answer" });
      expect(server.requests).toHaveLength(1);
      const sent = server.requests[0]!;
      expect(sent.body).not.toBeNull();
      expect(sent.body).not.toHaveProperty("tools");
      expect(sent.body).not.toHaveProperty("tool_choice");
      expect(sent.body?.model).toBe("deepseek-v4.1-flash");
      expect(sent.body?.max_tokens).toBe(1_024);
      expect(JSON.stringify(sent.body?.messages)).toContain("UNTRUSTED SOURCE TEXT");
      expect(sent.headers.authorization).toBe("Bearer test-key");
      expect(sent.headers["user-agent"]).toBe("opencode/1.0.0");
      expect(sent.headers["x-opencode-session"]).toMatch(/^knowledge-/);
    } finally {
      await server.close();
    }
  });

  it("uses a caller-supplied routing session id", async () => {
    const server = await startServer(() => ({ status: 200, body: { choices: [{ message: { content: "answer" } }] } }));
    try {
      await runner(server.url).run({ ...request, sessionId: "sess_1" });
      expect(server.requests[0]?.headers["x-opencode-session"]).toBe("sess_1");
    } finally {
      await server.close();
    }
  });

  it("rejects a tool call in the response and never exposes the key or source", async () => {
    const server = await startServer(() => ({
      status: 200,
      body: { choices: [{ message: { content: null, tool_calls: [{ id: "call_1", function: { name: "shell" } }] } }] },
    }));
    try {
      const result = await runner(server.url).run(request);
      expect(result.status).toBe("failed");
      expect(result.status === "failed" ? result.error.code : null).toBe("invalid_output");
      const serialized = JSON.stringify(result);
      expect(serialized).not.toContain("test-key");
      expect(serialized).not.toContain("UNTRUSTED SOURCE TEXT");
    } finally {
      await server.close();
    }
  });

  it("rejects non-text and oversized output", async () => {
    const nonText = await startServer(() => ({ status: 200, body: { choices: [{ message: { content: 42 } }] } }));
    try {
      const result = await runner(nonText.url).run(request);
      expect(result.status === "failed" ? result.error.code : null).toBe("invalid_output");
    } finally {
      await nonText.close();
    }

    const oversized = await startServer(() => ({ status: 200, body: { choices: [{ message: { content: "x".repeat(101) } }] } }));
    try {
      const result = await runner(oversized.url).run(request);
      expect(result.status === "failed" ? result.error.code : null).toBe("output_limit_exceeded");
    } finally {
      await oversized.close();
    }
  });

  it("maps provider HTTP errors to bounded failures", async () => {
    const rateLimited = await startServer(() => ({ status: 429, body: { error: "rate limited" } }));
    try {
      const result = await runner(rateLimited.url).run(request);
      expect(result.status === "failed" ? result.error.code : null).toBe("provider_error");
      expect(result.status === "failed" ? result.error.retryable : null).toBe(true);
    } finally {
      await rateLimited.close();
    }

    const badRequest = await startServer(() => ({ status: 400, body: { error: "bad request" } }));
    try {
      const result = await runner(badRequest.url).run(request);
      expect(result.status === "failed" ? result.error.code : null).toBe("provider_error");
      expect(result.status === "failed" ? result.error.retryable : null).toBe(false);
    } finally {
      await badRequest.close();
    }
  });

  it("times out and cancels without retrying", async () => {
    const slow = await startServer(async () => {
      await new Promise((resolve) => setTimeout(resolve, 200));
      return { status: 200, body: { choices: [{ message: { content: "late" } }] } };
    });
    try {
      const result = await runner(slow.url, { timeoutMs: 20 }).run(request);
      expect(result.status === "failed" ? result.error.code : null).toBe("timeout");
      expect(slow.requests).toHaveLength(1);
    } finally {
      await slow.close();
    }

    const cancellable = await startServer(async () => {
      await new Promise((resolve) => setTimeout(resolve, 200));
      return { status: 200, body: { choices: [{ message: { content: "late" } }] } };
    });
    try {
      const controller = new AbortController();
      const pending = runner(cancellable.url, { timeoutMs: 1_000 })
        .run({ ...request, signal: controller.signal });
      await cancellable.started;
      controller.abort();
      const result = await pending;
      expect(result.status === "failed" ? result.error.code : null).toBe("cancelled");
      expect(cancellable.requests).toHaveLength(1);
    } finally {
      await cancellable.close();
    }
  });

  it("rejects input over the bound before contacting the provider", async () => {
    const server = await startServer(() => ({ status: 200, body: { choices: [{ message: { content: "answer" } }] } }));
    try {
      const result = await runner(server.url, { maxInputChars: 5 }).run(request);
      expect(result.status === "failed" ? result.error.code : null).toBe("input_limit_exceeded");
      expect(server.requests).toHaveLength(0);
    } finally {
      await server.close();
    }
  });

  it("rejects untrusted endpoints before sending credentials or source text", async () => {
    for (const endpoint of [
      "http://opencode.ai/zen/go/v1", "https://example.com/zen/go/v1",
      "https://opencode.ai.evil.test/zen/go/v1", "https://opencode.ai:8443/zen/go/v1",
      "https://user:password@opencode.ai/zen/go/v1", "https://opencode.ai/other",
      "https://opencode.ai/zen/go/v1?target=other", "https://opencode.ai/zen/go/v1#other", "not a URL",
    ]) {
      let calls = 0;
      const result = await new ProviderRunner(config(endpoint), async () => {
        calls++;
        throw new Error("must not send");
      }).run(request);
      expect(result).toMatchObject({ status: "failed", error: { code: "not_configured", retryable: false } });
      expect(calls).toBe(0);
    }
  });

  it("does not follow redirects to another server", async () => {
    const target = await startServer(() => ({ status: 200, body: { choices: [{ message: { content: "leaked" } }] } }));
    const redirect = await startServer(() => ({ status: 307, headers: { Location: target.url }, body: {} }));
    try {
      const result = await runner(redirect.url).run(request);
      expect(result).toMatchObject({ status: "failed", error: { code: "provider_error", retryable: false } });
      expect(redirect.requests).toHaveLength(1);
      expect(target.requests).toHaveLength(0);
    } finally {
      await redirect.close();
      await target.close();
    }
  });

  it("bounds the response envelope before parsing JSON", async () => {
    const server = await startServer(() => ({ status: 200, body: { padding: "x".repeat(100_000), choices: [{ message: { content: "small" } }] } }));
    try {
      const result = await runner(server.url).run(request);
      expect(result).toMatchObject({ status: "failed", error: { code: "response_limit_exceeded", retryable: false } });
    } finally {
      await server.close();
    }
  });

  it("cancels chunked response reading as soon as the byte cap is exceeded", async () => {
    let pulls = 0;
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls++;
        controller.enqueue(new Uint8Array(8_192));
      },
      cancel() { cancelled = true; },
    });
    // React Native ambient types omit Node fetch's supported Web Stream body.
    const transport: typeof fetch = async () => new Response(stream as unknown as ConstructorParameters<typeof Response>[0]);
    const result = await new ProviderRunner(config("https://opencode.ai/zen/go/v1"), transport).run(request);
    expect(result).toMatchObject({ status: "failed", error: { code: "response_limit_exceeded" } });
    expect(cancelled).toBe(true);
    expect(pulls).toBeLessThanOrEqual(10);
  });

  it("cancels an advertised oversized body without consuming it", async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } });
    const result = await new ProviderRunner(config("https://opencode.ai/zen/go/v1"), async () =>
      new Response(stream as unknown as ConstructorParameters<typeof Response>[0], { headers: { "content-length": "65537" } })).run(request);
    expect(result).toMatchObject({ status: "failed", error: { code: "response_limit_exceeded" } });
    expect(cancelled).toBe(true);
  });

  it("keeps the deadline active while the response body is stalled", async () => {
    const server = await startServer(() => ({ status: 200, bodyDelayMs: 200, body: { choices: [{ message: { content: "late" } }] } }));
    try {
      const result = await runner(server.url, { timeoutMs: 30 }).run(request);
      expect(result).toMatchObject({ status: "failed", error: { code: "timeout" } });
      expect(server.requests).toHaveLength(1);
    } finally {
      await server.close();
    }
  });

  it("never accepts a completion after cancellation or a late transport result", async () => {
    const makeResponse = () => new Response(JSON.stringify({ choices: [{ message: { content: "late" } }] }));
    const controller = new AbortController();
    const cancelled = new ProviderRunner(config("https://opencode.ai/zen/go/v1"), async () => {
      controller.abort();
      return makeResponse();
    });
    expect(await cancelled.run({ ...request, signal: controller.signal }))
      .toMatchObject({ status: "failed", error: { code: "cancelled" } });
    const late = new ProviderRunner(config("https://opencode.ai/zen/go/v1", { timeoutMs: 5 }), async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
      return makeResponse();
    });
    expect(await late.run(request)).toMatchObject({ status: "failed", error: { code: "timeout" } });
  });

  it("does not send a pre-aborted request", async () => {
    let calls = 0;
    const controller = new AbortController();
    controller.abort();
    const result = await new ProviderRunner(config("https://opencode.ai/zen/go/v1"), async () => {
      calls++;
      throw new Error("must not send");
    }).run({ ...request, signal: controller.signal });
    expect(result).toMatchObject({ status: "failed", error: { code: "cancelled" } });
    expect(calls).toBe(0);
  });

  it("bounds UTF-8 request bytes as well as character counts", async () => {
    const server = await startServer(() => ({ status: 200, body: {} }));
    try {
      const result = await runner(server.url, { maxInputChars: 65_536 }).run({ prompt: "short", sources: [{ path: "synthetic.md", text: "界".repeat(23_000) }] });
      expect(result).toMatchObject({ status: "failed", error: { code: "input_limit_exceeded" } });
      expect(server.requests).toHaveLength(0);
    } finally {
      await server.close();
    }
  });

  it("rejects malformed, blank, truncated, and legacy tool-call responses", async () => {
    for (const body of [
      null, { choices: "invalid" }, { choices: [{ message: { content: "   " } }] },
      { choices: [{ message: { content: "partial" }, finish_reason: "length" }] },
      { choices: [{ message: { content: "text", function_call: { name: "shell" } } }] },
    ]) {
      const server = await startServer(() => ({ status: 200, body }));
      try {
        const result = await runner(server.url).run(request);
        expect(result).toMatchObject({ status: "failed", error: { code: "invalid_output", retryable: false } });
      } finally {
        await server.close();
      }
    }
    const malformed = await startServer(() => ({ status: 200, body: {}, rawBody: "not JSON" }));
    try {
      expect(await runner(malformed.url).run(request)).toMatchObject({ status: "failed", error: { code: "invalid_output", retryable: false } });
    } finally {
      await malformed.close();
    }
  });

  it("counts source paths and payload framing in the input budget", async () => {
    const server = await startServer(() => ({ status: 200, body: {} }));
    try {
      const oversizedPath = await runner(server.url, { maxInputChars: 500 }).run({ prompt: "short", sources: [{ path: "x".repeat(501), text: "short" }] });
      expect(oversizedPath).toMatchObject({ status: "failed", error: { code: "input_limit_exceeded" } });
      const withinRawBudget = { prompt: "short", sources: [{ path: "synthetic.md", text: "short" }] };
      expect(withinRawBudget.prompt.length + withinRawBudget.sources[0]!.path.length + withinRawBudget.sources[0]!.text.length).toBeLessThan(100);
      const framed = await runner(server.url, { maxInputChars: 100 }).run(withinRawBudget);
      expect(framed).toMatchObject({ status: "failed", error: { code: "input_limit_exceeded" } });
      expect(server.requests).toHaveLength(0);
    } finally {
      await server.close();
    }
  });

  it("fails closed on invalid resource limits", async () => {
    for (const overrides of [{ maxInputChars: NaN }, { maxOutputChars: Infinity }, { timeoutMs: 0 }, { maxTokens: -1 }]) {
      const server = await startServer(() => ({ status: 200, body: {} }));
      try {
        expect(await runner(server.url, overrides).run(request)).toMatchObject({ status: "failed", error: { code: "not_configured" } });
        expect(server.requests).toHaveLength(0);
      } finally {
        await server.close();
      }
    }
  });

  it("fails closed without a key", async () => {
    const result = await new ProviderRunner(config("http://127.0.0.1:9", { apiKey: "" })).run(request);
    expect(result.status === "failed" ? result.error.code : null).toBe("not_configured");
  });
});

describe("OpenCode credential loading", () => {
  const now = 1_000_000_000_000;

  it("prefers a valid OAuth token, then falls back to the Go API key", () => {
    const oauthRaw = JSON.stringify([
      { integrationID: "opencode-go", value: { type: "key", key: "expired-go-key" } },
      { integrationID: "opencode", value: { type: "oauth", access: "access-token", expires: now + 3_600_000 } },
    ]);
    expect(parseOpenCodeCredential(oauthRaw, now)).toEqual({
      token: "access-token",
      kind: "oauth",
      expiresAt: now + 3_600_000,
    });

    const expiredOAuth = JSON.stringify([
      { integrationID: "opencode-go", value: { type: "key", key: "go-key" } },
      { integrationID: "opencode", value: { type: "oauth", access: "old-token", expires: now - 1 } },
    ]);
    expect(parseOpenCodeCredential(expiredOAuth, now)).toEqual({ token: "go-key", kind: "api_key" });

    const onlyExpired = JSON.stringify([
      { integrationID: "opencode", value: { type: "oauth", access: "old-token", expires: now - 1 } },
    ]);
    expect(parseOpenCodeCredential(onlyExpired, now)).toBeNull();
    expect(parseOpenCodeCredential("not json", now)).toBeNull();
  });

  it("prefers OPENCODE_API_KEY and falls back to auth export", async () => {
    await expect(loadOpenCodeCredential({
      env: { OPENCODE_API_KEY: "env-key" },
      exportAuth: async () => { throw new Error("must not be called"); },
    })).resolves.toEqual({ token: "env-key", kind: "api_key" });

    await expect(loadOpenCodeCredential({
      env: {},
      now,
      exportAuth: async () => JSON.stringify([
        { integrationID: "opencode", value: { type: "oauth", access: "export-token", expires: now + 3_600_000 } },
      ]),
    })).resolves.toEqual({ token: "export-token", kind: "oauth", expiresAt: now + 3_600_000 });

    await expect(loadOpenCodeCredential({
      env: {},
      exportAuth: async () => { throw new Error("not logged in"); },
    })).resolves.toBeNull();
  });
});
