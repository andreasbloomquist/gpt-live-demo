import { CallsHeader } from "@/components/calls/States";
import styles from "@/components/calls/calls.module.css";

export default function Loading() {
  return (
    <main className="page" aria-busy="true">
      <CallsHeader />
      <p className="sr-only" role="status">Loading calls…</p>
      <ul className={styles.list} aria-hidden="true">
        {Array.from({ length: 5 }, (_, i) => (
          <li key={i} className={`card ${styles.row}`}>
            <div className="skeleton" style={{ width: 52, height: 52, borderRadius: "50%" }} />
            <div className={styles.rowMain}>
              <div className="skeleton" style={{ width: "45%", height: 18 }} />
              <div className="skeleton" style={{ width: "85%", height: 14, marginTop: 10 }} />
              <div className="skeleton" style={{ width: "30%", height: 12, marginTop: 10 }} />
            </div>
          </li>
        ))}
      </ul>
    </main>
  );
}
