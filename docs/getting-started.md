# Getting started

From a fresh clone to talking with the agent: first in your terminal, then in the browser. At the
end there's a checklist for adopting GPT-Live in your *own* LiveKit agent.

> Related: [`architecture.md`](architecture.md) (what you're running),
> [`modular-prompts.md`](modular-prompts.md), [`tools.md`](tools.md), [`evals.md`](evals.md).

---

## Prerequisites

| Need | Version / notes |
|---|---|
| Python | 3.10+ (developed on 3.11). |
| [uv](https://docs.astral.sh/uv/) | Manages the virtualenv and lockfile. Every command below is `uv run ...`. |
| Node.js | 20+, only for the web frontend. |
| A LiveKit server | A [LiveKit Cloud](https://cloud.livekit.io) project (free tier is fine), **or** a local `livekit-server --dev`. Not needed for console mode. |
| LiveKit CLI (`lk`) | Optional but recommended. LiveKit Agents 1.8 steers the dev loop toward `lk agent ...`, and it's how you deploy to LiveKit Cloud. |
| OpenAI API key | With access to **GPT-Live** (`gpt-live-1`) and the backend Responses model (`gpt-5.6-luna` by default). |
| A microphone | For console mode and the browser. Headphones help, since speakers feeding back into the mic confuse any full-duplex model. |

Cost: every connected session bills GPT-Live voice time (public pricing at time of writing is on
the order of $0.05/min, billed per second) plus backend tokens and web-search calls. Unit tests
and the prompt CLI cost nothing. See [primer §5](gpt-live-primer.md#pricing).

---

## 1. Clone and install

```bash
git clone <this-repo-url> gpt-live-demo
cd gpt-live-demo
uv sync                     # creates .venv, installs the agent + dev tools (pytest, ruff, mypy)
```

The eval runners need the `openai` SDK, which lives in a separate dependency group:
`uv sync --group evals`. See [`evals.md`](evals.md).

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env`. The placeholders look like `your_livekit_api_key` / `sk-your-openai-api-key`.

```dotenv
# LiveKit Cloud: Settings -> Keys. For `livekit-server --dev`: ws://localhost:7880, devkey, secret
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret

OPENAI_API_KEY=sk-your-openai-api-key
```

Everything else has working defaults: profile `concierge`, voice `marin`, the mock restaurant
provider, low reasoning effort, agent name `gpt-live-agent`. Every variable is listed in
[`architecture.md` §7](architecture.md#7-configuration-reference).

`.env` is gitignored. Never commit real keys.

## 3. Look at the prompts (free, no keys)

```bash
uv run python -m voice_agent.prompts list
uv run python -m voice_agent.prompts render concierge            # voice + backend + greeting
uv run python -m voice_agent.prompts render concierge --json     # full bundle incl. fingerprint
```

You should see `fingerprint=3a38d1b337b0` for the unmodified `concierge` profile. That is exactly
the text GPT-Live and the backend model will receive, except that `<runtime:today>` is filled in
at session start. See [`modular-prompts.md`](modular-prompts.md).

## 4. Run the tests (free, no keys, no network)

```bash
uv run pytest               # agent/tests + evals/tests
uv run ruff check .
```

The agent tests cover prompt composition and validation, the tool registry, the restaurant tool's
validation and mock data, and the OpenTable client against a mocked HTTP transport.

## 5. Talk to it in your terminal (console mode)

```bash
uv run voice-agent console
```

This runs the agent locally against your default mic and speakers. No LiveKit server or room is
needed, only `OPENAI_API_KEY`. Try this:

> "Hi! Is there a table for four at Nopa this Friday at seven?"
>
> "What about Zuni Cafe?"
>
> "Is the Ferry Building open on Sunday?"

Things to notice:

- the greeting;
- the spoken filler while a lookup runs;
- times said as words, not "19:00";
- the agent never claims to have booked anything;
- the `composed prompts` log line with `prompt_fingerprint`.

`uv run voice-agent ...` uses LiveKit's built-in Python CLI, which is **deprecated in 1.8** and
prints a deprecation warning. It still works. The forward-looking equivalent is the LiveKit CLI
(`lk agent console`, `lk agent dev`, `lk agent start`), which discovers the module-level `server`
in `agent/voice_agent/main.py`. Check `lk agent --help` for how your CLI version takes the
entrypoint path.

## 6. Run it as a worker and talk from the browser

### 6a. Start the worker

```bash
uv run voice-agent dev      # or: lk agent dev
```

The worker connects to `LIVEKIT_URL` and registers as **`gpt-live-agent`**. That enables
*explicit dispatch*: LiveKit only sends it rooms that ask for that agent by name. (In 1.8 the
Python CLI's `dev` no longer hot-reloads; use `lk agent dev` if you want reload.)

A third, CLI-free way to run the same worker:
`uv run python -m livekit.agents start agent/voice_agent/main.py --dev`.

### 6b. Start the frontend

```bash
cd frontend
npm install
cp .env.example .env.local
```

Edit `frontend/.env.local`. Use the **same LiveKit project** as the agent, and set the agent name
explicitly:

```dotenv
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret
LIVEKIT_AGENT_NAME=gpt-live-agent
```

`frontend/.env.example` already ships with `LIVEKIT_AGENT_NAME=gpt-live-agent`, matching the
worker's default. If you switch the worker to automatic dispatch (`LIVEKIT_AGENT_NAME=` empty),
clear it on the frontend too. The two settings must agree.

```bash
npm run dev                 # http://localhost:3000
```

Click **Start conversation** and allow the microphone. The transcript fills in live from the
`lk.transcription` streams: GPT-Live transcribes the caller itself, and there's no separate STT.

### 6c. Or skip the frontend: Agents Playground

LiveKit's hosted [Agents Playground](https://agents-playground.livekit.io) can connect to your
Cloud project and talk to the running worker. Because this worker uses explicit dispatch, the
Playground session must request the agent `gpt-live-agent`. If your Playground version has no
agent-name setting, run the worker with `LIVEKIT_AGENT_NAME=` (empty) while testing, so it joins
every new room.

### 6d. Local LiveKit server instead of Cloud

```bash
livekit-server --dev        # ws://localhost:7880, API key "devkey", secret "secret"
```

Put those values in both `.env` and `frontend/.env.local`. Everything else is identical.

## 7. Switch on the real reservation provider (optional)

OpenTable's API requires an approved **partner agreement**. The endpoints in
`agent/voice_agent/tools/restaurants/opentable.py` are illustrative placeholders that you adapt
to the partner documentation ([`tools.md` §6](tools.md#opentableprovider-partner-gated)).

```dotenv
RESTAURANT_PROVIDER=opentable
OPENTABLE_CLIENT_ID=your_opentable_client_id
OPENTABLE_CLIENT_SECRET=your_opentable_client_secret
# OPENTABLE_API_BASE_URL=...   # from your partner docs
# OPENTABLE_OAUTH_URL=...
```

Keep `RESTAURANT_PROVIDER=mock` for evals. The mock is deterministic, and the live API isn't.

## 8. Deploy

This repo doesn't ship deployment manifests. These are the parts that matter, and what we're
confident about.

**What runs where.**

- The **agent worker** is a long-running Python process that dials *out* to LiveKit. It needs no
  inbound ports or public URL, and scales horizontally by adding processes.
- The **frontend** is an ordinary Next.js app (`npm run build && npm start`, or any Next.js
  host). Its only secret-bearing part is the `/api/token` route.

**LiveKit Cloud agent hosting.** LiveKit Cloud can build and run the worker for you from a
Dockerfile, driven by the LiveKit CLI (`lk agent create` for the first deploy, then
`lk agent deploy` for new versions). Secrets such as `OPENAI_API_KEY` are stored with the agent
in Cloud, not in the image. The CLI's flags and generated files change between versions, so
follow LiveKit's current "deploying agents" docs rather than this page for the exact commands.

**Your own infrastructure (Docker, Kubernetes, VMs).** A minimal image needs:

- the source tree, **including `prompts/`**. The composer finds `prompts/` relative to the
  source checkout. If you install the package elsewhere (a non-editable wheel), set
  `PROMPTS_DIR=/path/to/prompts`;
- dependencies installed from the lockfile (`uv sync --frozen --no-dev`);
- a production start command: `uv run voice-agent start`, or
  `python -m livekit.agents start agent/voice_agent/main.py`;
- `LIVEKIT_*`, `OPENAI_API_KEY`, and any non-default settings injected as environment variables
  or secrets. Don't bake `.env` into the image.

A starting point, not a supported artifact. The `uv` steps were checked locally, but the image
itself hasn't been built or run:

```dockerfile
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY agent ./agent
COPY prompts ./prompts
RUN uv sync --frozen --no-dev
CMD ["uv", "run", "--no-sync", "voice-agent", "start"]
```

**Things to decide before real traffic:**

- auth and rate limiting on `/api/token`, since every token can start a paid session;
- `max_session_duration` and an idle policy (per-minute billing);
- where logs go (the prompt fingerprint is on every line);
- which evals gate a deploy ([`evals.md`](evals.md)).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser stuck on "Waiting for agent…" | Dispatch mismatch: the frontend's `LIVEKIT_AGENT_NAME` is empty while the worker registered `gpt-live-agent`, or the reverse. | Set `LIVEKIT_AGENT_NAME=gpt-live-agent` in `frontend/.env.local` (or empty on both sides). Restart both. |
| Same, and the names match | The worker isn't running, or it's connected to a different LiveKit project or URL. | Check the worker log for a successful registration. Compare `LIVEKIT_URL` and keys in both env files. |
| Worker log shows the job starting and then failing; the agent never speaks | `ConfigurationError: OPENAI_API_KEY is not set` | Set it in `.env` (the worker reads `.env` from the directory you start it in). |
| `ConfigurationError: RESTAURANT_PROVIDER=opentable needs OPENTABLE_CLIENT_ID ...` | OpenTable selected without credentials. | Add the credentials or set `RESTAURANT_PROVIDER=mock`. |
| `PromptCompositionError: ...` at startup or in tests | A module or manifest edit broke a rule (undeclared variable, wrong target, missing tool). | Run `uv run python -m voice_agent.prompts render <profile>`; the message names the module and rule. |
| `UnknownToolError: unknown tool 'x'` | A profile lists a tool that isn't in `TOOL_REGISTRY`. | Register it ([`tools.md` §10](tools.md#10-tutorial-add-your-own-custom-tool)) or fix the typo. |
| OpenAI error such as `invalid_api_key`, `insufficient_quota`, or model not found | The key lacks GPT-Live or backend-model access, or billing limits were hit. The plugin doesn't retry fatal errors. | Check the key's project, model access, and billing in the OpenAI dashboard. |
| Agent says a past date or the wrong weekday | `AGENT_TIMEZONE` is unset or wrong, so "today" is off by a day near midnight. An unknown zone falls back to UTC with a warning. | Set `AGENT_TIMEZONE` to the callers' IANA zone. Check `today` in the `composed prompts` log line. |
| Echo, or the agent interrupting itself | Speakers feeding into the mic. A full-duplex model hears its own voice. | Use headphones. Browsers apply echo cancellation, but laptop console mode may not. |
| Silence after the caller speaks, then a late answer | A slow tool, or high backend reasoning effort. | Keep `GPT_LIVE_BACKEND_REASONING_EFFORT=low`. Check tool timeouts. Look for provider warnings in the worker log. |
| `DeprecationWarning: the built-in Python CLI is deprecated` | Expected with `uv run voice-agent ...` on LiveKit Agents 1.8. | Harmless. Switch to `lk agent ...` when convenient. |
| `RuntimeError` mentioning text simulation / `lk agent simulate audio` | A test tried to run GPT-Live in LiveKit's text mode. A duplex model is audio-only. | Test the backend brain as text instead ([`evals.md`](evals.md)). |
| Frontend `500: Missing environment variable(s)` | `frontend/.env.local` missing or incomplete. | Copy `frontend/.env.example` and fill in the three `LIVEKIT_*` values. |

---

## Use GPT-Live in your own agent: checklist

You don't need this repo's structure to use GPT-Live. You need a handful of decisions. The
minimum to adopt it in an existing LiveKit Agents 1.8 project:

- [ ] **Dependency:** `livekit-agents[openai]~=1.8` (GPT-Live ships in `livekit-plugins-openai`
      1.8.x).
- [ ] **Model:** pass `llm=GPTLiveModel(model="gpt-live-1", voice=..., responses_options={...})`
      to `AgentSession`. Remove `vad=`, `stt=`, `tts=`, and `turn_detection=`; GPT-Live does all
      of it. ([primer §6.1](gpt-live-primer.md#61-minimal-agent))
- [ ] **Split your prompt in two.** `Agent(instructions=...)` becomes *voice* instructions (how
      to talk). `responses_options["instructions"]` becomes *backend* instructions (when to use
      tools, argument formats, how to report). ([`modular-prompts.md` §10](modular-prompts.md#10-writing-voice-prompts-vs-backend-prompts))
- [ ] **Compose instructions before `session.start`.** They are immutable afterwards. Anything
      dynamic (date, caller facts) must be rendered in up front, or appended later via
      `duplex_session.append_thinking()` / `append_instructions()` (≤500 tokens each).
- [ ] **Put today's date in both prompts.** The backend resolves "Friday" against it.
- [ ] **Keep your tools.** Under `delegation="responses"`, existing `@function_tool`s work
      unchanged and run on the backend brain. Make their docstrings and error messages
      model-facing, and keep outputs compact. ([`tools.md` §11](tools.md#11-checklist-for-production-tools))
- [ ] **Replace `session.say(...)`**, which is unsupported, with
      `generate_reply(instructions=...)` (for the greeting) or `append_commentary(...)`.
- [ ] **Remove code that relies on interrupting or truncating the agent**, such as custom barge-in
      handling or `interrupt()` calls. They're no-ops on a duplex model.
- [ ] **Tell the voice model to use filler** while it waits for tools. It's a prompt line, not
      code.
- [ ] **Set backend `reasoning.effort` and `text.verbosity` deliberately** (start with `low`), and
      consider `max_output_tokens`.
- [ ] **Log a prompt version** per session, so transcripts can be tied to what the model was told.
- [ ] **Re-plan tests.** LiveKit's text simulation can't run a duplex model. Test the backend
      brain as text and keep a smaller set of audio evals ([`evals.md`](evals.md)).
- [ ] **Budget for two meters:** per-minute voice plus backend tokens and tool fees. Watch
      `metrics_collected` and usage events.
- [ ] **Read the "when not to use it" list** in
      [`voice-agent-architectures.md` §9](voice-agent-architectures.md#9-when-you-should-not-use-this-architecture)
      before committing.

To reuse this repo's pieces rather than just the pattern, the LiveKit-free parts lift out
cleanly: `prompts/` + `voice_agent/prompts/` (composer), `voice_agent/tools/restaurants/`
(providers and validation), and `voice_agent/tools/registry.py`.
