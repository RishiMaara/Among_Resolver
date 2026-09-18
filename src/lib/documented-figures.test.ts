/**
 * The documented frontend test count has to be the real one.
 *
 * The backend learned this the hard way: four documents once claimed four
 * different backend test counts, and nothing caught it until an external
 * reviewer did in about a minute (see
 * engine/tests/test_documented_figures.py). This is the same guard for the
 * frontend half of that same "Tests | **335** backend · **95** frontend"
 * line in README.md — checked against what vitest itself collects, not
 * trusted by hand.
 *
 * `vitest list` only collects tests, it does not run them — the same reason
 * the backend check is safe spawning a nested `pytest --collect-only`. A
 * nested `vitest run` here would re-execute this very file and recurse.
 */

import { describe, it, expect } from "vitest";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

function collectedFrontendCount(): number {
  const out = execFileSync("npx", ["vitest", "list", "--json", "--config", "vitest.config.ts"], {
    cwd: ROOT,
    encoding: "utf-8",
    timeout: 120_000,
    // npx is a .cmd shim on Windows; CreateProcess cannot exec it directly
    // there. Matches scripts/generate_benchmarks.py's run() helper.
    shell: process.platform === "win32",
  });
  return (JSON.parse(out) as unknown[]).length;
}

describe("documented frontend test count", () => {
  // Spawning vitest as a subprocess takes several seconds — comfortably over
  // the default 5s per-test timeout, so this needs its own.
  it("matches what README.md states", () => {
    const readme = readFileSync(path.join(ROOT, "README.md"), "utf-8");
    const m = readme.match(/\*\*(\d+)\*\* frontend/);
    expect(
      m,
      'README.md no longer states a frontend test count where this looked for one (expected "**N** frontend")',
    ).not.toBeNull();

    const documented = Number(m![1]);
    const actual = collectedFrontendCount();
    expect(
      documented,
      `README.md says ${documented} frontend tests; vitest collects ${actual}. ` +
        `Update the "Tests" row in README.md's benchmark table — this is exactly ` +
        `how the backend count drifted across four documents before anything checked it.`,
    ).toBe(actual);
  }, 60_000);
});
