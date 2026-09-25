# Frontend: Live, Calls and Call detail

The web app in [`frontend/`](../frontend) is a Next.js (App Router) app with three screens: talk
to the agent, browse past calls, and read one call's scorecard and transcript. It's deliberately
small: no UI kit, no chart library, no client-side state store. Plain CSS with design tokens,
inline SVG icons, and a hand-drawn sparkline. Light and dark follow the OS, the layout works
down to phone width, and motion is off under `prefers-reduced-motion`.

Setup, scripts and the environment table are in [`frontend/README.md`](../frontend/README.md).
This page covers how it's put together and why.

> Related: [`call-analyzer.md`](call-analyzer.md) (where the Calls data comes from),
> [`architecture.md`](architecture.md#21-browser-client-frontend) (where the app sits in the
> system), [`why-livekit.md`](why-livekit.md) (the client SDKs).

---

## Screens

| Route | Screen | Data |
|---|---|---|
| `/` | **Live**: an agent-state orb with an audio visualizer, mic toggle, end call, and a live transcript as chat bubbles, with the caller's interim text shown as it's recognized. | LiveKit room (WebRTC audio + `lk.transcription` text streams) |
| `/calls` | **Calls**: newest first, with date, duration, caller intent, summary, outcome, overall score and analysis status. "Load more" pages with the analyzer's keyset cursor. | Call Analyzer, `GET /v1/calls` |
| `/calls/[id]` | **Call detail**: overall score and outcome, the nine-dimension scorecard with rationale and evidence quotes (each links to its turn), flags, exact metrics, a caller-sentiment sparkline, and the full transcript with per-turn transcription confidence, interruptions and tool calls. **Re-analyze** re-runs grading, and the page updates itself while it runs. | Call Analyzer, `GET /v1/calls/{id}` |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/ui-live-dark.png">
  <img alt="Live view: agent orb, controls and live transcript" src="images/ui-live-light.png" width="720">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/ui-call-detail-dark.png">
  <img alt="Call detail view: overall score, outcome, scorecard and transcript" src="images/ui-call-detail-light.png" width="720">
</picture>

<p>
  <img alt="Call detail on a phone" src="images/ui-call-detail-mobile.png" width="280">
  <img alt="Calls list on a phone, dark mode" src="images/ui-calls-mobile-dark.png" width="280">
</p>

---

## The server/client boundary

The rule is simple: **the browser never talks to the analyzer, and never sees a secret.**

- **Server Components fetch.** `/calls` and `/calls/[id]` are Server Components that call the
  analyzer through [`lib/analyzer.ts`](../frontend/lib/analyzer.ts), which starts with
  `import "server-only"`, so the build fails if a Client Component ever imports it. Every request
  has an 8 s timeout and `cache: "no-store"`, and every failure becomes a friendly
  `AnalyzerError` ("Check that CALL_ANALYZER_TOKEN matches on both sides") while details go to
  the server log only.
- **Server Actions mutate.** "Load more" and "Re-analyze" are Server Actions in
  [`app/actions.ts`](../frontend/app/actions.ts). Each one is a public POST endpoint, so each
  re-checks the passcode gate and validates its arguments (cursor format, call-id alphabet)
  before it touches the analyzer. There is no generic proxy route, so the browser can't reach
  analyzer paths the app doesn't use.
- **No `NEXT_PUBLIC_` variables.** `CALL_ANALYZER_TOKEN`, the LiveKit API secret and
  `DEMO_PASSCODE` are all read server-side.
- **Client Components are leaves**: the LiveKit room and live transcript, the call list's
  "Load more", the re-analyze button, the unlock form, local-time formatting, the nav, and
  `AnalysisPoller`, which calls `router.refresh()` while an analysis
  is `pending` or `running` (backing off from 2.5 s to 30 s, paused in background tabs, giving up
  after three minutes of visible time).

The benefit is a small attack surface and nothing to keep in sync between a REST layer and the
pages. The cost is that the Calls pages need a Node server (not a static export), and every
page view is a round trip to the analyzer.

---

## Live transcript via `lk.transcription`

The agent doesn't send transcript messages of its own. LiveKit Agents publishes both sides of
the conversation as text streams on the `lk.transcription` topic: the agent's speech, synced to
its audio, and the caller's speech, which GPT-Live transcribes itself (no separate STT). The
browser reads them with `useTranscriptions()` from `@livekit/components-react`
([`components/live/Transcript.tsx`](../frontend/components/live/Transcript.tsx)):

