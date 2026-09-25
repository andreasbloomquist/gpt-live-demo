import { CallsHeader } from "@/components/calls/States";
import styles from "@/components/calls/calls.module.css";

export default function Loading() {
  return (
    <main className="page" aria-busy="true">
      <CallsHeader />
      <p className="sr-only" role="status">
        Loading calls…
      </p>
      <ul className={styles.list} aria-hidden="true">
        {Array.from({ length: 5 }, (_, i) => (
          <li key={i} className={`card ${styles.row}`}>
            <div className={`skeleton ${styles.skeletonRing}`} />
            <div className={styles.rowMain}>
              <div className={`skeleton ${styles.skeletonTitle}`} />
              <div className={`skeleton ${styles.skeletonSummary}`} />
              <div className={`skeleton ${styles.skeletonMeta}`} />
            </div>
          </li>
        ))}
      </ul>
    </main>
  );
}
