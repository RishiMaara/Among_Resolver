/**
 * The demo sign-in gate.
 *
 * READ THIS BEFORE TRUSTING IT: this is NOT authentication. The credentials
 * are in the client bundle, the check runs in the browser, and the "session"
 * is a key in sessionStorage. Anyone can open devtools and set that key, or
 * read the password out of the JavaScript. It stops nobody.
 *
 * It exists so a demo opens on a sign-in screen rather than straight into a
 * finance tool, and so the reconciliation screens have a name to attribute a
 * decision to. That is the whole scope, and the screen says so on its face
 * rather than letting a viewer assume otherwise.
 *
 * Real auth is the engine's job, not the browser's: engine/src/auth.py already
 * checks an API key with hmac.compare_digest on every non-public route. Per-
 * user accounts would mean sessions and a user table behind that, which is a
 * backend change — not a login form.
 */

const KEY = "among.session";

/** Demo credentials. Deliberately obvious: these are not a secret. */
export const DEMO_EMAIL = "tester@amongresolver.app";
export const DEMO_PASSWORD = "reconcile2026";

export interface Session {
  email: string;
  signedInAt: string;
}

export function currentSession(): Session | null {
  if (typeof window === "undefined") return null; // SSR pass
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    // Private mode, or storage disabled. Treat as signed out rather than
    // crashing the app on a storage read.
    return null;
  }
}

export function signIn(email: string, password: string): Session | null {
  const ok = email.trim().toLowerCase() === DEMO_EMAIL && password === DEMO_PASSWORD;
  if (!ok) return null;
  const session: Session = {
    email: DEMO_EMAIL,
    signedInAt: new Date().toISOString(),
  };
  try {
    sessionStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    /* Storage refused. The session still holds for this page's lifetime. */
  }
  return session;
}

export function signOut() {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* nothing to clear */
  }
}
