# Evals

This directory holds the eval harness for the GPT-Live voice agent. It has three tiers, and
paid tiers run only when a change can actually affect behaviour. This file covers usage; the
design rationale is in [`docs/evals.md`](../docs/evals.md).

| tier    | what runs                                                             | cost        | when |
|---------|-----------------------------------------------------------------------|-------------|------|
| `unit`  | pytest: composer, tools, detector, suite schema, runners with fakes   | free        | every PR (`ci.yml`) |
| `brain` | backend delegation model via the Responses API, with real tools on mock data | cents per case | when planned |
| `voice` | real GPT-Live sessions in LiveKit `AgentSession`, driven by TTS audio | ~$0.05/min + judge | when planned, and only if brain passed |

## Quick start

```bash
uv sync --group evals

uv run python -m evals validate                      # schema + cross-check against the agent
uv run python -m evals run --tier brain --dry-run    # what would run + cost estimate; no keys

# paid (needs OPENAI_API_KEY)
uv run python -m evals run --tier brain --suite restaurant_availability --max-cost-usd 2
uv run python -m evals run --tier voice --suite conversation_style --trials 5 --max-cost-usd 5

# which suites × tiers does my change need? (head = working tree; add --head REF for a commit)
uv run python -m evals.impact plan --base origin/main
uv run python -m evals.impact plan --base origin/main --format json
uv run python -m evals.impact probe --strict         # render all profiles + tool schemas
```

Results are written to `evals/.results/<tier>__<suite>.{json,xml,md}` as JSON (full transcripts),
JUnit XML and a Markdown summary. `python -m evals report --plan plan.json` aggregates them into
the PR comment. Exit codes: `0` pass, `1` eval failure, `2` config error, `3` budget exhausted.

## Suites

Each file in `suites/*.yaml` is one suite, validated by pydantic (`schema.py`, `extra="forbid"`).
The JSON Schema in `suite.schema.json` gives editors autocomplete; regenerate it with
`python -m evals schema --write`.

```yaml
name: restaurant_availability      # must match the file name
profile: concierge                 # prompt profile from prompts/manifest.yaml
tiers: [brain, voice]
tools: [check_restaurant_availability]   # tools this suite exercises (drives change routing)
trials: 3
pass_threshold: 0.66               # per-case pass rate required across trials
cases:
  - id: explicit_request
    user: Is there a table for four at Nopa tomorrow at 7pm?   # or `turns: [...]`
    tiers: [brain, voice]          # optional subset
    expect:
      tool_calls:
        - name: check_restaurant_availability
          args_subset:             # only listed keys are checked
            restaurant: { $regex: "nopa" }
            date: { $days_from_today: 1 }
            time: "19:00"
            party_size: 4
      no_tool_calls: false
      forbidden_tools: [web_search]
      max_words: 70                # final spoken reply
      must_not_match: ["https?://"]   # any assistant speech after the first user turn (not the greeting)
      judge: >-                    # LLM rubric, graded only if the deterministic checks pass
        Tells the caller whether 7pm is available ... never implies a booking was made.
```

Argument matchers: plain values compare case- and whitespace-insensitively, and numbers accept
numeric strings. The operators are `$regex`, `$in`, `$contains`, `$exists` and
`$days_from_today`. The last one checks relative-date resolution without hard-coding a date.

## How grading works

1. **Deterministic checks first**: tool calls and their arguments, forbidden tools, word
   limits and regex guardrails. They cost nothing and never flake.
2. **LLM judge second** (`runners/judge.py`): a pinned model (`EVALS_JUDGE_MODEL`) sees only
   the transcript and the rubric, and returns structured `{reasoning, verdict}` output. The
   transcript is fenced as untrusted data, and the judge is told it is data to grade, never
   instructions. `reasoning` is sent only to reasoning models (`gpt-5*`, `o<digit>*`). Both
   tiers use the same judge, so their pass rates are comparable.
3. **Repeated trials**: voice models are nondeterministic, so each case runs `trials` times
   and passes if `pass_rate ≥ pass_threshold`. Reports also show `pass^2`, the unbiased
   estimate `C(c,2)/C(n,2)` of the chance that two independent callers *both* get it right.
   k is fixed at 2 so the number is comparable across cases and still informative with 3
   trials (with k = n it could only be 0 or 1). It is empty when fewer than 2 trials ran.
   Once a case can no longer reach its threshold, its remaining trials are skipped (it has
   failed either way). Passing cases always run every trial.
