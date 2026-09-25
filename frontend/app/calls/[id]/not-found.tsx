import Link from "next/link";
import styles from "@/components/calls/calls.module.css";

export default function CallNotFound() {
  return (
    <main className="page">
      <section className={`card ${styles.state}`}>
        <h2>Call not found</h2>
        <p>This call doesn&rsquo;t exist, or the analyzer no longer has it.</p>
        <Link href="/calls" className="btn">
          Back to calls
        </Link>
      </section>
    </main>
  );
}
