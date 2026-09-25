# Frontend: web client for the GPT-Live voice agent

A small Next.js (App Router) app for talking to the Python agent in `../agent`. It is intentionally tiny:

| File | What it does |
| --- | --- |
| `app/api/token/route.ts` | `POST /api/token`: mints a LiveKit access token for a fresh random room (join that room, publish the microphone only, subscribe). It runs on the server only, so your API secret stays there. |
| `app/page.tsx` | Start/stop button, error display, and `<LiveKitRoom>` + `<RoomAudioRenderer>` (plays the agent's voice). |
| `components/VoiceSession.tsx` | Agent state (`useVoiceAssistant`), `BarVisualizer`, mic toggle, end button. |
| `components/Transcript.tsx` | Live transcript from `useTranscriptions()` (the `lk.transcription` text streams that livekit-agents publishes). |

## Setup

Requires Node 20+.

```bash
cd frontend
npm install
cp .env.example .env.local   # then fill in LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
npm run dev                  # http://localhost:3000
```

Start the agent too (see the root README), e.g. `uv run voice-agent dev`. Then open the page, click **Start conversation**, and allow microphone access.

Use the same LiveKit project for the agent and this app. The worker in `agent/` registers as `gpt-live-agent` (explicit dispatch), and `.env.example` already sets `LIVEKIT_AGENT_NAME=gpt-live-agent`, so the token includes a `RoomConfiguration` agent dispatch for it. If you run the worker with automatic dispatch (empty `LIVEKIT_AGENT_NAME` on the agent side), clear it here too.

Other scripts: `npm run lint`, `npm run typecheck`, `npm run build && npm start`.

## Before you deploy it publicly

`/api/token` is **unauthenticated**: anyone who can reach it gets a token, and every token dispatches the agent and starts a paid GPT-Live session. That is fine on `localhost`. Before putting the app on a public URL, put your own auth (or at least a shared secret) and a rate limit in front of the route, and cap session length on the agent side.

## Alternative: no frontend at all

LiveKit's hosted **[Agents Playground](https://agents-playground.livekit.io)** works as a client too. Connect it to your LiveKit Cloud project (or paste a URL and token) and talk to the same agent.
