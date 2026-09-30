/**
 * The bar at the top of every screen, and the title under it, in one place.
 *
 * Six screens carried six hand-copied headers, and they had drifted: the links
 * came in a different order on each, the rulebook was "Compliance Rulebook" on
 * one screen and "Rulebook" on the rest, the judges' brief had no icons, and
 * the rulebook's bar was taller, a different colour and a different width, so
 * the logo moved when a visitor changed screen. Titles came in two unrelated
 * styles. Every screen now gets the same bar, with the links in the same
 * order; the current screen is named beside the logo and left out of the links.
 */

import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Flag, Gavel, Network, ScrollText } from "lucide-react";
import { HistoryLink } from "@/components/history-link";
import { PayoutsLink } from "@/components/payouts-link";
import { SessionMenu } from "@/components/session-menu";
import { ThemeToggle } from "@/components/theme-toggle";
import { Wordmark } from "@/components/wordmark";

export type Screen =
  "Agent Flow" | "History" | "Escalations" | "Rulebook" | "Payouts" | "Judges' brief";

const NAV =
  "flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground";

export function SiteHeader({ current }: { current: Screen }) {
  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur-md">
      <div className="mx-auto flex max-w-[1180px] items-center justify-between gap-3 px-4 py-2.5 sm:gap-4 sm:px-6">
        <Wordmark pill={current} briefOnPhones={false} />
        <div className="flex items-center gap-2.5 sm:gap-3.5">
          {current !== "Judges' brief" && (
            <Link to="/judge" aria-label="Judges' brief" className={`sm:hidden ${NAV}`}>
              <Gavel className="size-4" />
            </Link>
          )}
          <nav aria-label="Screens" className="flex items-center gap-2.5 sm:gap-3.5">
            {current !== "Agent Flow" && (
              <Link to="/" className={NAV}>
                <Network className="size-4" />
                <span className="hidden sm:inline">Agent Flow</span>
              </Link>
            )}
            {current !== "History" && <HistoryLink className={NAV} />}
            {current !== "Escalations" && (
              <Link to="/escalations" className={`nav-flag ${NAV}`}>
                <Flag className="size-4" />
                <span className="hidden sm:inline">Escalations</span>
              </Link>
            )}
            {current !== "Rulebook" && (
              <Link to="/rulebook" className={`nav-scroll ${NAV}`}>
                <ScrollText className="size-4" />
                <span className="hidden sm:inline">Rulebook</span>
              </Link>
            )}
            {current !== "Payouts" && <PayoutsLink className={NAV} />}
          </nav>
          <SessionMenu />
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

/** A screen's eyebrow, title and opening paragraph, set the same on every screen. */
export function PageIntro({
  eyebrow,
  title,
  children,
}: {
  eyebrow: ReactNode;
  title: ReactNode;
  children?: ReactNode;
}) {
  return (
    <>
      <p className="eyebrow-display m-0 uppercase text-muted-foreground">{eyebrow}</p>
      <h1 className="m-0 mt-4 max-w-[720px] font-[family-name:var(--font-display)] text-[38px] font-normal leading-[1.05] tracking-[-0.025em]">
        {title}
      </h1>
      {children && (
        <div className="narrative-copy mt-4 max-w-[660px] text-[15.5px] leading-[1.65] text-muted-foreground">
          {children}
        </div>
      )}
    </>
  );
}
