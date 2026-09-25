"use client";

/**
 * Live transcript for both sides of the conversation.
 *
 * livekit-agents publishes transcriptions as text streams on the
 * `lk.transcription` topic (agent speech and, when available, user speech).
 * useTranscriptions() collects them; each stream is one utterance that grows
 * as new text arrives, so re-rendering the list gives a live transcript.
 */
import { useEffect, useRef } from "react";
import { useLocalParticipant, useTranscriptions } from "@livekit/components-react";

export function Transcript() {
  const transcriptions = useTranscriptions();
  const { localParticipant } = useLocalParticipant();
  const logRef = useRef<HTMLDivElement>(null);

  const lines = [...transcriptions]
    .filter((t) => t.text.trim().length > 0)
    .sort((a, b) => a.streamInfo.timestamp - b.streamInfo.timestamp);

  // Keep the newest line in view (re-runs as lines are added or grow). Scroll only the
  // transcript box: scrollIntoView() would also yank the whole page on every word.
  const lastText = lines.at(-1)?.text;
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, lastText]);

  return (
    <div ref={logRef} className="transcript" role="log" aria-label="Transcript">
      {lines.length === 0 ? (
        <p className="muted">Transcript will appear here.</p>
      ) : (
        lines.map((t) => {
          const isUser = t.participantInfo.identity === localParticipant.identity;
          return (
            <p key={t.streamInfo.id} className={isUser ? "line user" : "line agent"}>
              <span className="who">{isUser ? "You" : "Agent"}</span>
              {t.text}
            </p>
          );
        })
      )}
    </div>
  );
}
