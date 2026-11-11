import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("Paseo Knowledge client contribution lifecycle", () => {
  it("returns a cleanup function after registering client surfaces", () => {
    const source = readFileSync(new URL("../index.client.tsx", import.meta.url), "utf8");

    expect(source).toContain("const cleanups = [");
    expect(source).toContain("client.addCommandCenterItem");
    expect(source).toContain("return () => {");
    expect(source).toContain("for (const cleanup of cleanups.reverse()) cleanup();");
  });
});
