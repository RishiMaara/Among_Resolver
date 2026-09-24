/**
 * Where the reconciliation engine lives.
 *
 * This constant was declared independently in three files — index.tsx,
 * rulebook.tsx and results-panel.tsx — each with its own copy of the same
 * parsing and the same default. Three copies of a base URL is three places to
 * miss when the deployment target changes, and one of them had the fallback
 * host written into user-facing text as well.
 *
 * Set VITE_ENGINE_URL at build time to point somewhere else. The localhost
 * default is kept because it is what `npm run dev` alongside the engine
 * actually needs, and a default that works for the common case is worth more
 * than one that forces configuration before anything runs.
 */
const RAW = (import.meta.env["VITE_ENGINE_URL"] as string | undefined) ?? "http://127.0.0.1:8001";

/** No trailing slash, so callers can write `${ENGINE_URL}/audit/x` safely. */
export const ENGINE_URL = RAW.replace(/\/+$/, "");

/** Host and port only — for telling a user where the engine is expected. */
export const ENGINE_HOST = ENGINE_URL.replace(/^https?:\/\//, "");

/**
 * What one request to the hosted engine can carry. Vercel refuses a larger
 * body with 413 before the engine sees it (measured: 3.75 MB, 60,000 rows,
 * cleared in 13 s; 5.01 MB refused). A local engine takes MAX_UPLOAD_MB.
 */
export const HOSTED_UPLOAD_LIMIT_BYTES = 4.5 * 1000 * 1000;
export const ENGINE_IS_LOCAL = /^(localhost|127\.0\.0\.1)(:|$)/.test(ENGINE_HOST);

/** A size problem to show before sending, or null when the files fit. */
export function uploadTooLarge(files: (File | null | undefined)[]): string | null {
  if (ENGINE_IS_LOCAL) return null;
  const total = files.reduce((n, f) => n + (f?.size ?? 0), 0);
  if (total <= HOSTED_UPLOAD_LIMIT_BYTES) return null;
  const mb = (total / 1_000_000).toFixed(1);
  return (
    `These files come to ${mb} MB; the hosted demo accepts up to 4.5 MB in one run ` +
    `(about 60,000 rows), a limit of the hosting, not the engine. Split the export, ` +
    `or run the engine locally, where it has reconciled 1,000,000 records in one file.`
  );
}

/** The engine's error body, or a stand-in when the reply was not JSON. */
export async function errorBody(res: Response): Promise<unknown> {
  try {
    return await res.json();
  } catch {
    return {
      detail:
        res.status === 413
          ? "The upload is larger than the hosted demo accepts (4.5 MB). Split the export, or run the engine locally."
          : `The engine replied ${res.status} without an explanation.`,
    };
  }
}

/**
 * The API key, if this deployment uses one.
 *
 * Adding auth to the engine without teaching the frontend to send a key made
 * every screen fail the moment API_KEY was set — the backend was secured and
 * the app was broken, which is the worst of both. VITE_API_KEY is baked at
 * build time.
 *
 * A build-time key is visible to anyone who opens the bundle, so this is
 * appropriate for a shared-secret deployment behind a trusted boundary and
 * NOT for per-user credentials. The engine has no user model; pretending
 * otherwise here would be dishonest about what the key protects.
 */
const API_KEY = (import.meta.env["VITE_API_KEY"] as string | undefined)?.trim();

/** Headers for an engine call. Spread into fetch init. */
export function engineHeaders(extra?: HeadersInit): HeadersInit {
  const h = new Headers(extra);
  if (API_KEY) h.set("X-API-Key", API_KEY);
  return h;
}

/**
 * fetch, with the key attached.
 *
 * Every engine call goes through this so a new call site cannot forget the
 * header — the same reasoning that put auth in middleware on the server
 * rather than on each route.
 */
export function engineFetch(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(path.startsWith("http") ? path : `${ENGINE_URL}/${path.replace(/^\/+/, "")}`, {
    ...init,
    headers: engineHeaders(init.headers),
  });
}

/**
 * The message to actually show a person when an engine call fails.
 *
 * The engine returns a rejection as a structured object in `detail`, and
 * index.tsx did `new Error(err.detail || "...")` on it — throwing an object
 * into Error's constructor, which stringifies to "[object Object]". So the
 * one place a user most needs to know what went wrong showed them nothing at
 * all. queue.tsx handled the object but reached for `.message`, the engineer's
 * text, over the plain-language explanation sitting next to it.
 *
 * Order matters: the plain rejection first, then the technical message, then
 * a generic fallback. Never the raw object.
 */
export function engineErrorMessage(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  // FastAPI's own validation errors arrive as an ARRAY of {loc, msg, type},
  // not as this engine's {plain, message} object. Nothing handled that shape,
  // so a missing form field produced a 422 the screen rendered as a bare
  // "FAILED" with no reason at all — the exact failure this product exists to
  // not have. Name the field, in the words the form uses for it.
  if (Array.isArray(detail)) {
    const FIELD: Record<string, string> = {
      net_amount: "the settlement amount",
      batch_id: "the batch id",
      settled_at: "the settlement date",
      settlement_window_days: "the lookback window",
      declared_deductions: "the declared deductions",
    };
    const named = detail
      .map((e) => {
        const loc = (e as { loc?: unknown[] })?.loc ?? [];
        const field = String(loc[loc.length - 1] ?? "");
        return FIELD[field] ?? field;
      })
      .filter(Boolean);
    if (named.length) {
      return named.length === 1
        ? `Please fill in ${named[0]} before running.`
        : `Please fill in ${named.slice(0, -1).join(", ")} and ${named[named.length - 1]} before running.`;
    }
  }

  if (detail && typeof detail === "object") {
    const d = detail as { plain?: unknown; message?: unknown };
    if (typeof d.plain === "string" && d.plain.trim()) return d.plain;
    if (typeof d.message === "string" && d.message.trim()) return d.message;
  }
  return fallback;
}
