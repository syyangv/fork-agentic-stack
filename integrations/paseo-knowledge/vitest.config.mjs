import { defineConfig } from "/Users/syang/projects/paseo/node_modules/vitest/dist/config.js";

export default defineConfig({
  resolve: {
    alias: {
      "@getpaseo/plugin/server": "/Users/syang/projects/paseo/packages/plugin/src/server.ts",
      "@getpaseo/plugin": "/Users/syang/projects/paseo/packages/plugin/src/index.ts",
      "@getpaseo/client": "/Users/syang/projects/paseo/packages/client/src/index.ts",
      "zod": "/Users/syang/projects/paseo/node_modules/zod/index.js",
      "react": "/Users/syang/projects/paseo/node_modules/react/index.js",
      "react/jsx-runtime": "/Users/syang/projects/paseo/node_modules/react/jsx-runtime.js",
      "react-native": "/Users/syang/projects/paseo/node_modules/react-native/index.js",
      "use-sync-external-store/shim/with-selector": "/Users/syang/projects/paseo/node_modules/use-sync-external-store/shim/with-selector.js",
    },
  },
  test: {
    include: ["integrations/paseo-knowledge/tests/**/*.test.ts"],
    environment: "node",
  },
});
