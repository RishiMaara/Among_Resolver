/**
 * Shared test setup.
 *
 * sessionStorage and clipboard are real browser APIs the components lean on,
 * and happy-dom does not provide all of them. Stubbing them here rather than
 * in each test keeps the tests about behaviour instead of plumbing.
 */

import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

// Components call navigator.clipboard when a link is blocked; happy-dom has no
// implementation, and an unhandled rejection there would fail an unrelated
// assertion.
Object.defineProperty(navigator, "clipboard", {
  value: { writeText: vi.fn().mockResolvedValue(undefined) },
  configurable: true,
});
