"use client";

/**
 * The in-call UI. Must be rendered inside <LiveKitRoom>.
 *
 * useVoiceAssistant() finds the agent participant and exposes its state
 * (published by livekit-agents as the `lk.agent.state` attribute) plus its
 * audio track, which drives the visualizer.
 */
import {
  BarVisualizer,
  DisconnectButton,
  TrackToggle,
  useVoiceAssistant,
} from "@livekit/components-react";
import { Track } from "livekit-client";
import { Transcript } from "./Transcript";

const STATE_LABELS: Record<string, string> = {
  disconnected: "Disconnected",
  connecting: "Waiting for agent…",
  "pre-connect-buffering": "Waiting for agent…",
  initializing: "Agent starting…",
  idle: "Idle",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
  failed: "Agent failed to join",
};

export function VoiceSession() {
  const { state, audioTrack } = useVoiceAssistant();

  return (
    <section className="card session">
      <div className="status" role="status">
        <span className={`dot dot-${state}`} aria-hidden />
        <span>{STATE_LABELS[state] ?? state}</span>
      </div>

      {/* Bars animate with the agent's voice; `state` adds idle/thinking animations. */}
      <BarVisualizer
        state={state}
        track={audioTrack}
        barCount={7}
        className="visualizer"
        aria-hidden
      />

      <div className="controls">
        {/* TrackToggle sets aria-pressed, so "Microphone" + pressed state reads correctly. */}
        <TrackToggle
          source={Track.Source.Microphone}
          className="btn"
          showIcon
          aria-label="Microphone"
        >
          Mic
        </TrackToggle>
        <DisconnectButton className="btn danger" aria-label="End conversation">
          End
        </DisconnectButton>
      </div>

      <Transcript />
    </section>
  );
}
