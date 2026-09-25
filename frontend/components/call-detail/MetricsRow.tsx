/** Deterministic metrics (computed by the analyzer in code, not by the LLM). */
import { formatPercent } from "@/lib/format";
import type { Metrics } from "@/lib/types";
import { LOW_CONFIDENCE } from "./CallTranscript";
import styles from "./detail.module.css";

export function MetricsRow({ metrics }: { metrics: Metrics }) {
  const agentShare = Math.min(1, Math.max(0, metrics.talk_ratio_agent));
  return (
    <dl className={styles.metrics}>
      <div className={`card ${styles.metric}`}>
        <dt>Talk ratio</dt>
        <dd>
          <span className={styles.metricValue}>{formatPercent(agentShare)}</span>
          <span className={styles.metricSub}>Ava · {formatPercent(1 - agentShare)} caller</span>
          <span className={styles.split} aria-hidden="true">
            <span style={{ width: `${agentShare * 100}%` }} />
          </span>
        </dd>
      </div>
      <Metric label="Interruptions" value={metrics.interruptions} sub="Turns cut off mid-speech" />
      <Metric
        label="Tool calls"
        value={metrics.tool_calls}
        sub={metrics.tool_errors > 0 ? `${metrics.tool_errors} failed` : "No errors"}
        alert={metrics.tool_errors > 0}
      />
      <Metric
        label="Transcript confidence"
        value={
          metrics.mean_transcript_confidence === null
            ? "–"
            : formatPercent(metrics.mean_transcript_confidence)
        }
        sub="Mean over caller turns"
      />
      <Metric
        label="Low-confidence turns"
        value={metrics.low_confidence_turns}
        sub={`Below ${formatPercent(LOW_CONFIDENCE)} STT confidence`}
        alert={metrics.low_confidence_turns > 0}
      />
      <Metric
        label="Words per Ava turn"
        value={metrics.avg_agent_words_per_turn.toFixed(0)}
        sub={`${metrics.agent_turns} Ava · ${metrics.user_turns} caller turns`}
      />
    </dl>
  );
}

type MetricProps = {
  label: string;
  value: string | number;
  sub: string;
  /** Highlights `sub` when the metric points at a problem. */
  alert?: boolean;
};

function Metric({ label, value, sub, alert = false }: MetricProps) {
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