- Updates that share an `lk.segment_id` merge into one bubble, so the caller's interim text is
  replaced in place and the agent's reply grows word by word.
- A caller segment with `lk.transcription_final = "false"` is shown as *transcribing…*. The
  agent's final flag rides on the stream trailer, which the hook doesn't surface, so for the
  agent the UI uses the voice-assistant state (*speaking…*) instead.
- Bubbles stay in first-seen order rather than being sorted by timestamp, because the hook
  replaces a segment's stream info on every update and a long caller segment would otherwise
  jump below the reply that started after it.

What the caller sees live and what the analyzer grades later both come from GPT-Live's own
transcription: the live view shows the streams as they arrive, and the call record is built from
the final messages in `session.history` when the session ends.

---

## Showing transcription quality

Speech recognition errors look like agent errors unless you can see them. The Call detail
transcript puts a confidence badge on every turn that has one (in practice, caller turns):

| Badge | Confidence | Why this threshold |
|---|---|---|
| high | ≥ 85% | Treat as what the caller said. |
| medium | 60-84% | Probably right; worth a glance if the agent's reply seems off. |
| **Low confidence · NN%** | < 60% | Matches the analyzer's `LOW_CONFIDENCE_THRESHOLD` (`analyzer/call_analyzer/metrics.py`), which counts these turns in the `low_confidence_turns` metric. The judge's rubric tells it that such turns may contain recognition errors. |

The Metrics row repeats the call-level view: mean transcript confidence and the count of
low-confidence turns, highlighted when non-zero. Interrupted agent turns carry an interruption
marker, and tool calls appear as chips between turns, in time order.

Scores follow the same "don't hide the instrument" idea. Every dimension is 1-5, higher is
better, **except customer frustration**, which the scorecard colors inverted and labels "Lower is
better". When `analyzer.provider` is `heuristic`, the detail page shows an **Offline heuristic
analysis** badge and the list a **Heuristic** badge, because those scores come from keyword
rules, not an LLM ([`call-analyzer.md` §9](call-analyzer.md#9-providers-an-llm-judge-or-an-offline-heuristic)).

![Transcript with confidence badges, an interruption and a tool call](images/ui-call-detail-transcript-light.png)

---

## `DEMO_PASSCODE`, and why it isn't enough

Every `POST /api/token` dispatches the agent and starts a **paid** GPT-Live session, and the
Calls pages show real callers' transcripts. On localhost that's fine. On a public URL it isn't.

Setting `DEMO_PASSCODE` puts a gate in front of both
([`lib/passcode.ts`](../frontend/lib/passcode.ts)):

- The Live and Calls pages redirect to `/unlock`; the token route returns 401 and every Server
  Action returns an error until the visitor enters the passcode.
- A correct passcode sets an `httpOnly`, `SameSite=Strict` cookie (`Secure` in production)
  holding `<issued_at>.<HMAC-SHA256(key = passcode, label + issued_at)>`. It proves the visitor
  knew the passcode without storing it, the **server** rejects it after 12 hours (so a copied
  cookie stops working even if a browser ignores `maxAge`), and changing `DEMO_PASSCODE` logs
  everyone out.
- Comparisons are constant-time, the post-unlock redirect only accepts same-site paths, and a
  wrong guess waits 500 ms.

It's a speed bump, not auth: no accounts, and anyone with the passcode gets in. And the 500 ms
delay slows a *sequential* guesser only; parallel requests aren't limited at all, and nothing
stops someone with the passcode from starting sessions in a loop. **A public deployment still
needs a rate limit at the edge** (your host's firewall or WAF rules, or your proxy), at least on
`POST /api/token` and `POST /unlock`, plus a session-length cap on the agent side. Rate limiting
in the app itself would need shared state across serverless instances; the edge already has it.
For anything beyond a demo, put real authentication in front of the app instead.

---

## Adapting it

- **Different client.** The agent doesn't depend on this app. Any LiveKit client SDK (Swift,
  Kotlin, Flutter, React Native, Unity) or LiveKit's hosted Agents Playground works against the
  same worker; only the token route is required.
- **Your own design system.** The screens are plain components over two data shapes
  (`lib/types.ts` mirrors the analyzer's `CallRecord` / `Analysis` v1). Swap the CSS modules for
  your components; the data layer stays.
- **Embed the scorecard elsewhere.** The analyzer is a plain JSON API. An internal admin tool or
  a support dashboard can read the same endpoints server-side with the same token.
