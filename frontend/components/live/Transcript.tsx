"use client";

/**
 * Live transcript for both sides of the conversation, as chat bubbles.
 *
 * livekit-agents publishes transcriptions as text streams on the `lk.transcription`
 * topic. useTranscriptions() collects them, merging updates that share an
 * `lk.segment_id` into one entry (so a caller's interim STT text is replaced in
 * place, and the agent's reply grows word by word).
 *
 * Order: the hook's array is in first-seen order, which is the order utterances
 * started. We keep that instead of sorting by `streamInfo.timestamp`, because the
 * hook swaps in the *latest* stream's info on every update, so a long user segment
 * would otherwise jump below the agent reply that started after it.
 */
import { useEffect, useRef } from "react";
import { useLocalParticipant, useTranscriptions } from "@livekit/components-react";
import styles from "./Live.module.css";

// Stream attributes set by livekit-agents on each transcription stream.
const SEGMENT_ID_ATTR = "lk.segment_id";
const FINAL_ATTR = "lk.transcription_final";

const TIME_FORMAT = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

export function Transcript({ agentSpeaking }: { agentSpeaking: boolean }) {
  const transcriptions = useTranscriptions();
  const { localParticipant } = useLocalParticipant();
  const logRef = useRef<HTMLDivElement>(null);

  const lines = transcriptions.filter((t) => t.text.trim().length > 0);

  // Keep the newest line in view (re-runs as lines are added or grow). Scroll only the
  // transcript box: scrollIntoView() would also yank the whole page on every word.
  const lastText = lines.at(-1)?.text;
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, lastText]);

  return (
    <section className={`card ${styles.transcriptCard}`} aria-labelledby="transcript-title">
      <header className={styles.transcriptHeader}>
        <h2 id="transcript-title">Transcript</h2>
        <span className="muted">Live</span>
      </header>
      <div ref={logRef} className={styles.log} role="log" aria-labelledby="transcript-title">
        {lines.length === 0 ? (
          <p className={styles.empty}>Say hello. The conversation appears here as you talk.</p>
        ) : (
          lines.map((t, i) => {
            const isUser = t.participantInfo.identity === localParticipant.identity;
            const attrs = t.streamInfo.attributes ?? {};
            // Caller STT sends interim results as separate non-final streams. The agent's
            // header always says non-final (its final flag rides on the stream trailer,
            // which the hook doesn't surface), so for Ava use "still speaking" instead.
            const interim = isUser
              ? attrs[FINAL_ATTR] === "false"
              : agentSpeaking && i === lines.length - 1;
            return (
              <div
                key={attrs[SEGMENT_ID_ATTR] ?? t.streamInfo.id}
                className={`${styles.msg} ${isUser ? styles.fromUser : styles.fromAgent}`}
                data-interim={interim || undefined}
              >
                <p className={styles.bubble}>
                  <span className="sr-only">{isUser ? "You: " : "Ava: "}</span>
                  {t.text}
                </p>
                <span className={styles.meta} aria-hidden="true">
                  {isUser ? "You" : "Ava"} · {TIME_FORMAT.format(t.streamInfo.timestamp)}
                  {interim && (isUser ? " · transcribing…" : " · speaking…")}
                </span>
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
