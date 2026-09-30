/**
 * The run's elapsed time, ticking on its own.
 *
 * The clock was state on the main screen, set every 100 ms, so for the whole
 * of a run the page, the agent canvas and everything under them re-rendered
 * ten times a second to change one number. Measured with the CPU slowed 4x,
 * as on a laptop on battery, a sample run blocked the main thread for 17.8 s.
 * Here only this component re-renders; the page stores when the run started
 * and ended, which changes twice a run.
 */

import { useEffect, useState } from "react";

export type RunClock = { start: number; end: number | null } | null;

function formatElapsed(ms: number): string {
  const v = Math.max(0, ms);
  return v < 1000 ? `${Math.round(v)}ms` : `${(v / 1000).toFixed(2)}s`;
}

export function ElapsedClock({ clock }: { clock: RunClock }) {
  const [now, setNow] = useState(() => performance.now());
  const running = clock !== null && clock.end === null;
  useEffect(() => {
    if (!running) return;
    setNow(performance.now());
    const id = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(id);
  }, [running]);
  if (!clock) return <>{formatElapsed(0)}</>;
  return <>{formatElapsed((clock.end ?? now) - clock.start)}</>;
}
