/**
 * POST /api/token: mints a short-lived LiveKit access token for the browser.
 *
 * The API key/secret never leave the server. Every call creates a fresh random
 * room, so each browser session gets its own agent. If LIVEKIT_AGENT_NAME is
 * set, the token also carries an explicit agent dispatch (needed when the
 * Python worker registers with `agent_name=...`, which disables auto-dispatch).
 *
 * The grant is least-privilege: join this one room, publish the microphone only
 * (no camera/screen, no data messages), and subscribe to the agent's audio.
 *
 * SECURITY: every token starts a paid GPT-Live session. Without DEMO_PASSCODE this
 * route is open to anyone who can reach it (fine on localhost). With DEMO_PASSCODE set
 * it requires the unlock cookie (see lib/passcode.ts). Either way, add a rate limit
 * before exposing it publicly.
 */
import { NextResponse } from "next/server";
import {
  AccessToken,
  RoomAgentDispatch,
  RoomConfiguration,
  TrackSource,
} from "livekit-server-sdk";
import { isUnlocked } from "@/lib/passcode";
import type { ConnectionDetails } from "@/lib/types";

// Never cache: every response contains a unique, secret token.
export const dynamic = "force-dynamic";

const NO_STORE = { "Cache-Control": "no-store" };

export async function POST() {
  if (!(await isUnlocked())) {
    return NextResponse.json(
      { error: "Enter the demo passcode first." },
      { status: 401, headers: NO_STORE },
    );
  }

  const { LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_AGENT_NAME } =
    process.env;

  if (!LIVEKIT_URL || !LIVEKIT_API_KEY || !LIVEKIT_API_SECRET) {
    const missing = Object.entries({ LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET })
      .filter(([, value]) => !value)
      .map(([key]) => key);
    return NextResponse.json(
      {
        error: `Missing environment variable(s): ${missing.join(", ")}. ` +
          "Copy frontend/.env.example to frontend/.env.local and fill in your LiveKit credentials.",
      },
      { status: 500, headers: NO_STORE },
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
    serverUrl: LIVEKIT_URL,
    roomName,
    participantToken: await token.toJwt(),
    participantName,
  };
  return NextResponse.json(details, { headers: NO_STORE });
}
