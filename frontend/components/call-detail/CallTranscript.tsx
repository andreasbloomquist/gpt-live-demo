/**
 * The recorded transcript: chat bubbles with per-turn STT confidence, interruption
 * markers, and tool-call chips interleaved by time. Each turn has an id anchor so
 * evidence quotes, flags, and sentiment points can link to it (highlighted via :target).
 */
import { AlertIcon, InterruptIcon, ToolIcon } from "@/components/icons";
import { formatClock, formatPercent } from "@/lib/format";
import { buildTimeline, offsetFrom } from "@/lib/timeline";
import type { CallRecord, ToolCall, Turn } from "@/lib/types";
import styles from "./detail.module.css";

/** Matches the analyzer's LOW_CONFIDENCE_THRESHOLD (analyzer/call_analyzer/metrics.py). */
export const LOW_CONFIDENCE = 0.6;
const HIGH_CONFIDENCE = 0.85;

export function turnAnchor(turnId: string): string {
  return `turn-${turnId}`;
}

type Confidence = "high" | "medium" | "low" | "none";

function confidenceLevel(c: number | null): Confidence {
  if (c === null) return "none";
  return c < LOW_CONFIDENCE ? "low" : c < HIGH_CONFIDENCE ? "medium" : "high";
}

export function CallTranscript({ record }: { record: CallRecord }) {
  const items = buildTimeline(record.turns, record.tool_calls);
  return (
    <>
      <Legend />
      <ol className={`card ${styles.transcript}`} aria-label="Call transcript">
        {items.map((item) =>
          item.kind === "turn" ? (
            <TurnRow key={`t-${item.turn.id}`} turn={item.turn} callStart={record.started_at} />
          ) : (
            <ToolRow key={`c-${item.call.id}`} call={item.call} callStart={record.started_at} />
          ),
        )}
      </ol>
    </>
  );
}

function TurnRow({ turn, callStart }: { turn: Turn; callStart: string }) {
  const isUser = turn.role === "user";
  const conf = confidenceLevel(turn.transcript_confidence);
  const offset = offsetFrom(callStart, turn.started_at);
  const pct =
    turn.transcript_confidence === null ? null : Math.round(turn.transcript_confidence * 100);

  return (
    <li
      id={turnAnchor(turn.id)}
      className={`${styles.turn} ${isUser ? styles.turnUser : styles.turnAgent}`}
      data-confidence={conf}
      tabIndex={-1}
    >
      <p className={styles.turnBubble}>
        <span className="sr-only">{isUser ? "Caller: " : "Ava: "}</span>
        {turn.text || <em className={styles.noWords}>No words transcribed</em>}
        {turn.interrupted && (
          <span className={styles.cut} aria-hidden="true">
            —
          </span>
        )}
      </p>
      <p className={styles.turnMeta}>
        <span>{isUser ? "Caller" : "Ava"}</span>
        {offset !== null && <span className={styles.mono}>{formatClock(offset)}</span>}
        {pct !== null && (
          <span
            className={styles.conf}
            data-level={conf}
            title="Speech-to-text confidence for this turn"
          >
            {conf === "low" && <AlertIcon />}
            <span className="sr-only">Transcription confidence </span>
            {conf === "low" ? `Low confidence · ${pct}%` : `${pct}%`}
          </span>
        )}
        {turn.interrupted && (
          <span className={styles.interrupted}>
            <InterruptIcon /> Interrupted
          </span>
        )}
      </p>
    </li>
  );
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function ToolRow({ call, callStart }: { call: ToolCall; callStart: string }) {
  const offset = offsetFrom(callStart, call.created_at);
  return (
    <li className={styles.toolRow}>
      <details className={styles.tool} data-error={call.is_error || undefined}>
        <summary>
          <ToolIcon />
          <code>{call.name}</code>
          <span className={styles.toolStatus}>{call.is_error ? "Error" : "OK"}</span>
          {offset !== null && <span className={styles.mono}>{formatClock(offset)}</span>}
        </summary>
        <div className={styles.toolBody}>
          <p className={styles.toolLabel}>Arguments</p>
          <pre>{prettyJson(call.arguments)}</pre>
          <p className={styles.toolLabel}>{call.is_error ? "Error" : "Output"}</p>
          <pre>{call.output === null ? "(no output)" : prettyJson(call.output)}</pre>
        </div>
      </details>
    </li>
  );
}

function Legend() {
  return (
    <ul className={styles.legend} aria-label="Legend">
      <li>
        <span className={styles.conf} data-level="high">
          97%
        </span>{" "}
        Caller speech-to-text confidence
      </li>
      <li>
        <span className={styles.conf} data-level="medium">
          72%
        </span>{" "}
        Medium (below {formatPercent(HIGH_CONFIDENCE)})
      </li>
      <li>
        <span className={styles.conf} data-level="low">
          <AlertIcon /> Low
        </span>{" "}
        Below {formatPercent(LOW_CONFIDENCE)}: likely misheard
      </li>
      <li>
        <span className={styles.interrupted}>
          <InterruptIcon /> Interrupted
        </span>{" "}
        Cut off mid-turn
      </li>
      <li>
        <span className={styles.legendTool}>
          <ToolIcon /> Tool
        </span>{" "}
        Tool call (tap for details)
      </li>
    </ul>
  );
}
