# GPT-Live × LiveKit: a reference voice agent

An end-to-end, production-shaped voice agent built on **OpenAI GPT-Live** (`gpt-live-1`) and
**LiveKit Agents**. It has a **modular prompt system**, two real tools (**web search** and
**restaurant availability**), and a **change-aware eval system** that runs paid evals only when a
change could actually alter the agent's behavior.

It is meant to be read as much as run. Every component has a written reason for existing, its
benefits and drawbacks, and the alternatives we turned down, so you can lift the parts you want
into your own voice agent.

> **Status:** reference demo. Everything runs offline without keys (tests, prompt rendering, eval
> planning, dry runs). A live call needs your own LiveKit project and an OpenAI key with GPT-Live
> access. All keys in this repo are placeholders.

---

## Why this exists

Voice agents used to be built as a relay race: detect speech, transcribe it, send the text to an
LLM, synthesize the reply, and hope the turn detector guessed right about when the caller had
finished. The result works, but it feels like talking to a walkie-talkie.

GPT-Live changes the shape of the problem. It is a **full-duplex** voice model: it listens and
speaks at the same time and decides turn-taking itself. It also **delegates** reasoning and tool
calls to a separate backend "brain" model. That is a real step forward for naturalness. It also
brings new constraints that shape everything around it:

- **The voice model's instructions are immutable once the session starts.** Your prompt has to be
  right before the call connects, which argues for composing it from versioned modules.
- **There are two brains, each with its own instructions.** The voice model needs to know how to
  *talk*; the backend model needs to know how to *decide and call tools*. Those are different
  prompts.
- **You can't evaluate it as plain text.** LiveKit refuses to run a duplex model in text
  simulation, and audio evals are slow and cost real money. You want to run them only when you
  have to.

This repo is one opinionated answer to each of those constraints.

---

## The architecture in one picture

```mermaid
flowchart LR
    User["Caller<br/>(browser / mobile / phone)"] <-->|"WebRTC / SIP"| SFU["LiveKit SFU<br/>(Cloud or self-hosted)"]
    SFU <-->|"WebRTC"| Worker["Agent worker<br/>LiveKit Agents (Python)"]
    Worker <-->|"WebSocket<br/>/v1/live/sessions"| Voice["GPT-Live voice model<br/>full-duplex, speaks + listens"]
    Voice -->|"delegation = responses"| Brain["Backend Responses model<br/>reasoning + tool choice"]
    Brain <--> WS["web_search<br/>(runs inside OpenAI)"]
    Brain <-->|"function call via worker"| Tool["check_restaurant_availability"]
    Tool --> Provider["ReservationProvider<br/>mock | OpenTable"]
    Prompts["prompts/manifest.yaml<br/>+ modules/*.md"] -->|"composed once per session"| Worker
```

1. The **browser** gets a token from the Next.js app and joins a LiveKit room over WebRTC.
2. LiveKit **dispatches** the `gpt-live-agent` worker into the room.
3. The worker **composes the prompts** for the session: voice instructions for GPT-Live, backend
   instructions for the brain, and the tool list, then fingerprints them.
4. **GPT-Live** holds the conversation. When the caller asks for something that needs thought or
   data, it hands the work to the **backend model**, which calls `web_search` (inside OpenAI) or
   `check_restaurant_availability` (in our worker). GPT-Live then speaks the result in its own words.

