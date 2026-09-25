/** Deterministic metrics (computed by the analyzer in code, not by the LLM). */
import { formatPercent } from "@/lib/format";
import type { Metrics } from "@/lib/types";
import styles from "./detail.module.css";

export function MetricsRow({ m }: { m: Metrics }) {
  const agentShare = Math.min(1, Math.max(0, m.talk_ratio_agent));
  return (
    <dl className={styles.metrics}>
      <div className={`card ${styles.metric} ${styles.metricWide}`}>
        <dt>Talk ratio</dt>
        <dd>
          <span className={styles.metricValue}>{formatPercent(agentShare)}</span>
          <span className={styles.metricSub}>Ava · {formatPercent(1 - agentShare)} caller</span>
          <span className={styles.split} aria-hidden="true">
            <span style={{ width: `${agentShare * 100}%` }} />
          </span>
        </dd>
      </div>
      <Metric label="Interruptions" value={m.interruptions} sub="Turns cut off mid-speech" />
      <Metric
        label="Tool calls"
        value={m.tool_calls}
        sub={m.tool_errors > 0 ? `${m.tool_errors} failed` : "No errors"}
        alert={m.tool_errors > 0}
      />
      <Metric
        label="Transcript confidence"
        value={m.mean_transcript_confidence === null ? "–" : formatPercent(m.mean_transcript_confidence)}
        sub="Mean over caller turns"
      />
      <Metric
        label="Low-confidence turns"
        value={m.low_confidence_turns}
        sub="Below 60% STT confidence"
        alert={m.low_confidence_turns > 0}
      />
      <Metric label="Words per Ava turn" value={m.avg_agent_words_per_turn.toFixed(0)} sub={`${m.agent_turns} Ava · ${m.user_turns} caller turns`} />
    </dl>
  );
}

function Metric({ label, value, sub, alert }: { label: string; value: string | number; sub: string; alert?: boolean }) {
  return (
    <div className={`card ${styles.metric}`}>
      <dt>{label}</dt>
      <dd>
        <span className={styles.metricValue}>{value}</span>
        <span className={styles.metricSub} data-alert={alert || undefined}>
          {sub}
        </span>
      </dd>
    </div>
  );
}
