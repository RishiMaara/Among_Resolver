/**
 * The wordmark, in one place.
 *
 * This markup was copy-pasted into three route headers. Three copies of a
 * brand mark is three places to miss when it changes, and it had already
 * started drifting.
 *
 * The mark is a 4x4 lattice with three of its sixteen nodes lifted onto a
 * path — which is the product's argument in a graphic: the grid is every
 * candidate transaction, the line is the subset that resolves to the
 * settlement, and the nodes on it are the members that were identified. The
 * thirteen that stay grey are the ones linkage ruled out.
 *
 * Colours come from --mark-dot and --mark-ink rather than being baked into
 * the paths, so the mark follows the app's own theme switch. The standalone
 * public/logo-mark.svg carries the same two palettes in an internal <style>,
 * because no CSS variable reaches a favicon or an <img>.
 */

import { Link } from "@tanstack/react-router";
import { History, Flag, ScrollText, Network } from "lucide-react";

export function Wordmark({ pill }: { pill?: string }) {
  return (
    <div className="flex items-center gap-2.5">
      <svg
        width="30"
        height="30"
        viewBox="0 0 32 32"
        role="img"
        aria-label="AmongResolver"
        className="block flex-none"
      >
        <g fill="var(--mark-dot)">
          <circle cx="5.5" cy="5.5" r="1.55" />
          <circle cx="12.5" cy="5.5" r="1.55" />
          <circle cx="19.5" cy="5.5" r="1.55" />
          <circle cx="26.5" cy="5.5" r="1.55" />
          <circle cx="5.5" cy="12.5" r="1.55" />
          <circle cx="12.5" cy="12.5" r="1.55" />
          <circle cx="26.5" cy="12.5" r="1.55" />
          <circle cx="12.5" cy="19.5" r="1.55" />
          <circle cx="19.5" cy="19.5" r="1.55" />
          <circle cx="26.5" cy="19.5" r="1.55" />
          <circle cx="5.5" cy="26.5" r="1.55" />
          <circle cx="12.5" cy="26.5" r="1.55" />
          <circle cx="19.5" cy="26.5" r="1.55" />
        </g>
        <path
          d="M5.5 19.5L19.5 12.5L26.5 26.5"
          fill="none"
          stroke="var(--mark-ink)"
          strokeWidth="2.3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <circle cx="5.5" cy="19.5" r="2.7" fill="var(--mark-ink)" />
        <circle cx="19.5" cy="12.5" r="2.7" fill="var(--mark-ink)" />
        <circle cx="26.5" cy="26.5" r="2.7" fill="var(--mark-ink)" />
      </svg>

      <Link to="/" className="leading-none no-underline">
        {/* Two weights, one word. The supplied lockup sets "Among" light and
            "Resolver" heavy, which is what makes the compound read as a name
            rather than as two words that happen to be adjacent. */}
        <span className="brand-name text-[17px] text-foreground">
          <span className="brand-name-light">Among</span>Resolver
        </span>
      </Link>

      {pill && (
        <>
          <div className="hidden h-4 w-px bg-border md:block" />
          <div className="hidden md:flex items-center gap-1.5 font-semibold text-[17px] text-foreground tracking-tight">
            {pill === "History" && <History className="size-4 text-muted-foreground animate-[spin_4s_linear_infinite_reverse]" />}
            {pill === "Escalations" && <Flag className="size-4 text-muted-foreground" />}
            {pill === "Rulebook" && <ScrollText className="size-4 text-muted-foreground" />}
            {pill === "Agent Flow" && <Network className="size-4 text-muted-foreground" />}
            <span>{pill}</span>
          </div>
        </>
      )}
    </div>
  );
}