A full walkthrough of one call ("a table for four at Nopa, Friday at seven") lives in
[`docs/architecture.md`](docs/architecture.md#4-request-walkthrough-a-table-for-four-at-nopa-friday-at-seven).

---

## What we chose, and what it costs

| Decision | Why | Benefit | Drawback we accept |
|---|---|---|---|
| **GPT-Live (full-duplex S2S)** instead of an STT→LLM→TTS cascade | A dining concierge is a *conversation*: people think aloud, interrupt, and change their minds. | Natural overlap, barge-in and backchannels with no VAD, STT, TTS or turn-detector tuning. | Less control over exact wording and turn-taking; per-minute voice pricing plus backend tokens. |
| **GPT-Live** instead of half-duplex realtime (`gpt-realtime`) | Tool calls take seconds, and delegation lets the voice keep the caller company meanwhile. | Reasoning model and effort are chosen separately from the voice. | Voice instructions can't change mid-session; appends are capped at about 500 tokens. |
| **`responses` delegation** instead of `client` | Our tools are normal `@function_tool`s plus OpenAI's hosted web search. | LiveKit's tool machinery works unchanged; tools can change mid-session. | The backend model is OpenAI's; you don't orchestrate the reasoning loop yourself. |
| **LiveKit Agents** instead of a raw WebSocket or a hosted platform | Production voice needs WebRTC, dispatch, telephony and observability, not just a model socket. | Network resilience, worker load-balancing, SIP, client SDKs, first-class `DuplexModel` support. Open source and self-hostable. | Another piece of infrastructure to learn and run; the framework can lag brand-new provider features. |
| **Modular prompts composed per session** | Voice instructions are immutable after start, and two brains need two prompts. | Reviewable modules, strict validation, fingerprinted versions attached to every call. | One more layer of indirection than a string literal. |
| **Tool registry + provider abstraction** | Tools should be swappable and testable without a live call. | Deterministic mock for demos and evals; OpenTable (or Resy, SevenRooms…) behind one interface. | The OpenTable client is illustrative: its API is partner-gated. |
| **Three-tier, change-aware evals** | Audio evals are slow and costly, and most PRs don't change behavior. | Free unit tests always; cheap backend-brain evals when prompts or tools change; full voice evals only when voice behavior could change. | Voice evals rely on TTS-synthesized callers, which are cleaner than real audio. |

Longer answers, with diagrams:

- [**Voice agent architectures**](docs/voice-agent-architectures.md): cascaded, half-duplex,
  full-duplex, hybrids, transports and hosting options, a decision matrix, and when **not** to use
  this design.
- [**Why LiveKit**](docs/why-livekit.md): what it gives you, what it costs, and the alternatives.
- [**GPT-Live primer**](docs/gpt-live-primer.md): duplex, delegation, append channels,
  capabilities, and how to use it in your own agent.

---

## Quickstart

**Prerequisites:** Python 3.10+, [uv](https://docs.astral.sh/uv/), Node 20+, a
[LiveKit Cloud](https://cloud.livekit.io) project (or `livekit-server --dev`), and an OpenAI API
key with GPT-Live access.

```bash
git clone <this repo> && cd gpt-live-demo
uv sync                                   # Python deps (agent + evals + dev tools)
cp .env.example .env                      # fill in LIVEKIT_* and OPENAI_API_KEY

# Free, offline, no keys:
uv run pytest -q                          # composer, tools, providers, eval planner
uv run python -m voice_agent.prompts render concierge     # see the exact prompts
uv run python -m evals run --tier brain --dry-run          # what an eval run would do + cost estimate

# Talk to it:
uv run voice-agent console                # in your terminal, with your mic
# ...or run it as a worker and use the web client:
uv run voice-agent dev                    # (or: lk agent dev)
cd frontend && npm install && cp .env.example .env.local && npm run dev   # http://localhost:3000
```

Step-by-step setup, deployment notes and troubleshooting are in
[`docs/getting-started.md`](docs/getting-started.md).

---

## Modular prompts

Prompts are **data, not code**. A manifest assembles small markdown modules into a *profile*, with
separate lists for the two brains:

```yaml
# prompts/manifest.yaml
profiles:
  concierge:
    voice:                       # -> GPT-Live voice instructions (immutable for the session)
      - core/identity
      - core/voice_style
      - core/guardrails
      - skills/restaurant_reservations.voice
      - skills/web_search.voice
    backend:                     # -> backend Responses model instructions
      - backend/tool_policy
      - skills/restaurant_reservations.backend
      - skills/web_search.backend
    tools: [web_search, check_restaurant_availability]
```

The composer is strict:
- Unknown variables, modules targeted at the wrong brain, and skills whose tools aren't enabled
  all fail in CI, not on a call.
- Runtime values such as `today` are injected at session start but left out of the fingerprint,
  so the fingerprint identifies the *prompt version*, not the day.
- Each call publishes its prompt fingerprint as LiveKit participant attributes, so any
  conversation can be traced back to the exact prompts that produced it.

→ [`docs/modular-prompts.md`](docs/modular-prompts.md)

## Tools

| Tool | Kind | Runs where | Notes |
|---|---|---|---|
| `web_search` | OpenAI provider tool (`WebSearch`) | Inside OpenAI, on the backend model | No search API key, no code of ours in the loop. |
| `check_restaurant_availability` | LiveKit `@function_tool` | In the agent worker | Validates arguments, **never books**, returns compact JSON the voice can speak. Backed by a deterministic mock or an OpenTable partner-API client. |

Adding your own tool takes one function, one registry entry, a voice module and a backend module,
one line in the manifest, a unit test, and an eval suite. The step-by-step tutorial is in
[`docs/tools.md`](docs/tools.md#10-tutorial-add-your-own-custom-tool).

## Evals that only run when they matter

```mermaid
flowchart LR
    PR["Pull request"] --> Plan["plan<br/>semantic diff of base vs head"]
    Plan -->|"prompts, tools or config changed"| Brain["brain tier<br/>backend model via Responses API<br/>cheap, text"]
    Brain -->|"voice behavior may have changed"| Voice["voice tier<br/>real GPT-Live session<br/>TTS-driven audio"]
    Plan -->|"docs, frontend, whitespace, comments"| Skip["no paid evals"]
    Voice --> Report["sticky PR comment:<br/>what ran, what was skipped, and why"]
    Brain --> Report
```

The planner doesn't look at file paths. It renders the prompts and tool schemas for **both** the
base and head commits and compares *behavioral fingerprints*:

- **Voice prompt** wording changed → voice tier.
- **Backend prompt** or a model-visible **tool schema** changed → brain + voice.
- A tool's **implementation** changed → brain tier, and only for the suites that use that tool.
- Reflowed paragraphs, HTML comments, Python comments or formatting, docs or frontend only →
  **nothing runs**.

Evals themselves use deterministic assertions first, then an LLM judge, over multiple trials with
pass^k thresholds, under a hard cost budget. In GitHub Actions they run behind a protected
environment, are skipped on fork PRs, and can be forced or skipped with the `evals:full` /
`evals:skip` labels.

→ [`docs/evals.md`](docs/evals.md) · [`evals/README.md`](evals/README.md)

---

## Repository map

```
agent/voice_agent/        Python LiveKit worker
  main.py                   AgentServer entrypoint: compose prompts -> tools -> GPT-Live -> AgentSession
  model.py                  GPTLiveModel + backend (responses delegation) options
  agent.py                  VoiceAgent: instructions, tools, greeting
  config.py, runtime.py     settings (.env) and runtime prompt variables (today, timezone)
  prompts/                  PromptComposer, PromptBundle, CLI (`python -m voice_agent.prompts`)
  tools/                    registry, web_search, restaurants/ (mock + OpenTable providers)
agent/tests/              unit tests (offline)
prompts/                  manifest.yaml + modules/**.md  (the prompt *data*)
evals/                    suites, brain/voice runners, judge, change-impact planner, tests
frontend/                 Next.js client + LiveKit token route
docs/                     architecture, trade-offs, primer, prompts, tools, evals, getting started
.github/workflows/        ci.yml (lint, tests, prompt render, frontend) and evals.yml
```

## Documentation

| Doc | Read it for |
|---|---|
| [Getting started](docs/getting-started.md) | Running it locally, deploying, troubleshooting, and adopting GPT-Live in your own agent |
| [Architecture](docs/architecture.md) | Components, session lifecycle, a request walkthrough, module map, configuration |
| [Voice agent architectures](docs/voice-agent-architectures.md) | Every architecture we considered, with trade-offs and a decision matrix |
| [Why LiveKit](docs/why-livekit.md) | What the media and orchestration layer buys you, and what it costs |
| [GPT-Live primer](docs/gpt-live-primer.md) | How GPT-Live works and how to use it |
| [Modular prompts](docs/modular-prompts.md) | The prompt workflow: manifest, modules, fingerprints, writing guidance |
| [Tools](docs/tools.md) | Where tools run, the registry, providers, and an add-a-tool tutorial |
| [Evals](docs/evals.md) | Eval tiers, suites, change-impact detection, CI workflows, best practices |

## Configuration

All configuration is environment variables. See [`.env.example`](.env.example) (agent) and
[`frontend/.env.example`](frontend/.env.example) (web client); the full reference is in
[`docs/architecture.md`](docs/architecture.md#7-configuration-reference). The main settings:

| Variable | Default | Purpose |
|---|---|---|
| `GPT_LIVE_MODEL` / `GPT_LIVE_VOICE` | `gpt-live-1` / `marin` | Voice model and voice |
| `GPT_LIVE_BACKEND_MODEL` | `gpt-5.6-luna` | Backend model GPT-Live delegates to |
| `GPT_LIVE_BACKEND_REASONING_EFFORT` | `low` | Latency beats depth on a call |
| `AGENT_PROFILE` | `concierge` | Which profile in `prompts/manifest.yaml` to run |
| `RESTAURANT_PROVIDER` | `mock` | `mock` (deterministic) or `opentable` (partner credentials) |
| `LIVEKIT_AGENT_NAME` | `gpt-live-agent` | Explicit-dispatch name; the frontend requests the same one |

## Caveats

- **GPT-Live is a young API.** Model slugs, defaults and pricing (around $0.05 per voice-minute at
  the time of writing, plus backend tokens) may change. Check OpenAI's current documentation.
- **OpenTable's API is partner-gated.** The client shows the real integration concerns (OAuth2
  client credentials, token caching, timeouts, error mapping), but its endpoints and fields are
  placeholders to adapt once you have partner access. The mock provider is the default.
- **The evals are designed and tested offline but haven't been run against live models** in this
  repo. Expect to tune voice-runner timing and pricing estimates on the first real run.

## License

[MIT](LICENSE)
