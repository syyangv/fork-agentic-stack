import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";
import { KnowledgeCliRunner } from "../server/core.js";
import { digest } from "../server/hosted-task-policy.js";
it("aborts only its owned provenance reader and never starts a pre-aborted reader", async () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "hosted-reader-cancel-"));
  const marker = join(root, "started.json"); const script = join(root, "reader.py");
  const python = execFileSync("/usr/bin/which", ["python3"], { encoding: "utf8" }).trim();
  writeFileSync(script, `import json, os, time\nwith open(${JSON.stringify(marker + ".tmp")}, "w") as f: json.dump({"pid":os.getpid()}, f)\nos.replace(${JSON.stringify(marker + ".tmp")}, ${JSON.stringify(marker)})\ntime.sleep(2)\nprint(json.dumps({"success":True}))\n`);
  try {
    const runner = new KnowledgeCliRunner({ pythonExecutable: python, cliPath: script, workVaultPath: root, runtimeDir: root });
    const request = { taskId: "task_cancel", source: { source_id: "synthetic", vault_id: "work", note_id: "synthetic", path: "synthetic.md",
      line_start: 1, line_end: 1, content_hash: digest("synthetic") }, maxChars: 100 };
    const preAborted = new AbortController(); preAborted.abort();
    await expect(runner.readSource({ ...request, signal: preAborted.signal })).rejects.toThrow(/cancelled/);
    expect(existsSync(marker)).toBe(false);
    const controller = new AbortController();
    const pending = runner.readSource({ ...request, signal: controller.signal }).then(() => null, (error: unknown) => error);
    for (let attempt = 0; attempt < 100 && !existsSync(marker); attempt++) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(existsSync(marker)).toBe(true);
    expect(JSON.parse(readFileSync(marker, "utf8")).pid).toBeGreaterThan(0);
    controller.abort();
    const result = await pending;
    expect(result).toBeInstanceOf(Error);
    expect((result as Error).message).toMatch(/cancelled/);
  } finally { rmSync(root, { force: true, recursive: true }); }
}, 10_000);

it("keeps an aborted reader pending until its SIGTERM-ignoring process really closes", async () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "hosted-reader-quarantine-"));
  const marker = join(root, "started"); const release = join(root, "release"); const script = join(root, "reader.py");
  const python = execFileSync("/usr/bin/which", ["python3"], { encoding: "utf8" }).trim();
  writeFileSync(script, `import json, os, time, signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwith open(${JSON.stringify(marker + ".tmp")}, "w") as f: f.write("ready")\nos.replace(${JSON.stringify(marker + ".tmp")}, ${JSON.stringify(marker)})\nend=time.monotonic()+2\nwhile time.monotonic()<end and not os.path.exists(${JSON.stringify(release)}): time.sleep(0.01)\nprint(json.dumps({"success":True}))\n`);
  try {
    const runner = new KnowledgeCliRunner({ pythonExecutable: python, cliPath: script, workVaultPath: root, runtimeDir: root });
    const controller = new AbortController(); let settled = false;
    const pending = runner.readSource({ taskId: "task_quarantine", source: { source_id: "synthetic", vault_id: "work", note_id: "synthetic", path: "synthetic.md",
      line_start: 1, line_end: 1, content_hash: digest("synthetic") }, maxChars: 100, signal: controller.signal })
      .then(() => { settled = true; return null; }, (error: unknown) => { settled = true; return error; });
    for (let attempt = 0; attempt < 100 && !existsSync(marker); attempt++) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(existsSync(marker)).toBe(true); controller.abort();
    await new Promise((resolve) => setTimeout(resolve, 30)); expect(settled).toBe(false);
    writeFileSync(release, "finish");
    const result = await pending; expect(result).toBeInstanceOf(Error); expect((result as Error).message).toMatch(/cancelled/);
  } finally { rmSync(root, { force: true, recursive: true }); }
}, 10_000);
