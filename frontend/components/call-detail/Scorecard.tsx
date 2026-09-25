/** Grid of the nine rubric dimensions: score, meter, rationale, and evidence links. */
import { DIMENSION_META, DIMENSION_ORDER, frustrationLevel, goodness } from "@/lib/dimensions";
import { humanize } from "@/lib/format";
import type { Analysis, Dimension } from "@/lib/types";
import { turnAnchor } from "./CallTranscript";
import styles from "./detail.module.css";

const tone = (good: number) => (good >= 4 ? "green" : good === 3 ? "amber" : "red");

export function Scorecard({ scores }: { scores: Analysis["scores"] }) {
  // Rubric order first, then anything newer the analyzer sends that we don't know yet.
  const keys = [
    ...DIMENSION_ORDER.filter((d) => scores[d]),
    ...(Object.keys(scores) as Dimension[]).filter((d) => !DIMENSION_META[d]),
  ];

  return (
    <div className={styles.scoreGrid}>
      {keys.map((dim) => {
        const s = scores[dim];
        if (!s) return null;
        const meta = DIMENSION_META[dim];
        const inverted = meta?.inverted ?? false;
        const good = goodness(dim, s.score);
        return (
          <article key={dim} className={`card ${styles.dim}`} data-tone={tone(good)}>
            <header className={styles.dimHead}>
              <div>
                <h3>{meta?.label ?? humanize(dim)}</h3>
                {inverted && <p className={styles.dimNote}>Lower is better · 1 = none, 5 = severe</p>}
              </div>
              <p className={styles.dimScore} aria-label={`Score ${s.score} out of 5`}>
                {s.score}
                <span>/5</span>
              </p>
            </header>
            <div className={styles.meter} aria-hidden="true">
              {[1, 2, 3, 4, 5].map((i) => (
                <span key={i} data-on={i <= s.score || undefined} />
              ))}
            </div>
            {inverted && <p className={styles.dimLevel}>{frustrationLevel(s.score)} frustration</p>}
            <p className={styles.rationale}>{s.rationale}</p>
            {s.evidence?.length > 0 && (
              <ul className={styles.evidence} aria-label="Evidence">
                {s.evidence.map((ev, i) => (
                  <li key={i}>
                    <a href={`#${turnAnchor(ev.turn_id)}`} title="Jump to this turn in the transcript">
                      &ldquo;{ev.quote}&rdquo;
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </article>
        );
      })}
    </div>
  );
}
