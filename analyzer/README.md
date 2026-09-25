# Call Analyzer

A small, separately deployable service that **stores every call the voice agent handles and grades
it**: a summary, the caller's intent, whether it was resolved, nine 1-5 quality scores with quoted
evidence, caller sentiment per turn, problem flags, and exact call metrics. The frontend's Calls
and Call detail views read from it.

It grades calls with a **small LLM as a judge** (any OpenAI-compatible endpoint), or, with no API
key at all, with a **deterministic offline heuristic**, so the whole demo works with zero keys.

```mermaid
flowchart LR
    Agent["Voice agent<br/>(session end)"] -->|"POST /v1/calls<br/>CallRecord JSON"| API["Call Analyzer API<br/>FastAPI"]
    API --> DB[("SQLite<br/>calls + analyses")]
    DB -->|"pending rows"| Worker["Background worker<br/>(in-process, bounded)"]
    Worker --> Judge{"Provider"}
    Judge -->|"key set"| LLM["LLM judge<br/>chat.completions.parse"]
    Judge -->|"no key"| Heur["Heuristic<br/>(offline)"]
    Worker -->|"validated Analysis"| DB
    UI["Next.js server routes"] -->|"GET /v1/calls, /v1/calls/{id}"| API
```

**Contents:** [Quick start](#quick-start-no-keys-needed) ·
[Why a separate service](#why-a-separate-service) · [How scoring works](#how-scoring-works) ·
[Providers](#providers) · [Worker, storage and logs](#worker-storage-and-logs) ·
[HTTP API](#http-api) · [Configuration](#configuration) · [Docker](#docker) ·
[Development](#development) · [Trade-offs and limitations](#trade-offs-and-limitations)

## Quick start (no keys needed)

Needs [uv](https://docs.astral.sh/uv/) and Python 3.10+.

```bash
cd analyzer
uv sync                                         # creates analyzer/.venv (separate from the agent's)
cp .env.example .env                            # placeholders; no API key -> heuristic provider
uv run python -m call_analyzer seed             # load + grade the six demo calls
uv run python -m call_analyzer serve            # http://127.0.0.1:8080
```

Real output of `seed` with the heuristic provider:

```text
created   gpt-live-4c1e9a07-d6f56f89  (01-available-state-bird.json)
created   gpt-live-9b27d3f1-a3c6bb5a  (02-alternatives-nopa.json)
created   gpt-live-e5a0c6b2-5b95b546  (03-frustrated-zuni.json)
created   gpt-live-27f8e4d9-5ea88d40  (04-web-search-ferry-building.json)
created   gpt-live-b3d9f0a4-d6133184  (05-large-party-foreign-cinema.json)
created   gpt-live-71c2a8e5-fbd0b0f9  (06-low-confidence-kokkari.json)

Analyzing with provider=heuristic model=None ...

call_id                          status   score  outcome
gpt-live-4c1e9a07-d6f56f89       done        95  resolved
gpt-live-9b27d3f1-a3c6bb5a       done        95  resolved
gpt-live-e5a0c6b2-5b95b546       done        61  resolved
gpt-live-27f8e4d9-5ea88d40       done        95  resolved
gpt-live-b3d9f0a4-d6133184       done        79  partially_resolved
gpt-live-71c2a8e5-fbd0b0f9       done        95  resolved
```

Other commands:

```bash
# print one Analysis (touches no database); --provider overrides ANALYZER_PROVIDER
uv run python -m call_analyzer analyze demo/calls/03-frustrated-zuni.json
uv run python -m call_analyzer analyze demo/calls/03-frustrated-zuni.json --provider openai
uv run python -m call_analyzer seed --dir ../.call-records   # records the agent saved to disk
uv run python -m call_analyzer seed --reanalyze              # re-grade (e.g. after a rubric bump)
```

`seed --dir ../.call-records` is how you ingest the files the agent writes when no analyzer URL is
configured (or the POST failed).

To connect the rest of the demo, give the agent and the frontend the same `CALL_ANALYZER_TOKEN`
and this service's URL (see [Configuration](#configuration)); to grade with an LLM instead of the
heuristic, set `ANALYZER_API_KEY` (see [Providers](#providers)).

### The demo calls

`demo/calls/*.json` are realistic concierge calls in the exact `CallRecord` format the agent posts,
with tool outputs produced by the agent's own mock reservation provider:

| File | What happens | Why it's there |
|---|---|---|
| `01-available-state-bird` | Table for two, requested time open, agent says nothing is booked | The happy path |
| `02-alternatives-nopa` | 7:00 taken; agent offers 6:45 / 7:15 / 6:30, caller picks one | Alternatives handling |
| `03-frustrated-zuni` | ASR mishears "Zuni" as "Zooey", agent rambles and is talked over, first check fails, caller gets curt | Frustration, interruptions, tool failure |
| `04-web-search-ferry-building` | Market hours answered from web search, then an **unsupported** claim about free parking | A hallucination only an LLM judge can catch |
| `05-large-party-foreign-cinema` | Party of 12 can't be checked online; agent routes the caller to the restaurant | Correct "partial" resolution |
| `06-low-confidence-kokkari` | Noisy line (ASR confidence 0.41-0.77), agent confirms details, 11 s pause while caller asks their partner | Low-confidence transcript, long silence |

## Why a separate service

- **The agent must never wait on analysis.** A call ends, the agent posts one JSON document with a
  short timeout and moves on. Grading takes seconds and can be retried for hours; that belongs in a
  queue, not in a voice worker's shutdown path.
- **Different scaling and trust.** Voice workers scale with concurrent calls; analysis scales with
  finished calls. The analyzer holds transcripts (personal data), so it's one place to secure,
  back up, and apply retention to.
- **Independent iteration.** The rubric, the judge model, and the scoring change on their own
  schedule. Re-grading old calls after a rubric change is one API call per call.

## How scoring works

The division of labour is the core design decision:

| Computed in code (exact, free, reproducible) | Judged by the provider (LLM or heuristic) |
|---|---|
| `metrics`, `overall_score`, `tool_failure` + `long_silence` flags, all validation | `summary`, `caller_intent`, `outcome`, the 1-5 `scores` with rationale + evidence, `sentiment`, qualitative flags |

### The rubric

[`call_analyzer/rubric.yaml`](call_analyzer/rubric.yaml) is data, versioned like the agent's
prompt modules: judge instructions, the agent policy the call is graded against, and for each
dimension a question, 1/3/5 anchors, and a weight. Every analysis records `rubric_version`, so
scores from different rubrics are never silently compared. A test pins the file's hash, so
editing it without bumping `version` fails CI.

| Dimension | Weight | Direction |
|---|---|---|
| `resolution` | 0.25 | higher is better |
| `accuracy_groundedness` | 0.15 | higher is better |
| `agent_helpfulness` | 0.15 | higher is better |
| `customer_satisfaction` | 0.15 | higher is better |
| `policy_adherence` | 0.10 | higher is better |
| `conversation_flow` | 0.05 | higher is better |
| `efficiency` | 0.05 | higher is better |
| `tone_empathy` | 0.05 | higher is better |
| `customer_frustration` | 0.05 | **inverted: 1 = none, 5 = severe** (render it inverted in UIs) |

### Overall score (0-100)

```
overall = Σ weight_d · (s_d − 1) / 4 · 100  /  Σ weight_d      (s_d = 6 − score for customer_frustration)
```

rounded half-up, over the dimensions that survived validation (weights renormalized if one was
discarded). Then **caps**: if `policy_adherence` or `accuracy_groundedness` is 1, the call scores
at most 40. A friendly call that claimed a booking it couldn't make, or invented a fact, must not
average its way to a good grade. The LLM is never asked for an overall score: models asked for
one anchor on vibes and drift; a weighted average of anchored judgements is explainable.

### Validation of the provider's answer

The judge's output is untrusted too. The policy (see `call_analyzer/analysis.py`, all tested):

- **Scores outside 1-5 are rejected, not clamped.** That dimension is omitted and the total
  renormalizes. Clamping would turn "the model said 7" into a confident 5 nobody judged. In-range
  fractions (e.g. 3.5) are rounded half-up. If fewer than 5 of 9 dimensions survive, the whole
  answer counts as malformed and is retried.
- **Evidence must be real.** A quote must cite an existing turn and appear in that turn's text
  (case-, whitespace- and quote-style-insensitive). Anything else is dropped: a quote that isn't
  in the transcript is exactly the hallucination evidence is meant to rule out.
- **Sentiment** is clamped to [-1, 1] and kept only for real caller turns (first per turn wins).
- **Flags** with an unknown `turn_id` keep their detail but lose the reference; the provider's
  `tool_failure`/`long_silence` flags are replaced by the deterministic ones.
- **Free text** is trimmed (summary 1000, rationale 600, reasons 500 chars; 3 quotes per
  dimension; 20 flags).

### Metrics (deterministic)

| Field | Definition |
|---|---|
| `duration_s` | From the record |
| `turns`, `user_turns`, `agent_turns` | Turn counts by role |
| `talk_ratio_agent` | Agent share of speaking time if every turn has timestamps, else of words |
| `interruptions` | Turns with `interrupted: true` (agent cut off by the caller) |
| `tool_calls`, `tool_errors` | Count, and count with `is_error` |
| `avg_agent_words_per_turn` | Mean words per agent turn (voice turns should be short) |
| `mean_transcript_confidence` | Mean ASR confidence over turns that have one, else `null` |
| `low_confidence_turns` | Turns with confidence < 0.6 |

Plus a `long_silence` flag for any gap over 6 s between turns (from timestamps; overlapping
barge-in speech is not a gap).

## Providers

Selected by `ANALYZER_PROVIDER`: `auto` (default) uses `openai` when an API key is configured and
`heuristic` otherwise. The chosen provider and model are logged at startup and recorded in every
analysis.

### `openai`: LLM-as-judge, any OpenAI-compatible endpoint

Uses the OpenAI Python SDK's **`chat.completions.parse`** with the pydantic output schema as a
strict `json_schema` response format. Why Chat Completions and not the Responses API: it's the
surface that OpenAI-compatible servers actually implement, including JSON-schema response
formats, so one code path covers them all. The SDK derives the schema from the same pydantic
model it parses the answer into, so there's no hand-written schema to drift.

**Default model: `gpt-5.4-mini`.** Grading a transcript against fixed anchors is a
reading-comprehension task, not a frontier-reasoning one, and a judge runs on every call, so we
use the newest *small* model listed in the installed SDK's `ChatModel` type (openai 3.19.2).
`gpt-5.4-nano` is cheaper still, at some cost in nuance. `ANALYZER_REASONING_EFFORT=low` keeps
reasoning tokens (and latency) down.

| Endpoint | `ANALYZER_BASE_URL` | Notes |
|---|---|---|
| OpenAI | *(empty)* | Default |
| Azure OpenAI (v1 API) | `https://<resource>.openai.azure.com/openai/v1/` | Model = your deployment name |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | e.g. `gemini-2.5-flash` |
| Groq | `https://api.groq.com/openai/v1` | Pick a model with JSON-schema support |
| Ollama | `http://localhost:11434/v1` | `ANALYZER_API_KEY=ollama` (any non-empty value) |
| vLLM / LiteLLM | `http://<host>:<port>/v1` | Structured output support varies by backend |

For non-reasoning models, or servers that reject the parameter, set `ANALYZER_REASONING_EFFORT=`
(empty). Servers that reject `max_completion_tokens` (OpenAI's current name for the output cap;
some older vLLM, Ollama or LiteLLM setups only know `max_tokens`) work with
`ANALYZER_MAX_TOKENS_PARAM=max_tokens`. If a server ignores the JSON schema, answers that don't
parse are treated as malformed and retried, then failed with a clear error.

**Prompt-injection defenses.** The transcript and tool outputs (which carry web content) are
untrusted. (1) Each turn and tool call is serialized as one JSON object per line, so text can't
break out of its string; (2) the block is fenced by markers containing a random nonce the
transcript can't predict, and the system prompt says everything inside is data, never
instructions; (3) the output is validated in code, so even a successful injection can't
fabricate evidence or push scores out of range.

**Cost control.** The transcript is trimmed to `ANALYZER_MAX_PROMPT_CHARS` (default 60,000,
about 15k tokens): long calls keep their opening (intent) and ending (outcome) with an explicit
"omitted" marker; single turns are clipped at 2,000 chars and tool outputs at 1,500. Output is
capped by `ANALYZER_MAX_OUTPUT_TOKENS`.

**Errors and retries.** The SDK retries individual requests on 429/5xx/timeouts (honouring
`Retry-After`, `ANALYZER_SDK_RETRIES`, default 2). If that still fails, the worker re-queues the
job with exponential backoff (30 s, 60 s, ... with jitter, never sooner than `Retry-After`) up to
`ANALYZER_MAX_ATTEMPTS` (default 3). Bad keys, unknown models, refusals, answers cut off at the
token limit, and responses that aren't chat completions at all (usually a wrong base URL) fail
immediately with an actionable message in `analysis.error`. Stored error messages never contain
secrets or transcript text.

### `heuristic`: deterministic and offline

Built from the metrics, the tool results, and small phrase lexicons (frustration, gratitude,
escalation requests, empathy, "I've booked..." claims, requests for card details, repeated
requests). Every rationale starts with `Heuristic:` and `analyzer.provider` is `"heuristic"` so
the UI can label it. Its `summary` is one or two sentences built from the tool outcomes (for
example "Nopa was full at 7:00, so the agent offered 6:45, 7:15 or 6:30, and the caller took
7:15."); it never repeats `caller_intent`, which the UI already shows as the title, and leaves
issue lists to `flags` and `metrics`. It's good at facts (did the check find a table? did a tool
fail? was the agent interrupted?) and useless at meaning: it can't see that the free-parking
answer in demo call 04 was made up, or that a polite caller left unhappy. It exists so the
pipeline works with zero keys and as a sanity baseline for the LLM.

## Worker, storage and logs

- **The database is the queue.** An analysis row in `pending` *is* the job. Ingest never rejects a
  call because an in-memory queue is full; a backlog is just rows waiting their turn, and nothing
  is lost on restart.
- **Bounded concurrency.** `ANALYZER_CONCURRENCY` loops (default 2) each claim one due job at a
  time in a transaction. Ingest wakes them immediately; otherwise they poll every 2 s, which also
  picks up retries whose backoff elapsed. Each job is bounded by `ANALYZER_JOB_TIMEOUT_S`.
- **Shutdown and restart recovery.** On a graceful shutdown, in-flight jobs are handed back as
  `pending` without charging an attempt. Rows left `running` by a process that *died* are
  re-queued on startup, unless they already used all attempts (so a call that crashes the
  process can't crash-loop it).
- **SQLite** via stdlib `sqlite3` in a thread (`asyncio.to_thread`), WAL mode, one connection
  serialized by a lock. Two tables: `calls` (the immutable record) and `analyses` (the current,
  re-computable verdict and job state). The repository is a small `Protocol`
  ([`storage.py`](call_analyzer/storage.py)); a Postgres implementation would use
  `SELECT ... FOR UPDATE SKIP LOCKED` to claim jobs and could then run several replicas.
- **Logs never contain transcripts.** Log lines carry ids, counts and durations. Exceptions,
  including the ones uvicorn would log for a crashed request, are written with their type, stack
  frames and (for validation errors) field locations only, never their message: a pydantic
  error quotes its input, which here is caller speech. The API catches unexpected errors itself,
  so none reach uvicorn's own error logging.

## HTTP API

The full reference, with `curl` examples and real responses. All `/v1` routes require
`Authorization: Bearer $CALL_ANALYZER_TOKEN`. All bodies are JSON. Interactive schema docs (`/docs`,
`/redoc`, `/openapi.json`) are **off by default**, because they map the API for anyone who can reach
the port; set `ANALYZER_ENABLE_DOCS=true` to serve them (unauthenticated) during development.

```bash
export URL=http://127.0.0.1:8080
export AUTH="Authorization: Bearer change-me-shared-secret"
```

### Errors

Every error has the same shape, never includes a stack trace, and never echoes submitted values
(they may contain caller speech):

```json
{"error": {"code": "validation_error", "message": "Invalid call record.",
           "details": [{"loc": ["room"], "msg": "Field required"}]}}
```

| Status | `code` | When |
|---|---|---|
| 400 | `bad_request` | Invalid `cursor`; client disconnected mid-upload. (A malformed `Content-Length` is rejected by uvicorn itself, as plain text.) |
| 401 | `unauthorized` | Missing/wrong bearer token (with `WWW-Authenticate: Bearer`) |
| 404 | `not_found` | Unknown `call_id` |
| 409 | `conflict` | A *different* record was already stored under this `call_id` |
| 413 | `payload_too_large` | Body over 2 MiB (checked on `Content-Length` *and* while streaming) |
| 422 | `validation_error` | Body or query parameters violate the contract |
| 500 | `internal_error` | A bug; details are only in the server log |

### `GET /healthz` (no auth)

```bash
curl -s $URL/healthz
# {"status":"ok"}
```

### `POST /v1/calls` - ingest a call

Stores the record and queues its analysis. Returns **202** `{call_id, status}`.

```bash
curl -s -X POST $URL/v1/calls -H "$AUTH" -H 'Content-Type: application/json' \
     --data-binary @demo/calls/01-available-state-bird.json
# HTTP/1.1 202 Accepted
# {"call_id":"gpt-live-0a1b2c3d-feedbeef","status":"pending"}
```

**Idempotency** (the agent may retry a POST whose response it never saw): `call_id` is the key.

- Re-posting an **identical** record (same canonical content hash: validated, UTC-normalized,
  key-sorted) returns **200** with the current status and does not re-analyze:
  `{"call_id":"gpt-live-0a1b2c3d-feedbeef","status":"done"}`.
- Posting **different** content under an existing `call_id` returns **409**. We chose rejection
  over upsert: a call record is an immutable fact, a changed record under the same id means a bug
  or an id collision, and a silent overwrite would also discard a paid-for analysis.

**Limits** (413 for size, 422 for the rest): body ≤ 2 MiB; ≤ 2000 turns; turn text ≤ 20,000
chars; ≤ 500 tool calls (arguments ≤ 20,000, output ≤ 50,000 chars); ≤ 200 usage entries;
duration ≤ 24 h. Timestamps must include a timezone, be from 1970 on, and are normalized to UTC.
`call_id` must match `^[A-Za-z0-9_][A-Za-z0-9._:@=+-]{0,199}$` (it appears in URLs and logs).
Unknown fields are rejected rather than dropped, so contract drift is loud (the agent then falls
back to disk). Turn ids and tool call ids must be unique within a call.

<details><summary>CallRecord v1 (abridged example)</summary>

```json
{
  "schema_version": 1,
  "call_id": "gpt-live-4c1e9a07-d6f56f89",
  "room": "gpt-live-4c1e9a07",
  "agent_name": "gpt-live-agent",
  "started_at": "2026-09-23T01:42:10.000Z",
  "ended_at": "2026-09-23T01:43:03.705Z",
  "duration_s": 53.7,
  "end_reason": "participant_disconnected",
  "prompt": {"profile": "concierge", "fingerprint": "3a38d1b3...", "version": "3a38d1b337b0",
             "voice": "156db23431d2", "backend": "e3ad63e9c465"},
  "models": {"voice_model": "gpt-live-1", "voice": "marin", "backend_model": "gpt-5.6-luna"},
  "turns": [
    {"id": "item_5d0c...", "role": "user",
     "text": "Hi! Can you check if State Bird Provisions has a table for two this Saturday at seven thirty?",
     "started_at": "2026-09-23T01:42:16.1Z", "ended_at": "2026-09-23T01:42:22.3Z",
     "interrupted": false, "transcript_confidence": 0.97}
  ],
  "tool_calls": [
    {"id": "call_...", "name": "check_restaurant_availability",
     "arguments": "{\"restaurant\":\"State Bird Provisions\",\"date\":\"2026-09-26\",\"time\":\"19:30\",\"party_size\":2}",
     "output": "{\"status\":\"available\",\"nearest_available_times\":[\"19:30\",\"19:15\",\"19:45\"],...}",
     "is_error": false, "created_at": "2026-09-23T01:42:27.0Z"}
  ],
  "usage": [{"type": "realtime_model", "model": "gpt-live-1", "session_duration_s": 53.7}]
}
```
</details>

### `GET /v1/calls?limit=20&cursor=...` - list calls, newest first

`limit` is 1-100 (default 20). Ordering is by call start time (then `call_id`), with **keyset**
pagination: pass `next_cursor` back as `cursor`. Pages stay stable when new calls arrive.
`next_cursor` is `null` on the last page. Cursors are opaque.

```bash
curl -s "$URL/v1/calls?limit=2" -H "$AUTH"
```

```json
{
  "items": [
    {
      "call_id": "gpt-live-71c2a8e5-fbd0b0f9",
      "started_at": "2026-09-24T23:33:40Z",
      "duration_s": 74.8,
      "turns": 15,
      "status": "done",
      "caller_intent": "Table for 3 at Kokkari (Thu Sep 24, 7:30 PM)",
      "summary": "Kokkari had 7:30 open on Thursday for 3; the agent made clear nothing was booked and pointed the caller to the restaurant or the app.",
      "outcome": {"status": "resolved", "reason": "The requested time was open; the caller was told to finish booking."},
      "overall_score": 95,
      "analyzer": {"provider": "heuristic", "model": null, "rubric_version": "1"}
    },
    {
      "call_id": "gpt-live-b3d9f0a4-d6133184",
      "started_at": "2026-09-24T21:20:16Z",
      "duration_s": 63.0,
      "turns": 8,
      "status": "done",
      "caller_intent": "Table for 12 at Foreign Cinema (Sat Oct 3, 7:00 PM)",
      "summary": "Foreign Cinema doesn't take parties of 12 online, so the agent sent the caller to the restaurant directly.",
      "outcome": {"status": "partially_resolved", "reason": "Party too large to book online; caller sent to the restaurant."},
      "overall_score": 79,
      "analyzer": {"provider": "heuristic", "model": null, "rubric_version": "1"}
    }
  ],
  "next_cursor": "WyIyMDI2LTA5LTI0VDIxOjIwOjE2LjAwMDAwMFoiLCJncHQtbGl2ZS1iM2Q5ZjBhNC1kNjEzMzE4NCJd"
}
```

`turns` is the turn count. `caller_intent`, `summary`, `outcome`, `overall_score`, and `analyzer`
are `null` until the analysis is `done`. `analyzer` is included so a list can label heuristic
results without fetching each call.

### `GET /v1/calls/{call_id}` - one call with its analysis

Returns `{"record": CallRecord, "analysis": Analysis}`. The record is served exactly as stored
(it was validated and normalized at ingest), so tightening the model later never breaks old
calls. The `analysis` part of the real output for the frustrated demo call (heuristic provider;
scores and sentiment trimmed for length):

```json
{
  "call_id": "gpt-live-e5a0c6b2-5b95b546",
  "status": "done",
  "error": null,
  "analyzer": {"provider": "heuristic", "model": null, "rubric_version": "1"},
  "created_at": "2026-09-25T17:55:56.941359Z",
  "summary": "After a failed first check, Zuni Cafe had 7:30 open on Wednesday for 2; the agent made clear nothing was booked and pointed the caller to the restaurant or the app. The caller grew frustrated after 2 interruptions.",
  "caller_intent": "Table for 2 at Zuni Cafe (Wed Sep 23, 7:30 PM)",
  "outcome": {"status": "resolved", "reason": "The requested time was open; the caller was told to finish booking."},
  "overall_score": 61,
  "scores": {
    "customer_frustration": {
      "score": 3,
      "rationale": "Heuristic: 2 caller turn(s) with frustration phrases.",
      "evidence": [
        {"turn_id": "item_993fce692d76", "quote": "Seriously? I already told you everything twice. Just try it again"},
        {"turn_id": "item_4d2f22584114", "quote": "Finally. Okay. Whatever, I'll do it myself"}
      ]
    },
    "resolution": {
      "score": 4,
      "rationale": "Heuristic: outcome classified as resolved.",
      "evidence": [
        {"turn_id": "item_b189793f4941", "quote": "That went through. Seven thirty tonight is open for two at Zuni. I haven't booked it, so you'll need to reserve with the restaurant or in the app"}
      ]
    }
  },
  "flags": [
    {"type": "tool_failure", "turn_id": null, "detail": "check_restaurant_availability failed: The reservation system isn't responding right now. Suggest trying again shortly or calling the restaurant directly."},
    {"type": "caller_repeated", "turn_id": "item_993fce692d76", "detail": "Caller had to repeat themselves."}
  ],
  "sentiment": [{"turn_id": "item_3d7d9bf7fd2e", "value": 0.0}, {"turn_id": "item_f16d0ba3e1dc", "value": 0.0}],
  "metrics": {
    "duration_s": 74.1, "turns": 11, "user_turns": 4, "agent_turns": 7, "talk_ratio_agent": 0.776,
    "interruptions": 2, "tool_calls": 2, "tool_errors": 1, "avg_agent_words_per_turn": 22.7,
    "mean_transcript_confidence": 0.797, "low_confidence_turns": 1
  }
}
```

While an analysis is `pending`/`running`/`failed`, only `call_id`, `status`, `error` and
`created_at` are set (everything else is `null`/empty). `created_at` is when the analysis was
produced; for a not-yet-done analysis it's when it was requested. `analysis` is never `null` in
this implementation (every stored call has an analysis row), but clients should tolerate it.

### `POST /v1/calls/{call_id}/analyze` - re-analyze

Forces a fresh analysis (after a rubric bump, a model change, or a failure). Returns **202**
`{"call_id": ..., "status": "pending"}`; **404** for an unknown call. The previous result is
cleared, and a run already in flight for this call is superseded: a generation counter makes sure
its late result is discarded instead of overwriting the new one.

```bash
curl -s -X POST $URL/v1/calls/gpt-live-e5a0c6b2-5b95b546/analyze -H "$AUTH"
# {"call_id":"gpt-live-e5a0c6b2-5b95b546","status":"pending"}
```

## Configuration

Environment variables (or `analyzer/.env`; see [`.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `CALL_ANALYZER_TOKEN` | *(required for `serve`)* | Shared bearer token; ≥ 16 chars. Startup fails without it |
| `ANALYZER_HOST` / `ANALYZER_PORT` | `127.0.0.1` / `8080` | Bind address (the Docker image uses `0.0.0.0`) |
| `ANALYZER_MAX_BODY_BYTES` | `2097152` | Request body cap |
| `ANALYZER_DB_PATH` | `./data/calls.db` | SQLite file (directory is created) |
| `ANALYZER_PROVIDER` | `auto` | `auto` \| `openai` \| `heuristic` |
| `ANALYZER_API_KEY` | falls back to `OPENAI_API_KEY` | Judge API key |
| `ANALYZER_BASE_URL` | *(OpenAI)* | Any OpenAI-compatible endpoint |
| `ANALYZER_MODEL` | `gpt-5.4-mini` | Judge model |
| `ANALYZER_REASONING_EFFORT` | `low` | Empty to omit (non-reasoning models) |
| `ANALYZER_MAX_TOKENS_PARAM` | `max_completion_tokens` | Or `max_tokens`, for servers that only accept that name |
| `ANALYZER_MAX_OUTPUT_TOKENS` | `8000` | Includes reasoning tokens |
| `ANALYZER_TIMEOUT_S` | `60` | Per request |
| `ANALYZER_SDK_RETRIES` | `2` | Request-level retries inside the SDK |
| `ANALYZER_MAX_PROMPT_CHARS` | `60000` | Transcript budget per analysis |
| `ANALYZER_CONCURRENCY` | `2` | Parallel analyses |
| `ANALYZER_MAX_ATTEMPTS` | `3` | Job-level attempts before `failed` |
| `ANALYZER_RETRY_BASE_S` | `30` | Backoff base (doubles per attempt, max 30 min) |
| `ANALYZER_JOB_TIMEOUT_S` | `300` | Hard cap per analysis |
| `ANALYZER_POLL_INTERVAL_S` | `2` | Idle poll interval |
| `ANALYZER_ENABLE_DOCS` | `false` | Serve `/docs`, `/redoc`, `/openapi.json` (unauthenticated) |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL`. Logs carry ids and counts, never transcript text ([details](#worker-storage-and-logs)) |

## Docker

```bash
cd analyzer
docker build -t call-analyzer .
docker run --rm -p 8080:8080 -v analyzer-data:/data \
  -e CALL_ANALYZER_TOKEN="$(openssl rand -hex 32)" \
  -e OPENAI_API_KEY=sk-... \
  call-analyzer
# seed the demo calls into the same volume:
docker run --rm -v analyzer-data:/data -e ANALYZER_PROVIDER=heuristic call-analyzer \
  python -m call_analyzer seed
```

Multi-stage build with `uv sync --locked` (the lockfile is the source of truth), no build tools
or `uv` in the final image, runs as an unprivileged user, data on a `/data` volume, and a
`HEALTHCHECK` on `/healthz` (in Python, since slim images have no curl; it follows
`ANALYZER_PORT`). Run **one** container per database: the worker is in-process and SQLite has a
single writer.

The base image is pinned by tag (`python:3.12-slim-bookworm`), which still moves with security
patches. For reproducible production builds, pin it by digest:
`docker build --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest> .` (get the digest
with `docker buildx imagetools inspect python:3.12-slim-bookworm`), and bump it deliberately.

## Development

```bash
cd analyzer
uv run ruff check . && uv run ruff format --check . && uv run mypy call_analyzer tests && uv run pytest -q
```

The tests need no network or keys: the OpenAI provider runs against a mocked HTTP transport
(the SDK is built on `httpx2`), the API is exercised end to end with a real SQLite file and the
background worker running.

Code map (all under `call_analyzer/`; start with `__main__.py` for the CLI or `api.py` for the
service, both of which wire the pieces below together):

| Module | Responsibility |
|---|---|
| `models.py` | `CallRecord` / `Analysis` contracts and limits; provider draft schema |
| `rubric.yaml`, `rubric.py` | Versioned rubric, weights, caps, overall score |
| `metrics.py` | Deterministic metrics and silence detection |
| `prompt.py` | Judge prompt: rubric + fenced, budgeted transcript |
| `providers/` | `base.py` protocol + errors, `openai_provider.py`, `heuristic.py` |
| `analysis.py` | Runs a provider, validates its draft, adds metrics/flags/overall score |
| `storage.py` | Repository protocol + SQLite (the DB is the job queue) |
| `worker.py` | Bounded background loops, retries/backoff, restart recovery |
| `api.py` | FastAPI app factory, auth, body limit, error handlers |
| `__main__.py` | CLI: `serve`, `seed`, `analyze` |

## Trade-offs and limitations

- **LLM-as-judge is a measurement instrument with known biases.** Judges tend to be lenient,
  favour longer and more confident answers, can prefer text in their own style, and vary between
  runs. Mitigations here: anchored 1-5 scales instead of open-ended ratings, evidence that must
  exist in the transcript, the overall score computed in code, caps for showstoppers, and a
  recorded `rubric_version` + model. What's *not* here and worth adding before trusting trends:
  a small human-labelled calibration set, agreement tracking between judge versions, and a
  periodic spot-check of low- and high-scoring calls.
- **The judge only sees text.** It can't hear tone, latency, or audio quality; interruptions and
  silences come from timestamps and flags the agent recorded, and ASR errors can look like agent
  errors (the rubric tells the judge to discount low-confidence turns).
- **The heuristic is shallow by design.** Keyword lexicons miss sarcasm, paraphrase, and
  anything factual. Its tone score can't hear warmth and defaults to 4. Treat its numbers as a
  smoke test, not a quality measure.
- **SQLite means one process.** Great for a demo and small deployments (and zero ops); for
  several replicas or high write volume, implement the repository on Postgres.
- **One shared bearer token** for both writer (agent) and readers (frontend). Separate
  write/read tokens (or mTLS/OIDC) are the next step for a real deployment. There's no
  rate limiting; put it behind a gateway if it's reachable from untrusted networks.
- **Transcripts are personal data.** This demo keeps them forever and sends them to the judge
  endpoint you configure. A real deployment needs a retention policy, deletion, access logging,
  and a data-processing agreement with the LLM provider (or a self-hosted judge).
- **Re-analysis replaces the previous result** rather than keeping a history of verdicts.
- **A shutdown can strand one claimed job until the next start.** If the process is stopped in
  the instant between a worker claiming a job (the row is already `running`, attempt charged)
  and receiving it, the job isn't handed back; the next startup's recovery re-queues it. In the
  worst case this uses up one of its `ANALYZER_MAX_ATTEMPTS` early.
