/**
 * Whether a model is live on the engine this app talks to.
 *
 * Read once from GET /ai/status. The interface uses it to ask for model help
 * only where a model exists, and to say plainly which answers came from one.
 */

import { useEffect, useState } from "react";
import { engineFetch } from "@/lib/api";

export interface AiStatus {
  live: boolean;
  model: string | null;
  uses: Record<string, boolean>;
  decides_membership: boolean;
  budget: { per_day: number; used_today: number; left_today: number; per_visitor_per_hour: number };
  plain: string;
}

let pending: Promise<AiStatus | null> | null = null;

export function aiStatus(): Promise<AiStatus | null> {
  pending ??= engineFetch("ai/status")
    .then((r) => (r.ok ? (r.json() as Promise<AiStatus>) : null))
    .catch(() => null);
  return pending;
}

/** For tests: forget the cached answer. */
export function resetAiStatus() {
  pending = null;
}

export function useAiStatus(): AiStatus | null {
  const [status, setStatus] = useState<AiStatus | null>(null);
  useEffect(() => {
    let live = true;
    void aiStatus().then((s) => {
      if (live) setStatus(s);
    });
    return () => {
      live = false;
    };
  }, []);
  return status;
}
