import {constants} from "node:fs";
import {lstat, mkdir, open, realpath} from "node:fs/promises";
import {dirname, isAbsolute, join} from "node:path";

/** Counts reserved attempts, not successful responses; unknown outcomes are never refunded. */
export class HostedRequestBudget {
  public constructor(private readonly directory: string, private readonly limit: number, private readonly now: () => number = Date.now) {
    if (!isAbsolute(directory) || !Number.isSafeInteger(limit) || limit < 1 || limit > 100) throw new Error("Invalid hosted budget");
  }
  private async syncChain(): Promise<void> {
    let current = await realpath(this.directory);
    for (;;) {
      const handle = await open(current, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try { await handle.sync(); } finally { await handle.close(); }
      const parent = dirname(current); if (parent === current) return; current = parent;
    }
  }
  public async take(): Promise<boolean> {
    const time = this.now(); if (!Number.isSafeInteger(time) || time < 0) throw new Error("Invalid hosted budget time");
    const day = new Date(time).toISOString().slice(0, 10);
    await mkdir(this.directory, {recursive: true, mode: 0o700});
    const info = await lstat(this.directory);
    if (!info.isDirectory() || (info.mode & 0o777) !== 0o700 || info.uid !== process.getuid?.()) throw new Error("Unsafe hosted budget");
    for (let slot = 0; slot < this.limit; slot++) {
      let handle;
      try { handle = await open(join(this.directory, `${day}-${slot}.json`), "wx", 0o600); }
      catch (error) { if ((error as NodeJS.ErrnoException).code === "EEXIST") continue; throw error; }
      try { await handle.writeFile(JSON.stringify({version: 1, day, slot, createdAt: time})); await handle.sync(); }
      finally { await handle.close(); }
      await this.syncChain();
      return true;
    }
    return false;
  }
}
