/**
 * The judge page's figures must be the committed measurements, not copies.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { MEASURED, format } from "./measured";

const BENCH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "engine",
  "docs",
  "benchmarks",
);

function pick(obj: unknown, dotted: string): number {
  let cur: unknown = obj;
  for (const key of dotted.split(".")) {
    cur = (cur as Record<string, unknown>)[key];
  }
  if (typeof cur !== "number") throw new Error(`${dotted} is not a number`);
  return cur;
}

describe("measured figures", () => {
  for (const m of MEASURED) {
    it(`${m.label} matches ${m.source.file}`, () => {
      const data = JSON.parse(readFileSync(path.join(BENCH, m.source.file), "utf-8"));
      const per = m.source.perThousandOf ? pick(data, m.source.perThousandOf) / 1000 : 1;
      expect(format(pick(data, m.source.path) / per, m.source)).toBe(m.after);
      if (m.source.beforePath) {
        expect(format(pick(data, m.source.beforePath), m.source)).toBe(m.before);
      }
    });
  }

  it("prints percentages with their sign", () => {
    expect(format(0.8271, { file: "", path: "x", scale: 100, digits: 2 })).toBe("82.71%");
    expect(format(65, { file: "", path: "a_pct", digits: 0 })).toBe("65%");
  });
});
