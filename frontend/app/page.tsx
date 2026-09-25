"use client";

/**
 * Landing page: fetches a token from our own API route, then mounts <LiveKitRoom>.
 * Everything that needs the room (agent state, visualizer, transcript) lives in
 * <VoiceSession>, which only renders once we are connected.
 */
import { useCallback, useState } from "react";
import { LiveKitRoom, RoomAudioRenderer } from "@livekit/components-react";
import { MediaDeviceFailure } from "livekit-client";
import { VoiceSession } from "@/components/VoiceSession";
import type { ConnectionDetails } from "@/lib/types";

export default function Home() {
  const [details, setDetails] = useState<ConnectionDetails | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connect = useCallback(async () => {
    setConnecting(true);
    setError(null);
    try {
      const res = await fetch("/api/token", { method: "POST" });
      // A crashed route or a proxy can answer with HTML: don't surface a JSON parse error.
      const body = await res.json().catch(() => null);
      if (!res.ok || !body) {
        throw new Error(body?.error ?? `Token request failed (HTTP ${res.status})`);
      }
      setDetails(body as ConnectionDetails);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setConnecting(false);
    }
  }, []);

  const disconnect = useCallback(() => setDetails(null), []);

  const onMediaDeviceFailure = useCallback((failure?: MediaDeviceFailure) => {
    setError(
      failure === MediaDeviceFailure.PermissionDenied
        ? "Microphone permission denied. Allow mic access and try again."
        : "Could not access your microphone.",
    );
  }, []);

  // LiveKitRoom reports both connection failures and mic-publish failures here. A mic
  // failure was already explained by onMediaDeviceFailure (which fires first), so keep
  // that message. Anything else means the call cannot work: go back to the start screen
  // instead of leaving a dead session on screen.
  const onError = useCallback((e: Error) => {
    const failure = MediaDeviceFailure.getFailure(e);
    if (failure !== undefined && failure !== MediaDeviceFailure.Other) return;
    setError(e.message);
    setDetails(null);
  }, []);

  return (
    <main className="shell">
      <header className="header">
        <h1>GPT-Live voice agent</h1>
        <p className="muted">Full-duplex speech-to-speech with OpenAI <code>gpt-live-1</code>, served by LiveKit Agents.</p>
      </header>

      {details ? (
        <LiveKitRoom
          serverUrl={details.serverUrl}
          token={details.participantToken}
          connect
          audio // publish the microphone on connect
          video={false}
          onDisconnected={disconnect}
          onMediaDeviceFailure={onMediaDeviceFailure}
          onError={onError}
          className="room"
        >
          <VoiceSession />
          {/* Plays every remote audio track (i.e. the agent's voice). */}
          <RoomAudioRenderer />
        </LiveKitRoom>
      ) : (
        <section className="card idle">
          <p className="muted">Press start, allow microphone access, and just talk. You can interrupt at any time.</p>
          <button className="btn primary" onClick={connect} disabled={connecting}>
            {connecting ? "Connecting…" : "Start conversation"}
          </button>
        </section>
      )}

      {error && <p className="error" role="alert">{error}</p>}

      <footer className="footer muted">
        GPT-Live × LiveKit demo ·{" "}
        <a href="https://agents-playground.livekit.io" target="_blank" rel="noreferrer">
          or use the Agents Playground
        </a>
      </footer>
    </main>
  );
}
