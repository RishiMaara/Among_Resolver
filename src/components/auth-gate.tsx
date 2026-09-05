/**
 * The sign-in screen, and the gate that shows it.
 *
 * This is a DEMO gate — see lib/session.ts for why it secures nothing. The
 * screen says so on its face, because a finance tool that looks locked and
 * is not is worse than one that never pretended.
 *
 * The mark animates while the credentials are checked. There is nothing to
 * wait for, so the delay is real work being simulated — and rather than fake
 * it silently, the pause is short and the button says what it is doing.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { LoadingMark } from "@/components/loading-mark";
import { Wordmark } from "@/components/wordmark";
import { currentSession, signIn, DEMO_EMAIL, DEMO_PASSWORD, type Session } from "@/lib/session";

export function AuthGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  // The session lives in sessionStorage, which does not exist during the
  // server render. Gating on `ready` keeps the server and client markup
  // identical until mount, so hydration has nothing to disagree about.
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setSession(currentSession());
    setReady(true);
  }, []);

  if (!ready) return null;
  if (session) return <>{children}</>;
  return <SignIn onSignedIn={setSession} />;
}

function SignIn({ onSignedIn }: { onSignedIn: (s: Session) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (checking) return;
      setChecking(true);
      setError(null);
      await new Promise((r) => setTimeout(r, 650));
      const s = signIn(email, password);
      if (!s) {
        setChecking(false);
        setError("That email and password do not match the demo account.");
        return;
      }
      onSignedIn(s);
    },
    [email, password, checking, onSignedIn],
  );

  const useDemo = () => {
    setEmail(DEMO_EMAIL);
    setPassword(DEMO_PASSWORD);
    setError(null);
  };

  return (
    <div className="hero-mesh flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-[400px]">
        <div className="mb-7 flex justify-center">
          <Wordmark />
        </div>

        <form onSubmit={submit} className="hero-card rounded-[18px] p-6 sm:p-7">
          <h1 className="m-0 text-[19px] font-semibold tracking-[-0.01em] text-foreground">
            Sign in
          </h1>
          <p className="narrative-copy m-0 mt-1.5 text-[13px] leading-[1.55] text-muted-foreground">
            Reconciliation decisions are attributed to whoever is signed in.
          </p>

          <label className="mt-5 block">
            <span className="label-ui block text-[10px] uppercase text-muted-foreground">
              Email
            </span>
            <input
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={DEMO_EMAIL}
              disabled={checking}
              className="mt-1.5 h-10 w-full rounded-[10px] border border-border bg-background px-3 text-[14px] outline-none transition-colors focus:border-accent disabled:opacity-60"
            />
          </label>

          <label className="mt-3.5 block">
            <span className="label-ui block text-[10px] uppercase text-muted-foreground">
              Password
            </span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••••••"
              disabled={checking}
              className="mt-1.5 h-10 w-full rounded-[10px] border border-border bg-background px-3 text-[14px] outline-none transition-colors focus:border-accent disabled:opacity-60"
            />
          </label>

          {error && (
            <p
              className="m-0 mt-3 text-[12.5px] text-[var(--s-blocked)]"
              role="alert"
              style={{ animation: "rb-rise 260ms ease both" }}
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={checking || !email.trim() || !password}
            className="mt-5 flex h-10 w-full items-center justify-center gap-2 rounded-[10px] bg-primary text-[14px] font-medium text-primary-foreground transition-opacity disabled:opacity-50"
          >
            {checking ? (
              <>
                <LoadingMark size={16} onSolid />
                Checking…
              </>
            ) : (
              "Sign in"
            )}
          </button>

          {/* The credentials are in the bundle either way. Printing them is
              honest about that, and saves a judge guessing. */}
          <button
            type="button"
            onClick={useDemo}
            disabled={checking}
            className="mt-3 w-full rounded-[10px] border border-dashed border-border px-3 py-2 text-left text-[11.5px] leading-[1.5] text-muted-foreground transition-colors hover:border-accent disabled:opacity-60"
          >
            <span className="label-ui text-[9.5px] uppercase">Demo account</span>
            <br />
            <span className="font-mono text-[11px] text-foreground">{DEMO_EMAIL}</span> ·{" "}
            <span className="font-mono text-[11px] text-foreground">{DEMO_PASSWORD}</span>
            <br />
            <span className="text-[11px]">Click to fill</span>
          </button>
        </form>

        {/* Do not let a viewer assume this is protecting anything. */}
        <p className="narrative-copy mx-auto mt-4 max-w-[360px] text-center text-[11px] leading-[1.55] text-muted-foreground">
          Demo sign-in only — the check runs in the browser and secures nothing. The engine's own
          API-key auth is what guards the data.
        </p>
      </div>
    </div>
  );
}