4. **Hard budget**: `--max-cost-usd` reserves a pessimistic per-trial estimate when a trial
   starts running (once a concurrency slot is free), so concurrent trials together cannot
   exceed the cap. Spend comes from reported token usage and billed session seconds, except
   that a crashed trial (`crashed: true`) is charged its reservation and a failed judge call an
   estimate. Prices in `runners/common.py` are estimates; you can override them with
   `EVALS_PRICING_JSON`.

## The two paid tiers

**Brain** (`runners/brain.py`) calls the backend Responses model with exactly what GPT-Live
sends it: the composed backend instructions, `voice_agent.model.build_responses_options`, and
tool schemas converted the way `gpt_live_model._build_delegation_tools` converts them.
`web_search` is `{"type": "web_search", ...}`. Function tools execute for real through
LiveKit's own `llm.utils.execute_function_call` (which uses `prepare_function_arguments`)
against the deterministic `MockReservationProvider` (`RESTAURANT_PROVIDER` is forced to `mock`),
so tool results, `ToolError` messages, and the unknown-tool and internal-error texts match
production. The tool loop is capped at 6 steps per turn; hitting the cap ends the conversation.
Responses that come back `incomplete` (e.g. `max_output_tokens`) are recorded in the trial's
`meta.incomplete_responses`.

**Voice** (`runners/voice.py`) runs a real `AgentSession(llm=build_gpt_live_model(...))` with
the production `VoiceAgent`. It drives the session with **audio**, not text:

- GPT-Live is a `DuplexModel`, which only speaks. LiveKit 1.8.3 refuses to run one under a
  *text simulation* (`agent_activity.py`: "run `lk agent simulate audio` instead").
  Outside a simulation, `session.run(user_input=...)` sends typed text to the model as a
  one-off commentary ask. That path differs from a caller speaking and skips the model's own
  turn-taking. It is available as `--voice-input text` for smoke tests but has not been
  verified against the live service.
- Instead, each user turn is synthesized with OpenAI TTS as 24 kHz mono PCM. The audio is
  cached in `.cache/tts/` under `sha256(model|voice|instructions|text)` (per-clip lock, atomic
  write), so input audio is identical across runs and paid for once. It is streamed in real
  time through a custom `voice.io.AudioInput`, with silence between turns. A paced
  `AudioOutput` acts as the speaker and measures **voice-to-voice latency**; it honours
  `clear_buffer()`, so barge-ins are recorded as interrupted replies. The agent's turn counts
  as done once the session has been quiet for a short time. If the session closes on its own
  (a GPT-Live error), the trial ends at once as a crashed trial. Grading reads
  `session.history`, the same `ChatContext` that LiveKit's `evals.JudgeGroup` consumes.
- Trade-offs:
  - Cases run in real time (about 20–60 s each).
  - TTS voices are cleaner than real callers. For acoustic robustness, swap in recorded or
    noise-augmented audio at `TTSCache.synthesize`.
  - Provider-side tools such as `web_search` run inside OpenAI and are not observable as
    function-call events. Expectations about them are marked *skipped* in this tier rather
    than passed, and so is `no_tool_calls` when nothing visible was called but such a tool is
    enabled; the brain tier asserts them.
  - The runner reports `asr_similarity` between the script and what GPT-Live heard. When it
    is low, a failure says more about the input audio than about the agent.

## Change-impact detection (`impact/`)

`python -m evals.impact plan` exports the merge-base tree with `git archive`. It then builds a
**behavioural fingerprint** from components of both trees and diffs the two. Each tree is
probed in its own subprocess (`probe.py`) with that tree's `voice_agent` on `PYTHONPATH`.

