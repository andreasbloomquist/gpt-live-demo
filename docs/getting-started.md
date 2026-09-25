# Getting started

From a fresh clone to talking with the agent: first in your terminal, then in the browser, and
then reading the graded call afterwards. At the end there's a checklist for adopting GPT-Live in
your *own* LiveKit agent.

> Related: [`architecture.md`](architecture.md) (what you're running),
> [`modular-prompts.md`](modular-prompts.md), [`tools.md`](tools.md), [`evals.md`](evals.md),
> [`call-analyzer.md`](call-analyzer.md), [`frontend.md`](frontend.md).

---

## Prerequisites

| Need | Version / notes |
|---|---|
| Python | 3.10+ (developed on 3.11). |
| [uv](https://docs.astral.sh/uv/) | Manages the virtualenv and lockfile. Every command below is `uv run ...`. |
| Node.js | 20+, only for the web frontend (CI builds it on 22). |
| A LiveKit server | A [LiveKit Cloud](https://cloud.livekit.io) project (free tier is fine), **or** a local `livekit-server --dev`. Not needed for console mode. |
| LiveKit CLI (`lk`) | Optional but recommended. LiveKit Agents 1.8 steers the dev loop toward `lk agent ...`, and it's how you deploy to LiveKit Cloud. |
| OpenAI API key | With access to **GPT-Live** (`gpt-live-1`) and the backend Responses model (`gpt-5.6-luna` by default). |
| A microphone | For console mode and the browser. Headphones help, since speakers feeding back into the mic confuse any full-duplex model. |

Cost: every connected session bills GPT-Live voice time (public pricing at time of writing is on
the order of $0.05/min, billed per second) plus backend tokens and web-search calls. Unit tests,
the prompt CLI and the Call Analyzer's offline mode cost nothing; with an API key, the analyzer's
judge adds one small-model request per call. See [primer §5](gpt-live-primer.md#pricing).

---

## 1. Clone and install

```bash
git clone <this-repo-url> gpt-live-demo
cd gpt-live-demo
uv sync                     # creates .venv, installs the agent + dev tools (pytest, ruff, mypy)
```

The eval runners need the `openai` SDK, which lives in a separate dependency group:
`uv sync --group evals`. See [`evals.md`](evals.md). The Call Analyzer in `analyzer/` is a
separate uv project with its own virtualenv; you install it in [step 6](#6-run-the-call-analyzer-free-no-keys).

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
validation and mock data, the OpenTable client against a mocked HTTP transport, call-record
building and export, and the worker's startup checks. The analyzer has its own suite:
`cd analyzer && uv run pytest -q`.

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

## 6. Run the Call Analyzer (free, no keys)

The analyzer stores every call the agent handles and grades it. It backs the **Calls** pages.
Start it before the worker so the worker has somewhere to send calls. In its own terminal:

```bash
cd analyzer
uv sync                                   # creates analyzer/.venv
cp .env.example .env                      # placeholder token; no API key -> heuristic grading
uv run python -m call_analyzer seed       # load and grade the six demo calls
uv run python -m call_analyzer serve      # http://127.0.0.1:8080
```

`seed` prints what it loaded and a score per call:

```text
created   gpt-live-4c1e9a07-d6f56f89  (01-available-state-bird.json)
...
Analyzing with provider=heuristic model=None ...

call_id                          status   score  outcome
gpt-live-4c1e9a07-d6f56f89       done        95  resolved
gpt-live-9b27d3f1-a3c6bb5a       done        95  resolved
gpt-live-e5a0c6b2-5b95b546       done        61  resolved
...
```

`provider=heuristic` means no API key was found, so a keyword-and-metrics heuristic did the
grading. To use the LLM judge, set `ANALYZER_API_KEY` in `analyzer/.env` (or have
`OPENAI_API_KEY` in the environment; `ANALYZER_PROVIDER=auto` picks it up) and re-grade with
`seed --reanalyze`. The heuristic can't judge meaning; see
[`call-analyzer.md` §9](call-analyzer.md#9-providers-an-llm-judge-or-an-offline-heuristic).

`serve` refuses to start without `CALL_ANALYZER_TOKEN` (at least 16 characters). The
placeholder in `.env.example` works on a laptop, and the analyzer logs a warning about it; for
anything else use `openssl rand -hex 32`. Check it's up:

```bash
curl -s http://127.0.0.1:8080/healthz              # {"status":"ok"}
```

Now tell the **agent** where to send calls. In the repo-root `.env`, uncomment and set:

```dotenv
CALL_ANALYZER_URL=http://localhost:8080
CALL_ANALYZER_TOKEN=change-me-shared-secret     # the same value as analyzer/.env
```

Without `CALL_ANALYZER_URL`, the agent still records every call, to `.call-records/<call_id>.json`
in the directory you start it from. Load those later with
`uv run python -m call_analyzer seed --dir ../.call-records` (from `analyzer/`).

## 7. Run it as a worker and talk from the browser

### 7a. Start the worker

```bash
uv run voice-agent dev      # or: lk agent dev
```

The worker connects to `LIVEKIT_URL` and registers as **`gpt-live-agent`**. That enables
*explicit dispatch*: LiveKit only sends it rooms that ask for that agent by name. (In 1.8 the
Python CLI's `dev` no longer hot-reloads; use `lk agent dev` if you want reload.)

A third, CLI-free way to run the same worker:
`uv run python -m livekit.agents start agent/voice_agent/main.py --dev`.

The worker checks its configuration before it registers. If something is wrong (no
`OPENAI_API_KEY`, an unknown `AGENT_TIMEZONE`, `CALL_ANALYZER_URL` without a token), it exits
straight away with one line, `voice-agent: invalid configuration, not starting: <reason>`, instead
of taking calls it can't serve.

### 7b. Start the frontend

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

CALL_ANALYZER_URL=http://localhost:8080
CALL_ANALYZER_TOKEN=change-me-shared-secret     # the same value as analyzer/.env
DEMO_PASSCODE=                                  # optional; leave empty on localhost
```

`frontend/.env.example` already ships with `LIVEKIT_AGENT_NAME=gpt-live-agent`, matching the
worker's default, and the local analyzer values. If you switch the worker to automatic dispatch (`LIVEKIT_AGENT_NAME=` empty),
clear it on the frontend too. The two settings must agree.

```bash
npm run dev                 # http://localhost:3000
```

Click **Start conversation** and allow the microphone. The transcript fills in live from the
`lk.transcription` streams: GPT-Live transcribes the caller itself, and there's no separate STT.

### 7c. See your calls

Open **Calls** (http://localhost:3000/calls). The six demo calls are there already. Hang up a
call on the Live page and it appears at the top within a few seconds: the worker posts the
record when the session closes, the analyzer grades it in the background, and the call shows
*Queued*, then *Analyzing*, and the page refreshes itself until the scorecard is ready. Open a call for the scorecard,
flags, metrics, caller sentiment and the transcript with per-turn transcription confidence.
**Re-analyze** grades it again (useful after you add an API key or change the rubric).

In the worker log, a successful export is an INFO line `call record exported` with
`destination: analyzer`. A WARNING with a file path means the analyzer didn't take the record;
the `detail` field says why.

### 7d. Or skip the frontend: Agents Playground

LiveKit's hosted [Agents Playground](https://agents-playground.livekit.io) can connect to your
Cloud project and talk to the running worker. Because this worker uses explicit dispatch, the
Playground session must request the agent `gpt-live-agent`. If your Playground version has no
agent-name setting, run the worker with `LIVEKIT_AGENT_NAME=` (empty) while testing, so it joins
every new room.

### 7e. Local LiveKit server instead of Cloud

```bash
livekit-server --dev        # ws://localhost:7880, API key "devkey", secret "secret"
```

Put those values in both `.env` and `frontend/.env.local`. Everything else is identical.

## 8. Switch on the real reservation provider (optional)

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

## 9. Deploy

This repo doesn't ship deployment manifests. These are the parts that matter, and what we're
confident about.

**What runs where.**

- The **agent worker** is a long-running Python process that dials *out* to LiveKit. It needs no
  inbound ports or public URL, and scales horizontally by adding processes.
- The **frontend** is an ordinary Next.js app (`npm run build && npm start`, or any Next.js
  host with a Node server). Its secrets (LiveKit, analyzer token, passcode) are read only on the
  server: in the token route, the Calls pages and the Server Actions.
- The **Call Analyzer** ships its own image (`analyzer/Dockerfile`: non-root, `/data` volume,
  healthcheck). Run **one** container per database, because the worker is in-process and SQLite
  has a single writer. Keep it on a private network: only the agent workers and the frontend's
  server need to reach it. See [`analyzer/README.md`](../analyzer/README.md#docker).

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

- auth and rate limiting on `/api/token`, since every token can start a paid session.
  `DEMO_PASSCODE` is a speed bump for a demo URL; the rate limit belongs at your host or proxy
  ([`frontend.md`](frontend.md#demo_passcode-and-why-it-isnt-enough));
- a retention and deletion policy for transcripts in the analyzer (and any `.call-records/`
  fallback files on workers), and a data-processing agreement with your judge provider;
- `max_session_duration` and an idle policy (per-minute billing);
- where logs go (the prompt fingerprint is on every line);
- which evals gate a deploy ([`evals.md`](evals.md)).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser stuck on "Waiting for agent…" | Dispatch mismatch: the frontend's `LIVEKIT_AGENT_NAME` is empty while the worker registered `gpt-live-agent`, or the reverse. | Set `LIVEKIT_AGENT_NAME=gpt-live-agent` in `frontend/.env.local` (or empty on both sides). Restart both. |
| Same, and the names match | The worker isn't running, or it's connected to a different LiveKit project or URL. | Check the worker log for a successful registration. Compare `LIVEKIT_URL` and keys in both env files. |
| Worker exits at startup with `voice-agent: invalid configuration, not starting: …` | The preflight check found a problem before registering with LiveKit. The reason follows the colon: `OPENAI_API_KEY is not set`, `CALL_ANALYZER_URL is set but CALL_ANALYZER_TOKEN is not`, an unknown `AGENT_TIMEZONE`, a bad profile or tool. | Fix that setting in `.env` (the worker reads `.env` from the directory you start it in) and start again. |
| `... RESTAURANT_PROVIDER=opentable needs OPENTABLE_CLIENT_ID ...` at startup | OpenTable selected without credentials. | Add the credentials or set `RESTAURANT_PROVIDER=mock`. |
| `PromptCompositionError: ...` at startup or in tests | A module or manifest edit broke a rule (undeclared variable, wrong target, missing tool). | Run `uv run python -m voice_agent.prompts render <profile>`; the message names the module and rule. |
| `UnknownToolError: unknown tool 'x'` | A profile lists a tool that isn't in `TOOL_REGISTRY`. | Register it ([`tools.md` §10](tools.md#10-tutorial-add-your-own-custom-tool)) or fix the typo. |
| OpenAI error such as `invalid_api_key`, `insufficient_quota`, or model not found | The key lacks GPT-Live or backend-model access, or billing limits were hit. The plugin doesn't retry fatal errors. | Check the key's project, model access, and billing in the OpenAI dashboard. |
| Agent says a past date or the wrong weekday | `AGENT_TIMEZONE` is set to the wrong (valid) zone, so "today" is off by a day near midnight. | Set `AGENT_TIMEZONE` to the callers' IANA zone. Check `today` in the `composed prompts` log line. |
| Echo, or the agent interrupting itself | Speakers feeding into the mic. A full-duplex model hears its own voice. | Use headphones. Browsers apply echo cancellation, but laptop console mode may not. |
| Silence after the caller speaks, then a late answer | A slow tool, or high backend reasoning effort. | Keep `GPT_LIVE_BACKEND_REASONING_EFFORT=low`. Check tool timeouts. Look for provider warnings in the worker log. |
| `DeprecationWarning: the built-in Python CLI is deprecated` | Expected with `uv run voice-agent ...` on LiveKit Agents 1.8. | Harmless. Switch to `lk agent ...` when convenient. |
| `RuntimeError` mentioning text simulation / `lk agent simulate audio` | A test tried to run GPT-Live in LiveKit's text mode. A duplex model is audio-only. | Test the backend brain as text instead ([`evals.md`](evals.md)). |
| Frontend `500: Missing environment variable(s)` | `frontend/.env.local` missing or incomplete. | Copy `frontend/.env.example` and fill in the three `LIVEKIT_*` values. |
| Worker log: `call record exported` at WARNING with `analyzer returned HTTP 401` | The agent's `CALL_ANALYZER_TOKEN` doesn't match the analyzer's. The record was saved to `.call-records/`. | Use the same token in `.env` and `analyzer/.env`, restart both, then ingest the saved files with `seed --dir ../.call-records`. |
| Calls page: "The call analyzer rejected this app's credentials" | The frontend's `CALL_ANALYZER_TOKEN` doesn't match the analyzer's (the analyzer returned 401). | Use the same token in `frontend/.env.local` and `analyzer/.env`, then restart `npm run dev` (Next.js reads env files at startup). |
| Calls page: "The call analyzer isn't configured" or "Couldn't reach the call analyzer" | `CALL_ANALYZER_URL` / `CALL_ANALYZER_TOKEN` missing in `frontend/.env.local`, or `serve` isn't running. | Set both, and check `curl http://127.0.0.1:8080/healthz`. |
| `serve` exits with `Configuration error: CALL_ANALYZER_TOKEN is not set` | No token in `analyzer/.env` or the environment. | `cp .env.example .env` in `analyzer/`, or export a token of 16+ characters. |
| Calls page shows "No calls yet" although you seeded | `seed` and `serve` used different databases: `ANALYZER_DB_PATH` defaults to `./data/calls.db`, relative to the directory you run the command in. | Run both from `analyzer/` (or set an absolute `ANALYZER_DB_PATH`). |
| Your own calls don't show up | The worker has no `CALL_ANALYZER_URL` (records go to `.call-records/`), recording is off (`CALL_RECORDING_ENABLED=false`), or the call never had a conversation turn (not recorded). | Check the worker log for `call record exported` and its `destination`. Set the URL and token in the repo-root `.env` and restart the worker. |
| A call stays *Queued* or *Analyzing*, or shows *Analysis failed* | The judge is slow or failing: rate limits back off and retry; a bad key, unknown model or wrong `ANALYZER_BASE_URL` fails at once with the reason. | Read the error on the call page and the analyzer log, fix the setting, then **Re-analyze**. |

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
