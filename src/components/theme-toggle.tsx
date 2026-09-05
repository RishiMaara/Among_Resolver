/**
 * Day/night toggle.
 *
 * The icon now shows the CURRENT sky rather than the pending action: sun in
 * light mode, moon in dark. The old component did the opposite — it showed a
 * sun while dark, meaning "click for light" — and that convention cannot
 * carry a sunset. Going dark would have raised the sun. Since the animation
 * is the point, the meaning follows it.
 *
 * Both bodies stay mounted and are driven by transitions rather than by
 * mounting one and unmounting the other. A swap cannot be interrupted
 * halfway; a transition can, so double-clicking reverses mid-arc instead of
 * queueing a second full animation.
 */

import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";

export function ThemeToggle() {
  const [dark, setDark] = useState(false);
  // The stored theme is not known until after the first paint, so `dark`
  // starts false and corrects itself on mount. Without this flag that
  // correction would play a full sunrise at every page load in dark mode —
  // an animation for something the user did not do.
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem("theme");
    const isDark = stored
      ? stored === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    setDark(isDark);
    document.documentElement.classList.toggle("dark", isDark);
    // Two frames: one for the state to paint, one for the browser to accept
    // it as the starting point rather than transitioning from the default.
    const id = requestAnimationFrame(() => requestAnimationFrame(() => setReady(true)));
    return () => cancelAnimationFrame(id);
  }, []);

  const toggle = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    localStorage.setItem("theme", next ? "dark" : "light");
  };

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
      aria-pressed={dark}
      className="theme-toggle"
      data-dark={dark ? "true" : "false"}
      data-ready={ready ? "true" : "false"}
    >
      <span className="theme-toggle-sky" aria-hidden="true">
        <Sun className="theme-toggle-body theme-toggle-sun" strokeWidth={2} />
        <Moon className="theme-toggle-body theme-toggle-moon" strokeWidth={2} />
      </span>
    </button>
  );
}
