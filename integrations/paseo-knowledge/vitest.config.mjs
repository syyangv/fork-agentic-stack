import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const integrationRoot = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: integrationRoot,
  test: {
    include: ["tests/**/*.test.ts"],
    environment: "node",
  },
});
