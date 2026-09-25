/**
 * POST /api/token — mints a short-lived LiveKit access token for the browser.
 *
 * The API key/secret never leave the server. Every call creates a fresh random
 * room, so each browser session gets its own agent. If LIVEKIT_AGENT_NAME is
 * set, the token also carries an explicit agent dispatch (needed when the
 * Python worker registers with `agent_name=...`, which disables auto-dispatch).
 *
 * SECURITY: this route is unauthenticated. Anyone who can reach it gets a token,
 * and every token starts a paid GPT-Live session. That is fine on localhost; before
 * exposing it publicly, put your app's auth and a rate limit in front of it.
 * The grant is least-privilege: join this one room, publish the microphone only
 * (no camera/screen, no data messages), and subscribe to the agent's audio.
 */
import { NextResponse } from "next/server";
import {
  AccessToken,
  RoomAgentDispatch,
  RoomConfiguration,
  TrackSource,
} from "livekit-server-sdk";
import type { ConnectionDetails } from "@/lib/types";

// Never cache: every response contains a unique, secret token.
export const dynamic = "force-dynamic";

export async function POST() {
  const { LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_AGENT_NAME } =
    process.env;

  const missing = Object.entries({ LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET })
    .filter(([, value]) => !value)
    .map(([key]) => key);
  if (missing.length > 0) {
    return NextResponse.json(
      {
        error: `Missing environment variable(s): ${missing.join(", ")}. ` +
          "Copy frontend/.env.example to frontend/.env.local and fill in your LiveKit credentials.",
      },
      { status: 500 },
    );
  }

  const suffix = crypto.randomUUID().slice(0, 8);
  const roomName = `gpt-live-${suffix}`;
  const participantIdentity = `user-${suffix}`;
  const participantName = "You";

  // `ttl` only bounds how long the token can be used to *join*; LiveKit refreshes the
  // credentials of connected participants, so calls can outlast it.
  const token = new AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET, {
    identity: participantIdentity,
    name: participantName,
    ttl: "15m",
  });
  token.addGrant({
    room: roomName,
    roomJoin: true,
    canPublish: true,
    canPublishSources: [TrackSource.MICROPHONE],
    canPublishData: false,
    canSubscribe: true,
  });

  // Explicit dispatch: the agent named here joins as soon as the room is created.
  if (LIVEKIT_AGENT_NAME) {
    token.roomConfig = new RoomConfiguration({
      agents: [new RoomAgentDispatch({ agentName: LIVEKIT_AGENT_NAME })],
    });
  }

  const details: ConnectionDetails = {
    serverUrl: LIVEKIT_URL!,
    roomName,
    participantToken: await token.toJwt(),
    participantName,
  };
  return NextResponse.json(details, { headers: { "Cache-Control": "no-store" } });
}
