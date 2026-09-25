# Frontend: GPT-Live Concierge web app

A Next.js (App Router) app with three screens:

| Route | Screen |
| --- | --- |
| `/` | **Live**: talk to Ava. Agent-state orb with a live audio visualizer, mic toggle, end call, and a real-time transcript as chat bubbles (interim caller text shown as it's recognized). |
| `/calls` | **Calls**: history from the [Call Analyzer](../analyzer): date, duration, caller intent, summary, outcome, overall score, and analysis status. Paginated with "Load more". |
| `/calls/[id]` | **Call detail**: overall score and outcome, a 9-dimension scorecard with rationale and evidence quotes (click one to jump to that turn), flags, exact metrics, a caller-sentiment sparkline, and the full transcript with per-turn speech-to-text confidence, interruptions, and tool calls. **Re-analyze** re-runs the analysis, and the page updates itself while it runs. |

Light and dark mode follow the OS setting. The layout works down to phone width, and motion is switched off under `prefers-reduced-motion`. There are no UI or chart libraries: styling is plain CSS (tokens in `app/globals.css`, CSS modules next to components), icons are inline SVG, and the sparkline is a small hand-drawn SVG.

![Call detail](../docs/images/ui-call-detail-light.png)

## Setup

Requires Node 20+.

```bash
cd frontend
npm install
cp .env.example .env.local   # then fill in the values (see below)
npm run dev                  # http://localhost:3000
```

**Live** needs the agent running (see the root README), e.g. `uv run voice-agent dev`. Open the page, click **Start conversation**, and allow microphone access. Use the same LiveKit project for the agent and this app. The worker in `agent/` registers as `gpt-live-agent` (explicit dispatch), so the token includes a `RoomConfiguration` agent dispatch for that name. If you run the worker with automatic dispatch, clear `LIVEKIT_AGENT_NAME` here too.

**Calls** needs the Call Analyzer. You don't need any LLM key to try it: seeding loads six demo calls and scores them with the offline heuristic provider.

```bash
cd analyzer
uv sync
uv run python -m call_analyzer seed                                    # demo calls
CALL_ANALYZER_TOKEN=change-me-shared-secret uv run python -m call_analyzer serve   # :8080
```

Then set `CALL_ANALYZER_URL=http://localhost:8080` and the same `CALL_ANALYZER_TOKEN` in `frontend/.env.local`. After that, every call you make on the Live page shows up under Calls once the agent posts it to the analyzer.

Other scripts: `npm run lint`, `npm run typecheck`, `npm run build && npm start`.

## Environment

All variables are server-only. None use the `NEXT_PUBLIC_` prefix, so none reach the browser.

| Variable | Used by | Notes |
| --- | --- | --- |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | `app/api/token/route.ts` | Mints a token for a fresh random room. The token can join only that room, publish only the microphone, and subscribe. |
| `LIVEKIT_AGENT_NAME` | token route | Explicit agent dispatch; must match the worker. |
| `CALL_ANALYZER_URL` | `lib/analyzer.ts` | Base URL of the analyzer (a path prefix is fine). |
| `CALL_ANALYZER_TOKEN` | `lib/analyzer.ts` | Bearer token for the analyzer's `/v1` API. |
| `DEMO_PASSCODE` | `lib/passcode.ts` | Optional passcode gate; see below. |

## How it's wired

| Path | What it does |
| --- | --- |
| `app/api/token/route.ts` | `POST /api/token`: returns LiveKit connection details, `Cache-Control: no-store`. |
| `components/live/*` | `LiveExperience` (hero, token fetch, `<LiveKitRoom>` with stable callbacks and mic-failure handling), `VoiceSession` (`useVoiceAssistant`, `BarVisualizer`, `TrackToggle`, `DisconnectButton`), `Transcript` (`useTranscriptions`, `role="log"`), `Orb`. |
| `lib/analyzer.ts` | Server-only analyzer client (`import "server-only"`). It uses an 8 s timeout (`AbortSignal.timeout`), `cache: "no-store"`, and minimal runtime shape checks. Every failure becomes a friendly `AnalyzerError`, while details go to the server log only. Call ids are checked against the analyzer's own call-id alphabet before they're put in a URL. |
| `app/calls/**` | Server Components that call the analyzer. They include `loading.tsx` skeletons, empty and error states, and `not-found`. |
| `app/actions.ts` | Server Actions: `loadMoreCalls` (pagination), `reanalyze` (`POST /v1/calls/{id}/analyze`, then `refresh()`), `unlock`. The browser only ever talks to these and never to the analyzer. |
| `components/calls/AnalysisPoller.tsx` | While an analysis is `pending` or `running`, calls `router.refresh()` with exponential backoff (2.5 s up to 30 s). It pauses in background tabs and gives up after 3 minutes of visible time, then tells you to refresh. |
| `lib/types.ts` | TypeScript mirror of the analyzer's CallRecord / Analysis v1 contracts. |

Scores are 1–5 where higher is better, except **customer frustration**, where 1 means no frustration. The scorecard colors that dimension inverted and labels it "Lower is better". When `analyzer.provider` is `heuristic`, the detail page shows an **Offline heuristic analysis** badge (and the Calls list a **Heuristic** badge), since those scores come from keyword rules, not an LLM.

## Passcode gate (`DEMO_PASSCODE`)

- **Unset** (default): open access, fine for localhost.
- **Set**: the Live and Calls pages redirect to `/unlock`, and `POST /api/token` and every Server Action return an error until the visitor enters the passcode. A correct passcode sets an `httpOnly`, `SameSite=Strict` cookie (`Secure` in production) for 12 hours. The cookie holds `<issued_at>.<HMAC-SHA256(key = passcode, label + issued_at)>`, so it proves the visitor knew the passcode without storing it, and the server itself rejects it after 12 hours. Changing `DEMO_PASSCODE` logs everyone out. Comparisons are constant-time, the post-unlock redirect only accepts same-site paths, and a wrong guess waits 500 ms (which slows a sequential guesser, not parallel requests: that needs the rate limit below).

This gate slows people down but isn't user auth: there are no accounts, and anyone with the passcode gets in.

## Before you deploy it publicly

Every call to `/api/token` dispatches the agent and starts a **paid** GPT-Live session, and the Calls pages show real caller transcripts. On a public URL:

1. Set `DEMO_PASSCODE` (or put real auth in front of the app).
2. Add a rate limit at your host or proxy, especially for `POST /api/token` and `POST /unlock`.
3. Cap session length on the agent side.
4. Keep the analyzer private: only this app's server should reach it. The frontend never exposes `CALL_ANALYZER_TOKEN` or proxies arbitrary analyzer paths.

## Alternative: no frontend at all

LiveKit's hosted **[Agents Playground](https://agents-playground.livekit.io)** works as a client too. Connect it to your LiveKit Cloud project (or paste a URL and token) and talk to the same agent.
