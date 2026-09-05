/**
 * The History nav link, whose clock winds forward on a click and back on the
 * next.
 *
 * THREE THINGS BROKE THIS, IN ORDER
 * ---------------------------------
 * 1. The handler sat on the clock icon: 16×16 inside a 68×21 link, about 18%
 *    of it. Four clicks in five landed on the word "History" and wound
 *    nothing. The whole link is the control now.
 *
 * 2. The click was not held, so the navigation unmounted the component about
 *    twenty milliseconds into a 620ms animation. It is held now, and the
 *    route changes when the wind finishes.
 *
 * 3. THE CSS TRANSITION ONLY EVER WORKED ONE WAY. Winding was expressed as
 *    `transform: rotate(360deg)` against `rotate(0deg)`, and those compute to
 *    the same matrix. Going 0 → 360 animated; coming back 360 → 0 was a
 *    no-change, so the browser started no transition at all — verified by
 *    listening for transitionrun, which fired in one direction and never in
 *    the other. Alternating clicks therefore spun once and then sat still.
 *
 * So the rotation is driven by the Web Animations API rather than a CSS
 * transition. Explicit keyframes cannot collapse to "no change": every press
 * plays a real 0→360 turn, reversed on the way back, and the direction is
 * what says whether this press is going or returning.
 */

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Clock } from "lucide-react";

const KEY = "among.clock-wound";
const WIND_MS = 620;

function read(): boolean {
  try {
    return sessionStorage.getItem(KEY) === "1";
  } catch {
    // Private mode or storage disabled. Level is the honest default.
    return false;
  }
}

function reducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function HistoryLink({
  className = "",
  children,
  iconClassName = "size-4",
}: {
  className?: string;
  children?: React.ReactNode;
  iconClassName?: string;
}) {
  const [wound, setWound] = useState(false);
  const iconRef = useRef<HTMLSpanElement>(null);
  const navigate = useNavigate();

  // Read after mount: the server pass has no sessionStorage, and seeding
  // state from it during render would mismatch hydration.
  useEffect(() => setWound(read()), []);

  function onClick(e: React.MouseEvent) {
    // Never intercept a deliberate new-tab or context click.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    e.preventDefault();

    const next = !wound;
    setWound(next);
    try {
      sessionStorage.setItem(KEY, next ? "1" : "0");
    } catch {
      /* Storage refused; the wind still plays for this view. */
    }

    const reduced = reducedMotion();
    const el = iconRef.current;

    if (!reduced && el?.animate) {
      // Rotation ALONE is not visible on this icon, which is the last reason
      // this looked broken after the mechanism was fixed. A clock face is a
      // circle: turning it through 360 degrees leaves the outline identical
      // and moves only the hands — about three pixels of them, at 16px, in
      // half a second. It was animating the whole time and there was nothing
      // to see.
      //
      // So the turn carries a scale with it. The icon swells slightly through
      // the middle of the wind and settles back, which the eye catches at any
      // size, and the rotation then reads as the thing that swelling was.
      //
      // Explicit keyframes rather than a CSS transition: rotate(360deg) and
      // rotate(0deg) compute to the same matrix, so the return trip was a
      // no-change and started no transition at all.
      el.animate(
        [
          { transform: "rotate(0deg) scale(1)" },
          { transform: "rotate(180deg) scale(1.35)", offset: 0.5 },
          { transform: "rotate(360deg) scale(1)" },
        ],
        {
          duration: WIND_MS,
          easing: "cubic-bezier(0.34, 1.4, 0.5, 1)",
          direction: next ? "normal" : "reverse",
        },
      );
    }

    // Let the wind play, then go. With motion off there is nothing to wait
    // for, so it navigates at once rather than sitting still for 620ms.
    window.setTimeout(() => void navigate({ to: "/history" }), reduced ? 0 : WIND_MS);
  }

  // A plain anchor, not a router Link: with the handler on a Link the
  // router's own click handler runs alongside this one and navigates
  // regardless of preventDefault. The href is real, so middle-click and
  // "open in new tab" behave normally; only an ordinary left click is
  // intercepted.
  return (
    <a href="/history" onClick={onClick} className={className} data-wound={wound}>
      <span ref={iconRef} className="nav-clock inline-flex">
        <Clock className={iconClassName} />
      </span>
      {children ?? <span className="hidden sm:inline">History</span>}
    </a>
  );
}
