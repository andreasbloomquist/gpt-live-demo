/**
 * Caller sentiment across the call as a tiny inline-SVG line chart (-1 … +1).
 * Each point links to its turn in the transcript.
 */
import type { Analysis } from "@/lib/types";
import { turnAnchor } from "./CallTranscript";
import styles from "./detail.module.css";

const W = 640;
const H = 132;
const PAD_X = 12;
const PAD_Y = 14;

const signed = (v: number) => (v > 0 ? `+${v.toFixed(2)}` : v.toFixed(2));

export function Sparkline({ points }: { points: Analysis["sentiment"] }) {
  if (points.length === 0) return <p className="muted">No sentiment data for this call.</p>;

  const x = (i: number) =>
    points.length === 1 ? W / 2 : PAD_X + (i * (W - 2 * PAD_X)) / (points.length - 1);
  const y = (v: number) => PAD_Y + ((1 - Math.max(-1, Math.min(1, v))) / 2) * (H - 2 * PAD_Y);
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(1)},${y(0)} L${x(0).toFixed(1)},${y(0)} Z`;

  const first = points[0].value;
  const last = points[points.length - 1].value;
  const summary = `Caller sentiment across ${points.length} caller turns, from ${signed(first)} at the start to ${signed(last)} at the end (scale −1 to +1).`;

  return (
    <figure className={styles.spark}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary}>
        <defs>
          <linearGradient id="sentiment-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <line className={styles.sparkZero} x1={0} x2={W} y1={y(0)} y2={y(0)} />
        <path d={area} fill="url(#sentiment-fill)" />
        <path className={styles.sparkLine} d={line} />
        {points.map((p, i) => (
          <a key={`${p.turn_id}-${i}`} href={`#${turnAnchor(p.turn_id)}`}>
            <title>{`Caller turn ${i + 1}: ${signed(p.value)}`}</title>
            <circle
              className={styles.sparkDot}
              data-tone={p.value > 0.2 ? "pos" : p.value < -0.2 ? "neg" : "neu"}
              cx={x(i)}
              cy={y(p.value)}
              r={5}
            />
          </a>
        ))}
      </svg>
      <figcaption className={styles.sparkAxis} aria-hidden="true">
        <span>Start</span>
        <span>End of call</span>
      </figcaption>
    </figure>
  );
}
