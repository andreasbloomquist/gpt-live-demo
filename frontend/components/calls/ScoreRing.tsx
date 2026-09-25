/** Circular 0–100 score gauge (inline SVG). Color encodes the band, the number is always shown. */
import styles from "./calls.module.css";

type Props = { score: number | null; size?: number; label?: string };

function scoreTone(score: number): "green" | "amber" | "red" {
  return score >= 80 ? "green" : score >= 60 ? "amber" : "red";
}

export function ScoreRing({ score, size = 52, label = "Overall score" }: Props) {
  const stroke = Math.max(4, Math.round(size / 12));
  const r = (size - stroke) / 2;
  const circumference = 2 * Math.PI * r;
  const value = score === null ? 0 : Math.min(100, Math.max(0, score));
  const tone = score === null ? "none" : scoreTone(value);

  return (
    <div
      className={styles.ring}
      data-tone={tone}
      style={{ width: size, height: size, fontSize: size * 0.32 }}
      role="img"
      aria-label={score === null ? `${label}: not available` : `${label}: ${value} out of 100`}
    >
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} aria-hidden="true">
        <circle className={styles.ringTrack} cx={size / 2} cy={size / 2} r={r} strokeWidth={stroke} />
        <circle
          className={styles.ringValue}
          cx={size / 2}
          cy={size / 2}
          r={r}
          strokeWidth={stroke}
          strokeDasharray={`${(value / 100) * circumference} ${circumference}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <span aria-hidden="true">{score === null ? "–" : value}</span>
    </div>
  );
}
