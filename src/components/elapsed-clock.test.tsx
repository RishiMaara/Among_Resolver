/**
 * The run's clock: it ticks while a run is going, stops where the run ended,
 * and ticking re-renders the clock alone, not the page holding it.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { ElapsedClock, type RunClock } from "@/components/elapsed-clock";

let now = 0;

function Page({ clock, renders }: { clock: RunClock; renders: { count: number } }) {
  renders.count += 1;
  return (
    <p data-testid="clock">
      <ElapsedClock clock={clock} />
    </p>
  );
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ElapsedClock", () => {
  it("ticks while running without re-rendering the page, and stops where the run ended", () => {
    vi.useFakeTimers();
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const renders = { count: 0 };
    now = 1000;
    const { rerender } = render(<Page clock={{ start: 1000, end: null }} renders={renders} />);
    expect(screen.getByTestId("clock")).toHaveTextContent("0ms");

    now = 1450;
    act(() => vi.advanceTimersByTime(100));
    expect(screen.getByTestId("clock")).toHaveTextContent("450ms");
    now = 3210;
    act(() => vi.advanceTimersByTime(100));
    expect(screen.getByTestId("clock")).toHaveTextContent("2.21s");
    // The page rendered once; only the clock re-rendered as it ticked.
    expect(renders.count).toBe(1);

    rerender(<Page clock={{ start: 1000, end: 3500 }} renders={renders} />);
    expect(screen.getByTestId("clock")).toHaveTextContent("2.50s");
    now = 9000;
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByTestId("clock")).toHaveTextContent("2.50s");
  });

  it("reads zero before a run", () => {
    render(<ElapsedClock clock={null} />);
    expect(screen.getByText("0ms")).toBeInTheDocument();
  });
});
