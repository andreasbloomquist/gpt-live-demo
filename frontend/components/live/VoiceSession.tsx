"use client";

/**
 * The in-call UI. Must be rendered inside <LiveKitRoom>.
 *
 * useVoiceAssistant() finds the agent participant and exposes its state
 * (published by livekit-agents as the `lk.agent.state` attribute) plus its
 * audio track, which drives the visualizer inside the orb.
 */
import { useEffect, useState } from "react";
import {
  BarVisualizer,
  DisconnectButton,
  TrackToggle,
  useVoiceAssistant,
} from "@livekit/components-react";
import { Track } from "livekit-client";
import { MicIcon, MicOffIcon, PhoneDownIcon } from "@/components/icons";
import { formatClock } from "@/lib/format";
import { Orb } from "./Orb";
import { Transcript } from "./Transcript";
import styles from "./Live.module.css";

const STATE_LABELS: Record<string, string> = {
  disconnected: "Disconnected",
  connecting: "Connecting to Ava…",
  "pre-connect-buffering": "Connecting to Ava…",
  initializing: "Ava is joining…",
  idle: "Ready",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
  failed: "Ava couldn't join. End the call and try again.",
};

/** Seconds since this component mounted (i.e. since the call connected). */
function useElapsed(): number {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const start = performance.now();
    const id = setInterval(() => setElapsed(Math.floor((performance.now() - start) / 1000)), 1000);
    return () => clearInterval(id);
  }, []);
  return elapsed;
}

export function VoiceSession() {
  const { state, audioTrack } = useVoiceAssistant();
  const elapsed = useElapsed();

  return (
    <div className={styles.call}>
      <section className={`card ${styles.stage}`} aria-label="Call">
        <div className={styles.stageTop}>
          <span className={styles.liveBadge}>
            <span className={styles.liveDot} aria-hidden="true" /> Live
          </span>
          <span className={styles.timer} aria-label="Call duration">
            {formatClock(elapsed)}
          </span>
        </div>

        <Orb state={state}>
          {/* Bars follow Ava's voice; `state` adds listening/thinking patterns. */}
          <BarVisualizer
            state={state}
            track={audioTrack}
            barCount={5}
            options={{ minHeight: 14, maxHeight: 70 }}
            className={styles.bars}
          />
        </Orb>

        <div className={styles.who}>
          <p className={styles.agentName}>Ava</p>
          <p className={styles.state} role="status" data-state={state}>
            {STATE_LABELS[state] ?? state}
          </p>
        </div>

        <div className={styles.controls}>
          <div className={styles.control}>
            {/* TrackToggle sets aria-pressed + data-lk-enabled; CSS swaps the icon and caption. */}
            <TrackToggle
              source={Track.Source.Microphone}
              showIcon={false}
              className={styles.roundBtn}
              aria-label="Microphone"
            >
              <MicIcon className={styles.iconOn} />
              <MicOffIcon className={styles.iconOff} />
            </TrackToggle>
            <span className={styles.caption} aria-hidden="true">
              <span className={styles.captionOn}>Mute</span>
              <span className={styles.captionOff}>Unmute</span>
            </span>
          </div>
          <div className={styles.control}>
            <DisconnectButton
              className={`${styles.roundBtn} ${styles.endBtn}`}
              aria-label="End call"
            >
              <PhoneDownIcon />
            </DisconnectButton>
            <span className={styles.caption} aria-hidden="true">
              End
            </span>
          </div>
        </div>
      </section>

      <Transcript agentSpeaking={state === "speaking"} />
    </div>
  );
}
