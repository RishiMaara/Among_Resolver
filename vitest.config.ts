/**
 * Frontend tests.
 *
 * There were none, in a project where every UI defect found so far would have
 * been caught by one: a rejection rendering as "[object Object]", a cash panel
 * claiming "confirmed" on a settlement the engine had refused, compliance
 * fields dropped before they reached the screen, an approved posting still
 * reading "awaiting approval". 228 backend tests against zero here was the
 * clearest imbalance in the repository.
 *
 * A separate config rather than a `test` block in vite.config.ts, because that
 * config comes from @lovable.dev/vite-tanstack-config and re-declaring its
 * plugins to add one field risks changing the build. This only has to resolve
 * the same "@" alias and run in a DOM.
 */

import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "happy-dom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // The engine is not running during a unit test and must never be reached
    // from one; every network call is stubbed per-test.
    restoreMocks: true,
  },
});
