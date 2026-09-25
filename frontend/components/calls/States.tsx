/** Header, empty and error states for the call history page. */
import Link from "next/link";
import { AlertIcon, WaveIcon } from "@/components/icons";
import styles from "./calls.module.css";

export function EmptyCalls() {
  return (
    <section className={`card ${styles.state}`}>
      <div className={styles.stateIcon}>
        <WaveIcon />
      </div>
      <h2>No calls yet</h2>
      <p>
        When a call ends, the agent sends its transcript to the Call Analyzer, which scores it in
        the background. Finished calls show up here.
      </p>
      <p>
        Want sample data? Load the demo calls into the analyzer:
      </p>
      <pre className={styles.cmd}>
        <code>cd analyzer && uv run python -m call_analyzer seed</code>
      </pre>
      <Link href="/" className="btn btn-primary">
        Start a call
      </Link>
    </section>
  );
}

export function AnalyzerUnavailable({
  title = "Can’t load calls right now",
  headingLevel = 2,
  message,
  retryHref,
}: {
  title?: string;
  /** 1 when this is the page's main content (no other <h1> on the page). */
  headingLevel?: 1 | 2;
  message: string;
  retryHref: string;
}) {
  const Heading = headingLevel === 1 ? "h1" : "h2";
  return (
    <section className={`card ${styles.state}`} role="alert">
      <div className={`${styles.stateIcon} ${styles.stateIconError}`}>
        <AlertIcon />
      </div>
      <Heading>{title}</Heading>
      <p>{message}</p>
      <p>Running locally? Start the analyzer:</p>
      <pre className={styles.cmd}>
        <code>cd analyzer && uv run python -m call_analyzer serve</code>
      </pre>
      <Link href={retryHref} className="btn">
        Try again
      </Link>
    </section>
  );
}

export function CallsHeader() {
  return (
    <header className={styles.header}>
      <h1>Calls</h1>
      <p>Every conversation with Ava, transcribed and scored by the Call Analyzer.</p>
    </header>
  );
}
