import styles from "@/components/call-detail/detail.module.css";

export default function Loading() {
  return (
    <main className="page" aria-busy="true">
      <p className="sr-only" role="status">Loading call…</p>
      <div aria-hidden="true">
        <div className={`skeleton ${styles.skeletonBack}`} />
        <div className={styles.hero}>
          <div className={styles.heroText}>
            <div className={`skeleton ${styles.skeletonDate}`} />
            <div className={`skeleton ${styles.skeletonTitle}`} />
            <div className={`skeleton ${styles.skeletonLede}`} />
            <div className={`skeleton ${styles.skeletonLedeEnd}`} />
          </div>
          <div className={`skeleton ${styles.skeletonRing}`} />
        </div>
        <div className={`${styles.scoreGrid} ${styles.skeletonGrid}`}>
          {Array.from({ length: 6 }, (_, i) => (
            <div key={i} className={`skeleton ${styles.skeletonDim}`} />
          ))}
        </div>
      </div>
    </main>
  );
}