| component | derived from | re-runs |
|---|---|---|
| `prompt.voice:<profile>` | rendered voice instructions + greeting | voice tier of that profile's suites |
| `prompt.backend:<profile>` | rendered backend instructions | brain + voice |
| `profile.tools:<profile>` / `tool.schema:<tool>` | tool JSON as the model sees it | brain + voice of suites whose profile exposes the tool |
| `profile.error:<profile>` | a profile that fails to render on one side | brain + voice of that profile's suites |
| `tool.impl:<tool>` | normalized AST of `ToolSpec.source_modules` plus the agent modules they import (transitively; core files and `config.py` excluded) | **brain** of suites that declare the tool |
| `code.core` | composer, registry | everything |
| `code.other:<path>` | any other file under `agent/voice_agent/`, data files included | everything |
| `code.backend_runtime` | `model.py` (it holds `build_responses_options`, the brain tier's backend config) | brain + voice |
| `code.voice_runtime` | `agent.py`, `main.py` | voice |
| `code.post_call` | `recording.py` | nothing (post-call only) |
| `config.voice:*` / `config.shared:*` | literal defaults in `config.py` (credentials, URLs and logging ignored; credentials match whole name segments, so `max_output_tokens` counts) | voice / both |
| `config.logic` | the rest of `config.py` (validators, helpers) | both |
| `suite:<name>` | normalized suite YAML | that suite |
| `runner:brain` / `runner:voice` / `runner:shared` | eval runner code | that tier / both |
| `deps:<pkg>` | `uv.lock` versions (and git/path sources) of `livekit*` and `openai` | everything |

Normalization decides what counts as "no change":
- **Prompts**: the *rendered* prompt is compared, so HTML comments the composer strips are
  ignored, and so are whitespace, blank lines and paragraph re-wrapping. Words, punctuation,
  list structure and nesting, and fenced code blocks are not. A comment that survives
  rendering (opened in one module, closed in another) reaches the model, so it counts.
- **Python**: comments and formatting are ignored, and so are *all* docstrings. That is safe
  because docstrings the model does see (tool descriptions, parameter docs) appear in the
  rendered tool schema, which is fingerprinted separately and routed to both tiers.
- **YAML**: parsed data is compared, not text.

Degradation and overrides:
- In CI the planner is the **base commit's** copy of `evals/` (exported with `git archive`
  and run with `--repo "$GITHUB_WORKSPACE"`), so a PR can't rewrite the rules that judge it.
  A diff that touches `evals/impact/**` or `.github/workflows/evals.yml`, or a base without a
  planner, runs everything: `planner code changed: running everything`.
- If the base tree cannot be rendered (for example, the composer did not exist yet), every
  suite is treated as changed.
- `--force-all`, `EVALS_FORCE_ALL=1` (with an optional `EVALS_FORCE_REASON`), the label
  `evals:full`, `workflow_dispatch` (by default) and the nightly schedule run everything.
- The label `evals:skip` runs nothing, even when a full run was forced.
- `--suites` and `--tiers` filter the plan; an unknown name is an error (exit 2), never an
  empty plan.
- Diffs that touch only docs, frontend or other CI files take a fast path that skips rendering.
  This fast path, not a workflow `paths:` filter, is the first gate in CI, because GitHub
  applies `paths:` to `labeled` events too. That would stop `evals:full` from working on a
  docs-only PR.

`--format github` writes `brain_matrix`, `voice_matrix`, `run_brain`, `run_voice`, `any` and
per-tier fingerprints to `$GITHUB_OUTPUT`, and a Markdown table to `$GITHUB_STEP_SUMMARY`.

## CI

- `.github/workflows/ci.yml`: ruff, pytest, rendering of every profile, suite validation,
  dry-runs, the call analyzer's own lint/tests/seed smoke test, and the frontend
  lint/typecheck/build. None of it needs secrets.
- `.github/workflows/evals.yml`: `plan` (base commit's planner) → `brain` matrix → `voice`
  matrix (only if brain passed) → `report` (builds it read-only) → `comment` (posts the sticky
  PR comment listing what ran, what was skipped, and why; `pull-requests: write`, no checkout).
  Actions are pinned to commit SHAs and every checkout uses `persist-credentials: false`.
  Draft PRs are planned (free) but the paid tiers wait until the PR is ready for review, unless
  it carries `evals:full`.

Setup: create an `evals` environment that holds the `OPENAI_API_KEY` secret. Add required
reviewers to it in the repository settings if paid runs should wait for approval (the workflow
can't set that), and optionally set the `EVALS_MAX_COST_USD` variable (per-job budget, default
3; a `workflow_dispatch` run defaults to 5). Fork PRs and repos without the key skip the paid
jobs and say so in the report. What this does and doesn't protect against is in
[`docs/evals.md` §8](../docs/evals.md#ci-trust-boundaries).

Environment knobs: `EVALS_JUDGE_MODEL`, `EVALS_TTS_MODEL`, `EVALS_TTS_VOICE`,
`EVALS_TTS_CACHE`, `EVALS_PRICING_JSON`, `EVALS_RESULTS_DIR`, `EVALS_LOG_LEVEL`; for the
planner, `EVALS_LABELS`, `EVALS_FORCE_ALL` and `EVALS_FORCE_REASON`.
