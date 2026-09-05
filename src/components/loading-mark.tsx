/**
 * The loading indicator, built from the logo rather than beside it.
 *
 * Ported from the supplied logo animation: the lattice holds still while the
 * green path draws through it and the nodes arrive as the line reaches them.
 * That motion is not decoration here — it is what the engine is doing while
 * you wait. The grid is the candidate pool, the line is the subset being
 * resolved, and the nodes are members as they are identified.
 *
 * The clip runs once and settles; a loading state has to loop, so the path
 * retracts from its tail instead of cutting back to the start. Nothing jumps,
 * and there is no frame where the mark is absent.
 *
 * The root is a <span>, not a <div>, and that is load-bearing. This is
 * dropped wherever the wait is — inside a stat card's <p>, inside a
 * <button>, beside a heading — and a <div> is invalid in most of those. It
 * was a <div>: inside a <p> the parser closed that paragraph early, so the
 * server-rendered DOM and the client tree diverged and React's hydration
 * failed for the entire route. inline-flex lays out identically.
 *
 * SVG, not the mp4: this scales to any size, follows the theme through
 * --mark-dot / --mark-ink, weighs nothing, and does not need to be decoded
 * before the first frame — which matters for a thing whose whole job is to
 * appear the instant something is slow.
 */

const DOTS = [
  [5.5, 5.5],
  [12.5, 5.5],
  [19.5, 5.5],
  [26.5, 5.5],
  [5.5, 12.5],
  [12.5, 12.5],
  [26.5, 12.5],
  [12.5, 19.5],
  [19.5, 19.5],
  [26.5, 19.5],
  [5.5, 26.5],
  [12.5, 26.5],
  [19.5, 26.5],
] as const;

// Both segments are hypot(14, 7); the dash animation needs the true length or
// the path draws at the wrong rate.
const NODES = [
  [5.5, 19.5],
  [19.5, 12.5],
  [26.5, 26.5],
] as const;

export function LoadingMark({
  size = 28,
  label,
  className = "",
  onSolid = false,
}: {
  size?: number;
  label?: string;
  className?: string;
  /** On a filled button the theme tokens are wrong: --mark-dot is tuned to sit
   *  on the page background and vanishes on a dark fill. Deriving both from
   *  currentColor keeps the mark legible on whatever it is placed. */
  onSolid?: boolean;
}) {
  const tone = onSolid
    ? ({
        "--mark-dot": "color-mix(in oklab, currentColor 32%, transparent)",
        "--mark-ink": "currentColor",
      } as React.CSSProperties)
    : undefined;
  return (
    <span className={`inline-flex items-center gap-2.5 align-middle ${className}`}>
      <svg
        width={size}
        height={size}
        viewBox="0 0 32 32"
        className="loading-mark block flex-none"
        style={tone}
        role="status"
        aria-label={label ?? "Loading"}
      >
        <g fill="var(--mark-dot)">
          {DOTS.map(([x, y], i) => (
            <circle
              key={`${x}-${y}`}
              cx={x}
              cy={y}
              r="1.55"
              className="loading-mark-dot"
              style={{ animationDelay: `${(i % 4) * 90 + Math.floor(i / 4) * 60}ms` }}
            />
          ))}
        </g>
        <path
          d="M5.5 19.5L19.5 12.5L26.5 26.5"
          fill="none"
          stroke="var(--mark-ink)"
          strokeWidth="2.3"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="loading-mark-path"
        />
        {NODES.map(([x, y], i) => (
          <circle
            key={`${x}-${y}`}
            cx={x}
            cy={y}
            r="2.7"
            fill="var(--mark-ink)"
            className="loading-mark-node"
            style={{ animationDelay: `${i * 300}ms` }}
          />
        ))}
      </svg>
      {label && <span className="text-[13px] text-muted-foreground">{label}</span>}
    </span>
  );
}
