/**
 * Caller sentiment across the call as a tiny line chart (-1 … +1), no chart library.
 *
 * The SVG (grid + line) stretches to the card at a fixed height, so it uses
 * preserveAspectRatio="none" with non-scaling strokes. Dots and axis labels are HTML
 * positioned in percentages on top of it, so they never distort, and each dot is a
 * real link to its turn in the transcript.
 */
import type { Analysis } from "@/lib/types";
import { turnAnchor } from "./CallTranscript";
import styles from "./detail.module.css";

const W = 640;
const H = 132;
const PAD_X = 12;
const PAD_Y = 14;

const AXIS: [number, string][] = [
  [1, "Positive"],
  [0, "Neutral"],
  [-1, "Negative"],
];

const signed = (v: number) => (v > 0 ? `+${v.toFixed(2)}` : v.toFixed(2));

export function Sparkline({ points }: { points: Analysis["sentiment"] }) {
  if (points.length === 0) return <p className="muted">No sentiment data for this call.</p>;

  const x = (i: number) =>
    points.length === 1 ? W / 2 : PAD_X + (i * (W - 2 * PAD_X)) / (points.length - 1);
  const y = (v: number) => PAD_Y + ((1 - Math.max(-1, Math.min(1, v))) / 2) * (H - 2 * PAD_Y);
  const pct = (value: number, total: number) => `${(value / total) * 100}%`;
  const line = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`)
    .join(" ");

  const first = points[0].value;
  const last = points[points.length - 1].value;

  return (
    <figure className={styles.spark}>
      <figcaption className="sr-only">
        Caller sentiment across {points.length} caller turns, from {signed(first)} at the start to{" "}
        {signed(last)} at the end, on a scale of −1 to +1. Each point links to its turn.
      </figcaption>
      <div className={styles.sparkPlot}>
        <ul className={styles.sparkLabels} aria-hidden="true">
          {AXIS.map(([v, label]) => (
            <li key={v} style={{ top: pct(y(v), H) }}>
              {label}
            </li>
          ))}
        </ul>
        <div className={styles.sparkArea}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
            {AXIS.map(([v]) => (
              <line
                key={v}
                className={styles.sparkGrid}
                x1={0}
                x2={W}
                y1={y(v)}
                y2={y(v)}
                data-zero={v === 0 || undefined}
              />
            ))}
            <path className={styles.sparkLine} d={line} />
          </svg>
          {points.map((p, i) => (
            <a
              key={`${p.turn_id}-${i}`}
              href={`#${turnAnchor(p.turn_id)}`}
              className={styles.sparkDot}
              data-tone={p.value > 0.2 ? "pos" : p.value < -0.2 ? "neg" : "neu"}
              style={{ left: pct(x(i), W), top: pct(y(p.value), H) }}
              aria-label={`Caller turn ${i + 1}: sentiment ${signed(p.value)}`}
              title={`Caller turn ${i + 1}: ${signed(p.value)}`}
            />
          ))}
        </div>
      </div>
      <div className={styles.sparkAxis} aria-hidden="true">
        <span>Start</span>
        <span>End of call</span>
      </div>
    </figure>
  );
}
