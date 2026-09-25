import styles from "@/components/call/detail.module.css";

export default function Loading() {
  return (
    <main className="page" aria-busy="true">
      <p className="sr-only" role="status">Loading call…</p>
      <div aria-hidden="true">
        <div className="skeleton" style={{ width: 70, height: 16, marginBottom: 36 }} />
        <div className={styles.hero}>
          <div className={styles.heroText}>
            <div className="skeleton" style={{ width: 180, height: 14 }} />
            <div className="skeleton" style={{ width: "70%", height: 40, marginTop: 14 }} />
            <div className="skeleton" style={{ width: "90%", height: 18, marginTop: 18 }} />
            <div className="skeleton" style={{ width: "60%", height: 18, marginTop: 8 }} />
          </div>
          <div className="skeleton" style={{ width: 112, height: 112, borderRadius: "50%" }} />
        </div>
        <div className={styles.scoreGrid} style={{ marginTop: 48 }}>
          {Array.from({ length: 6 }, (_, i) => (
            <div key={i} className="skeleton" style={{ height: 170, borderRadius: 20 }} />
          ))}
        </div>
      </div>
    </main>
  );
}
