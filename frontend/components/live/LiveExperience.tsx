"use client";

/**
 * The Live page: a hero with a start button, then the call itself.
 *
 * Fetches a token from our own API route, then mounts <LiveKitRoom>. Everything that
 * needs the room (agent state, visualizer, transcript) lives in <VoiceSession>, which
 * only renders once we are connected. The callbacks passed to <LiveKitRoom> are
 * stable (useCallback) so re-renders don't re-register room listeners.
 */
import { useCallback, useState } from "react";
import Link from "next/link";
import { LiveKitRoom, RoomAudioRenderer } from "@livekit/components-react";
import { MediaDeviceFailure } from "livekit-client";
import { AlertIcon, MicIcon } from "@/components/icons";
import type { ConnectionDetails } from "@/lib/types";
import { Orb } from "./Orb";
import { VoiceSession } from "./VoiceSession";
import styles from "./Live.module.css";

const FEATURES = [
  {
    title: "Full-duplex voice",
    body: "Speech in, speech out with GPT-Live. Interrupt, hesitate, change your mind.",
  },
  {
    title: "Real tools",
    body: "Ava checks availability and searches the web mid-conversation.",
  },
  {
    title: "Every call reviewed",
    body: "After you hang up, each call is transcribed, scored, and summarized.",
  },
];

/** The `error` string from a failed /api/token response body, if it has one. */
function tokenErrorMessage(body: unknown): string | null {
  if (typeof body !== "object" || body === null || !("error" in body)) return null;
  return typeof body.error === "string" ? body.error : null;
}

export function LiveExperience() {
  const [details, setDetails] = useState<ConnectionDetails | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connect = useCallback(async () => {
    setConnecting(true);
    setError(null);
    try {
      const res = await fetch("/api/token", { method: "POST", cache: "no-store" });
      // A crashed route or a proxy can answer with HTML: don't surface a JSON parse error.
      const body: unknown = await res.json().catch(() => null);
      if (!res.ok || !body) {
        throw new Error(tokenErrorMessage(body) ?? `Token request failed (HTTP ${res.status})`);
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

  const errorNotice = error && (
    <p className={`notice ${styles.error}`} role="alert">
      <AlertIcon />
      <span>{error}</span>
    </p>
  );

  if (details) {
    return (
      <main className="page">
        {errorNotice}
        <LiveKitRoom
          serverUrl={details.serverUrl}
          token={details.participantToken}
          connect
          audio // publish the microphone on connect
          video={false}
          onDisconnected={disconnect}
          onMediaDeviceFailure={onMediaDeviceFailure}
          onError={onError}
        >
          <VoiceSession />
          {/* Plays every remote audio track (i.e. Ava's voice). */}
          <RoomAudioRenderer />
        </LiveKitRoom>
      </main>
    );
  }

  return (
    <main className={`page ${styles.hero}`}>
      <p className="eyebrow">GPT-Live × LiveKit Agents</p>
      <h1 className={styles.title}>Talk to Ava.</h1>
      <p className={styles.lede}>
        A dining concierge you can simply talk to. Ask for a table tonight, check what&rsquo;s open
        nearby, or change your mind mid-sentence. She&rsquo;s listening even while she speaks.
      </p>

      <Orb state={connecting ? "connecting" : "idle"} />

      <div className={styles.start}>
        <button className="btn btn-primary btn-lg" onClick={connect} disabled={connecting}>
          <MicIcon />
          {connecting ? "Connecting…" : "Start conversation"}
        </button>
        <p className={styles.hint}>
          Uses your microphone. Afterwards, find the call in <Link href="/calls">Calls</Link>.
        </p>
      </div>

      {errorNotice}

      <ul className={styles.features}>
        {FEATURES.map((f) => (
          <li key={f.title}>
            <h2>{f.title}</h2>
            <p>{f.body}</p>
          </li>
        ))}
      </ul>
    </main>
  );
}
