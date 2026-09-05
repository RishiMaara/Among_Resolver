/**
 * The error message a person actually sees.
 *
 * This exists because of a real defect: the engine returns a rejection as a
 * structured object in `detail`, and index.tsx did `new Error(err.detail)` on
 * it — throwing an object into Error's constructor, which stringifies to
 * "[object Object]". So the one moment a user most needed to know what went
 * wrong showed them nothing at all. queue.tsx handled the object but reached
 * for `.message`, the engineer's text, over the plain-language explanation
 * sitting beside it.
 */

import { describe, it, expect } from "vitest";
import { engineErrorMessage } from "@/lib/api";

describe("engineErrorMessage", () => {
  it("never returns [object Object] for a structured rejection", () => {
    const body = {
      detail: {
        plain: "We could not read this file.\n\nWHAT IS MISSING\n  - the payment amount",
        message: "Cannot process 'x.csv'. Agent 0 rejected it...",
        rejected: true,
      },
    };
    const out = engineErrorMessage(body, "fallback");
    expect(out).not.toContain("[object Object]");
    expect(out).toContain("We could not read this file.");
  });

  it("prefers the plain explanation over the engineer's message", () => {
    const body = { detail: { plain: "PLAIN TEXT", message: "TECHNICAL TEXT" } };
    expect(engineErrorMessage(body, "fallback")).toBe("PLAIN TEXT");
  });

  it("falls back to the technical message when there is no plain one", () => {
    const body = { detail: { message: "TECHNICAL TEXT" } };
    expect(engineErrorMessage(body, "fallback")).toBe("TECHNICAL TEXT");
  });

  it("passes a plain string detail straight through", () => {
    expect(engineErrorMessage({ detail: "just a string" }, "fb")).toBe("just a string");
  });

  it("uses the fallback for an empty, null or shapeless body", () => {
    expect(engineErrorMessage(null, "fb")).toBe("fb");
    expect(engineErrorMessage({}, "fb")).toBe("fb");
    expect(engineErrorMessage({ detail: {} }, "fb")).toBe("fb");
    expect(engineErrorMessage({ detail: "   " }, "fb")).toBe("fb");
  });
});

describe("FastAPI's own validation errors", () => {
  /**
   * These arrive as an ARRAY of {loc, msg, type}, not as this engine's
   * {plain, message} object. Nothing handled that shape, so leaving the
   * amount blank produced a 422 the screen rendered as a bare "FAILED · 0ms ·
   * 0/12 agents" with no reason on it — an unexplained failure, on the most
   * ordinary mistake there is.
   */
  it("names the field a person left empty", () => {
    const body = {
      detail: [
        {
          type: "float_parsing",
          loc: ["body", "net_amount"],
          msg: "Input should be a valid number",
          input: "",
        },
      ],
    };
    const out = engineErrorMessage(body, "fallback");
    expect(out).toContain("the settlement amount");
    expect(out).not.toBe("fallback");
  });

  it("lists several missing fields readably", () => {
    const body = {
      detail: [
        { loc: ["body", "net_amount"], msg: "Field required" },
        { loc: ["body", "settled_at"], msg: "Field required" },
      ],
    };
    const out = engineErrorMessage(body, "fallback");
    expect(out).toContain("the settlement amount");
    expect(out).toContain("the settlement date");
    expect(out).toContain(" and ");
  });

  it("falls back rather than printing a field name nobody recognises", () => {
    const body = { detail: [{ loc: [], msg: "Field required" }] };
    expect(engineErrorMessage(body, "fallback")).toBe("fallback");
  });
});
