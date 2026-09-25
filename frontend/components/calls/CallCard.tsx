/** One row in the call history list. Works in both Server and Client Components. */
import Link from "next/link";
import { ChevronRightIcon } from "@/components/icons";
import { LocalTime } from "@/components/LocalTime";
import { formatDuration } from "@/lib/format";
import type { CallSummary } from "@/lib/types";
import { AnalysisStatusPill, OutcomePill } from "./Badges";
import { ScoreRing } from "./ScoreRing";
import styles from "./calls.module.css";

export function CallCard({ call }: { call: CallSummary }) {
  const title =
    call.caller_intent ?? (call.status === "done" ? "Call" : "Waiting for analysis…");
  return (
    <li>
      <Link href={`/calls/${encodeURIComponent(call.call_id)}`} className={`card ${styles.row}`}>
        <ScoreRing score={call.overall_score} />
        <div className={styles.rowMain}>
          <div className={styles.rowTitle}>
            <h2>{title}</h2>
          </div>
          {call.summary && <p className={styles.summary}>{call.summary}</p>}
          <p className={styles.meta}>
            {call.started_at && <LocalTime iso={call.started_at} />}
            <span aria-hidden="true">·</span>
            <span>{formatDuration(call.duration_s)}</span>
            <span aria-hidden="true">·</span>
            <span>{call.turns} turns</span>
          </p>
        </div>
        <div className={styles.rowSide}>
          <OutcomePill status={call.outcome?.status} />
          <AnalysisStatusPill status={call.status} />
          <ChevronRightIcon className={styles.chevron} />
        </div>
      </Link>
    </li>
  );
}
