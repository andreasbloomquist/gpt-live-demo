# Evals: testing a full-duplex voice agent without paying for every commit

This page explains how the repo evaluates the GPT-Live agent. It covers what makes a speech-to-speech
agent hard to test, the three eval tiers, how suites are written and graded, and the change-impact
detector, which reads a diff and decides which paid evals it needs. It also covers the CI
workflows and the trade-offs of the design. Everything here matches the code in `evals/` and
`.github/workflows/`. The command outputs below were produced by running that code.

> Related: [`gpt-live-primer.md`](gpt-live-primer.md) (the model),
> [`architecture.md`](architecture.md) (the system),
> [`modular-prompts.md`](modular-prompts.md) (what the prompt fingerprints hash),
> [`tools.md`](tools.md) (the tools and their `source_modules`),
> [`voice-agent-architectures.md`](voice-agent-architectures.md) (why testing duplex models is
> harder than testing pipelines), [`getting-started.md`](getting-started.md) (setup).
> Usage cheat sheet: [`evals/README.md`](../evals/README.md).

---

## Contents

1. [Why voice agents are hard to evaluate](#1-why-voice-agents-are-hard-to-evaluate)
2. [Three tiers](#2-three-tiers)
3. [Suites and grading](#3-suites-and-grading)
4. [Change-impact detection](#4-change-impact-detection)
5. [CI workflows](#5-ci-workflows)
6. [Running evals locally](#6-running-evals-locally)
7. [Best practices for evaluating GPT-Live agents](#7-best-practices-for-evaluating-gpt-live-agents)
8. [Trade-offs and limitations](#8-trade-offs-and-limitations)
9. [Extending the system](#9-extending-the-system)

---

## TL;DR

- **Test the brain as text and the voice as audio.** GPT-Live hands every tool decision to a
  backend text model. The cheap tier calls that model directly, using the production config,
  the real tool schemas and the real tool code. Real audio sessions are only for what only audio
  can break.
- **Fingerprint what the model sees, not the files you edited.** The detector renders the
  prompts and tool schemas on the base commit and on the head commit, then compares them.
  Reflowing a paragraph or editing a docstring triggers nothing. Rewording one voice rule
  triggers the voice tier and nothing else.
- **Cheap checks go before expensive ones.** Deterministic assertions run before the LLM judge,
  and the brain tier runs before the voice tier. Every tier has a hard dollar budget.
- **Report pass^k, not pass@k.** Each caller gets exactly one sample. An agent that is right two
  times out of three fails one caller in three.
- **When in doubt, run it.** Every normalizer and fallback leans toward "changed". A false
  positive costs a few cents. A false negative ships a behaviour change nobody tested.

---

## 1. Why voice agents are hard to evaluate

A text chatbot eval sends a string and reads the string that comes back. A GPT-Live agent works
differently in almost every way:

| Difficulty | What it means here | What this repo does about it |
|---|---|---|
| **Nondeterminism** | The same input produces different replies, tool arguments and timing on each run. | Every case runs `trials` times against a `pass_threshold`, and reports show pass^k (§3). |
| **Audio in, audio out** | The model hears speech and produces speech. The text you grade is a transcript of audio. | The voice tier synthesizes the caller with TTS, streams it in real time and grades `session.history` (§2.3). |
| **Server-driven turn-taking** | GPT-Live decides for itself when the caller has finished and when to speak. There is no VAD or turn detector to stub out. | The fake microphone streams silence between turns, the way a phone line does, so the model takes turns naturally. A turn counts as done after 2.5 s of quiet. |
| **Two brains** | The voice model (`gpt-live-1`) talks. The backend Responses model (`gpt-5.6-luna` by default) reasons and calls every tool ([`gpt-live-primer.md`](gpt-live-primer.md)). | The brain tier evaluates the backend on its own, using its exact production config (§2.2). |
| **No text simulation** | LiveKit 1.8.3 refuses to run a `DuplexModel` in a text simulation. `agent_activity.py` raises *"a DuplexModel speaks only through audio, so it cannot run under a text simulation; run `lk agent simulate audio` instead"*. The usual `session.run(user_input=...)` + `.expect` testing style therefore does not carry over. | Test the brain as text through the Responses API, and test the voice with audio. |
| **Invisible tools** | `web_search` is an OpenAI-hosted tool. It runs inside OpenAI, so the LiveKit session never sees a function call for it ([`tools.md`](tools.md#1-where-tools-execute-under-gpt-live)). | The brain tier observes `web_search_call` items directly. The voice tier marks web-search expectations as **skipped**, never as passed. |
| **Latency is a feature** | A correct answer that arrives after four seconds of silence still fails on a phone call. | The voice tier measures voice-to-voice latency: the time from the end of the scripted caller audio to the first agent audio frame. |

### Why cost control is part of the design

A single voice eval trial runs up several separate bills at once:

1. **The GPT-Live session**, billed per second. `runners/common.py` estimates it at $0.05/min;
   check current pricing. It runs in real time, so a case takes about 20–60 s of wall-clock time.
2. **Backend Responses tokens**, including reasoning tokens, plus **web-search call fees**. The
   repo estimates web search at $0.01 per call.
3. **TTS** for the synthetic caller, which is cached on disk so it is usually paid only once.
4. **The LLM judge**, which costs tokens on every graded trial.

Multiply that by 3 trials, ~12 cases and 3 suites, and running everything on every push adds up.
It also gets slow. In practice, teams stop running evals once they are slow and expensive. The
rest of this design exists so that evals stay cheap enough that nobody wants to turn them off.

---

## 2. Three tiers

```mermaid
flowchart TB
    PR["Pull request"] --> Unit
    subgraph Unit["unit tier: free, every PR (ci.yml)"]
        U1["pytest: composer, tools, detector, suite schema, runners with fakes"]
        U2["render every profile, build every tool schema, validate suites, dry-run"]
    end
    Unit --> Plan{"change-impact plan<br/>(evals.yml)"}
    Plan -- "brain matrix" --> Brain
    Plan -- "nothing relevant" --> Done["no paid evals"]
    subgraph Brain["brain tier: cents per case"]
        B1["backend Responses model<br/>+ real tools on mock data"]
    end
    Brain -- "passed, or not planned" --> Voice
    Plan -- "voice matrix" --> Voice
    subgraph Voice["voice tier: ~$0.05/min + TTS + judge"]
        V1["real GPT-Live AgentSession<br/>driven by TTS audio"]
    end
    Voice --> Report["sticky PR comment"]
    Brain --> Report
```

| tier | what runs | what it catches | cost (estimates) | when |
|---|---|---|---|---|
| `unit` | pytest (`agent/tests`, `evals/tests`: 74 eval tests), prompt rendering, tool schema build, suite validation, dry-runs | broken code, invalid manifests, suite typos, a tool a profile doesn't enable, detector regressions | free, no keys | every PR and push to `main` (`ci.yml`) |
| `brain` | `runners/brain.py`: the backend model through the Responses API, with real tools | wrong tool choice, bad arguments, unresolved relative dates, policy violations ("I've booked it"), verbose answers | ~$0.04 per single-turn trial including the judge; `dry-run` estimates ≈ $1.42 for all three suites × 3 trials | when the plan selects it |
| `voice` | `runners/voice.py`: real GPT-Live sessions in `AgentSession`, driven by TTS audio | persona, spoken style, URLs read aloud, turn-taking, latency, anything a voice prompt or model/voice setting can change | ~$0.07 per single-turn trial; `dry-run` estimates ≈ $2.67 for all suites | when planned, and only if the brain tier passed or wasn't needed |

### 2.1 Unit

The unit tier is plain pytest plus CI steps that exercise the agent's real code without calling a
model. It renders every profile with the real composer, builds every tool schema
(`evals.impact probe --strict`), validates the suites against the agent (`evals validate`),
checks that `suite.schema.json` is up to date, and dry-runs both paid tiers. The detector's own
tests (`evals/tests/test_impact_git.py`) build throwaway git repos and assert on the plans
they produce. The main routing rules in §4 each have a test.

### 2.2 Brain: the cheap tier that carries most of the signal

With `delegation="responses"`, **the voice model never picks a tool** ([`tools.md`](tools.md)).
It hands the decision to a backend Responses model. The backend decides whether to search, whether
to check availability, and with which arguments. It resolves "tomorrow" into an ISO date, and it
applies the "never claim a booking" policy. All of these are decisions made by a *text model*,
which means we can call it directly.

The trick is to call it with **exactly** what GPT-Live would send it:

```mermaid
flowchart LR
    subgraph Shared["shared production code"]
        C["PromptComposer<br/>backend_instructions<br/>+ runtime today/timezone"]
        O["voice_agent.model<br/>build_responses_options()"]
        T["tool registry<br/>resolve_tools()"]
        S["evals.toolschema<br/>mirrors _build_delegation_tools"]
        M["MockReservationProvider<br/>(seeded, deterministic)"]
    end
    subgraph Prod["production session"]
        GL["GPTLiveModel<br/>delegation=responses"] --> RB["backend model"]
    end
    subgraph BrainTier["brain tier"]
        BR["BrainRunner tool loop"] --> RB2["backend model<br/>(Responses API)"]
    end
    C --> GL
    O --> GL
    T --> GL
    C --> BR
    O --> BR
    T --> S --> BR
    T --> M
```

- **Same instructions**: `agent_bridge.compose_bundle` composes the profile the same way
  `main.py` does, and it injects the real `today`/`timezone` so relative dates resolve as they
  would in production.
- **Same model options**: `agent_bridge.responses_options` calls
  `voice_agent.model.build_responses_options`, the function that builds GPT-Live's
  `responses_options`. That covers the model, reasoning effort, verbosity,
  `parallel_tool_calls` and `max_output_tokens`.
- **Same tool JSON**: `evals/toolschema.py` mirrors `_build_delegation_tools` in
  `livekit-plugins-openai` 1.8.3. Function tools use LiveKit's legacy schema in the internally
  tagged Responses shape. `WebSearch` serializes with `to_dict()` to `{"type": "web_search", ...}`.
- **Same tool code**: function calls go through LiveKit's own `prepare_function_arguments` and
  the real `check_restaurant_availability`. That tool runs against `MockReservationProvider`
  (`RESTAURANT_PROVIDER` is forced to `mock`), so the model sees the same JSON outputs and
  `ToolError` messages it would see in production.
- **Web search runs for real** on OpenAI's side, and each `web_search_call` output item is
  recorded with its query. In the voice tier the same call can't be observed at all.

The runner loops until the model stops calling tools, capped at `MAX_STEPS_PER_TURN = 6`. A model
stuck calling tools counts as a failure. It never becomes an unbounded bill.

**What the brain tier cannot see** is how the voice model phrases, times or interrupts. It also
takes a shortcut: in production the backend receives the conversation through GPT-Live's
delegation channel, while here it receives the caller's words directly. That approximation is what
makes the tier cheap and fast, and it is the reason the voice tier exists.

### 2.3 Voice: real sessions, real audio

`runners/voice.py` builds a real `AgentSession(llm=build_gpt_live_model(settings, bundle))` with
the production `VoiceAgent`, then drives it the way a caller would:

1. **Synthesize** each scripted user turn with OpenAI TTS (`EVALS_TTS_MODEL`, default
   `gpt-4o-mini-tts`; `EVALS_TTS_VOICE`, default `alloy`) as 24 kHz mono PCM. Each clip is
   cached in `evals/.cache/tts/` under `sha256(model|voice|text)`. The same text therefore
   produces byte-identical audio on every run, which removes TTS as a source of variance
   between commits.
2. **Stream** the audio in real time through a custom `voice.io.AudioInput`, in 20 ms frames
   with 0.6 s of lead-in silence. Between turns the fake microphone keeps sending silence, so
   GPT-Live decides for itself when the caller has finished.
3. **Play out** the agent's audio through a paced `AudioOutput` that behaves like a speaker, so
   LiveKit's playout and transcript synchronization work as they would in a room. The first
   frame of each reply is timestamped to measure **voice-to-voice latency**.
4. **Settle**: a turn counts as done once the agent has replied and 2.5 s have passed with no
   speech, no state change and no tool activity (`SETTLE_S`), with a 60 s timeout per turn. The
   greeting is allowed to finish first, so the first user turn isn't treated as a barge-in.
5. **Grade** `session.history`, the same `ChatContext` that LiveKit's `evals.JudgeGroup`
   consumes, with the same assertions and judge the brain tier uses.

Every trial also records `asr_similarity`: how closely GPT-Live's transcription of the TTS clip
matches the script. If that number is low, the model misheard the input, and a failure says more
about the audio than about the agent.

`--voice-input text` skips audio and calls `session.run(user_input=...)`. Outside a text
simulation LiveKit forwards that text to GPT-Live as a one-off commentary request
(`GPTLiveSession._generate_reply`). That code path differs from a caller speaking and bypasses
turn-taking. It exists for smoke tests only and has **not** been verified against the live service.

---

## 3. Suites and grading

### The format

A suite is a YAML file in `evals/suites/`. Here is a real one, trimmed from
`restaurant_availability.yaml`:

```yaml
# yaml-language-server: $schema=../suite.schema.json
name: restaurant_availability        # must equal the file name
description: Checks availability with correct arguments; never claims a booking.
profile: concierge                   # prompt profile from prompts/manifest.yaml
tiers: [brain, voice]
tools: [check_restaurant_availability]  # tools this suite exercises; drives change routing
trials: 3
pass_threshold: 0.66                 # i.e. at least 2 of 3 trials

cases:
  - id: explicit_request
    user: Is there a table for four at Nopa tomorrow at 7pm?
    expect:
      tool_calls:
        - name: check_restaurant_availability
          args_subset:               # only the listed keys are checked
            restaurant: { $regex: "nopa" }
            date: { $days_from_today: 1 }
            time: "19:00"
            party_size: 4
      forbidden_tools: [web_search]
      max_words: 70
      judge: >-
        The reply tells the caller whether a table for four at Nopa is available around 7pm
        tomorrow, and if 7pm is not available it offers at least one concrete alternative
        time. It does NOT say or imply that a reservation has been made or confirmed.

  - id: slot_filling_multi_turn
    turns:                           # multi-turn script instead of `user`
      - Can you check if Zuni Cafe has anything this Saturday around 8?
      - Just two of us.
    expect:
      tool_calls:
        - name: check_restaurant_availability
          args_subset:
            party_size: 2
            time: { $in: ["20:00", "08:00"] }
```

The three suites in the repo test different things:

- **`restaurant_availability`**: tool choice, argument extraction, and the "check, never book"
  policy.
- **`web_search`**: when to search and when to answer directly. Its rubrics check *behaviour*
  (grounded, brief, no URLs read out), never facts that change daily.
- **`conversation_style`**: word limits, no URLs or markdown, staying in scope, persona. Its
  `persona_identity` case is voice-only.

### Validation: typos fail for free

`evals/schema.py` defines the format as pydantic models with `extra="forbid"` on every model.
A misspelled key such as `forbiden_tools` is almost always a broken assertion. Without this check
it would quietly make a paid eval assert nothing. With it, the suite fails in the free tier.
Cross-field rules are checked too:

- `name` must match the file name.
- Each case has exactly one of `user` or `turns`.
- Case `tiers` must be a subset of the suite's `tiers`.
- A case may not set `no_tool_calls` and also expect tool calls.
- The same tool can't be both expected and forbidden.
- Every tool in `expect.tool_calls` must be declared in the suite's `tools`.
- Every `must_not_match` regex must compile.
- Unknown suite names passed to `--suite` are an error, so a typo can't turn into "ran nothing,
  reported green".

`python -m evals validate` then cross-checks the suites against the *agent*. The profile must
render, the declared tools must be enabled in that profile, and every tool schema must build. The
JSON Schema in `evals/suite.schema.json` is generated from these models, which gives editors
autocomplete, and CI fails if it is stale.

### Grading order: deterministic first, judge second

```mermaid
flowchart LR
    T["trial transcript"] --> D{"deterministic checks<br/>tool calls + args, forbidden tools,<br/>max_words, must_not_match"}
    D -- "any fail" --> F["trial FAIL<br/>(judge not called)"]
    D -- "all pass" --> J{"LLM judge<br/>rubric + transcript only"}
    J -- "verdict=false" --> F
    J -- "verdict=true" --> P["trial PASS"]
```

1. **Deterministic checks** (`runners/assertions.py`) cost nothing and never flake:
   - `tool_calls`: `args_subset` constrains only the keys it lists. Strings compare case- and
     whitespace-insensitively, numbers accept numeric strings, and the operators are `$regex`,
     `$in`, `$contains`, `$exists` and `$days_from_today`.
   - `$days_from_today: 1` checks that "tomorrow" resolved to the right ISO date *in the agent's
     timezone*, without hard-coding a calendar date into the suite.
   - `ordered: true` enforces call order.
   - `no_tool_calls` and `forbidden_tools` guard precision.
   - `max_words` applies to the final spoken reply, which is all agent speech after the last user
     turn. In voice, a filler like "let me check…" and the answer arrive as separate messages.
   - `must_not_match` regexes run over everything the agent said after the first user turn. The
     greeting is excluded.
2. **The LLM judge** (`runners/judge.py`) runs **only if every deterministic check passed**. That
   saves money, and a judge should never be asked to excuse a wrong tool call. The judge:
   - sees only the rubric and the transcript, never the agent's prompts, so it grades behaviour
     rather than intent;
   - returns structured output `{reasoning, verdict}` through `responses.parse`, with the
     reasoning field placed before the verdict;
   - gives a binary verdict against explicit pass criteria, because rating scales drift and
     pass/fail is easier to read in a PR comment;
   - uses a model pinned by `EVALS_JUDGE_MODEL`, so upgrading the agent's model doesn't silently
     change the grader.

   Both tiers use the same judge, so a case that passes in brain and fails in voice points at the
   voice layer, not at a different grader.
3. **Unobservable is not passed.** In the voice tier, expectations about `web_search` are recorded
   as *skipped*, with the detail "not observable in this tier".

### Trials, thresholds, pass^k and pass@1

Each case runs `trials` times (suite default 3; a case can override it; the maximum is 20) and
**passes if `pass_rate ≥ pass_threshold`**. The default threshold of `0.66` means 2 of 3. The
suite passes only if every case passes.

Reports show two numbers per case (`runners/common.py`):

- **pass@1**: the chance that a single attempt succeeds. With the unbiased estimator this equals
  the plain pass rate, c/n.
- **pass^k**: the chance that *all k* attempts succeed, `C(c,k) / C(n,k)`. This is the number
  that matters for a production voice agent. Every caller gets exactly one sample, so an agent
  that is right 2 times out of 3 fails one caller in three. The more common pass@k ("at least
  one of k succeeded") flatters nondeterministic systems. It works as a metric for code
  generation, where a person reviews the attempts, but it is the wrong metric for a phone call.

Two policies keep the numbers honest and the cost down:

- **Early stop on failure, never on success.** Once a case can no longer reach its threshold
  (two failures out of three), its remaining trials are skipped. Passing cases always run every
  trial, so pass^k stays meaningful. A crashed trial counts as a failed trial, not a crashed run.
- **Hard budget.** `--max-cost-usd` *reserves* a pessimistic estimate before each trial starts.
  The estimate is the larger of the static estimate and the most expensive trial seen so far.
  Concurrent trials together therefore cannot overshoot the cap. Real cost comes from reported
  token usage and, for voice, billed session time. A model missing from the price table is priced
  as the most expensive known model, so the guard errs toward stopping early. When the budget runs
  out the suite is marked `budget_exceeded` and the CLI exits with `3`.

---

## 4. Change-impact detection

Running every paid eval on every push is expensive. Running none is how a reworded guardrail
reaches production untested. The detector sits between the two. It answers one question for each
(suite, tier) pair: **could this diff change the behaviour this eval measures?**

### The pipeline

```mermaid
flowchart TB
    A["python -m evals.impact plan --base origin/main --head HEAD"] --> MB["merge-base(base, head)"]
    MB --> CF["git diff --name-only"]
    CF --> FP{"any behaviour-relevant path?<br/>agent/ prompts/ evals/ uv.lock"}
    FP -- "no: docs, frontend, CI" --> FAST["fast path: skip all, with reason"]
    FP -- "yes" --> EX["git archive base tree and head tree<br/>into temp dirs"]
    EX --> PB["probe.py subprocess, base tree<br/>PYTHONPATH=base/agent, empty cwd, scrubbed env"]
    EX --> PH["probe.py subprocess, head tree<br/>PYTHONPATH=head/agent, empty cwd, scrubbed env"]
    PB --> SB["base snapshot<br/>rendered prompts, tool JSON,<br/>normalized ASTs, settings, suites, lockfile"]
    PH --> SH["head snapshot"]
    SB --> DIFF["diff component digests"]
    SH --> DIFF
    DIFF --> RULES["routing rules<br/>rules.affects(component, suite, tier)"]
    RULES --> PLAN["plan: RUN / skip per suite x tier,<br/>with reasons and fingerprints"]
    PLAN --> OUT["text / JSON / GITHUB_OUTPUT matrices<br/>+ step summary"]
```

The design choices behind the pipeline:

- **`git archive`, not `git worktree`.** An export needs no cleanup bookkeeping in `.git/worktrees`
  and works on CI clones. By default the base is the **merge-base**, so commits that landed on
  `main` after you branched are not attributed to your PR.
- **One subprocess per tree.** `probe.py` puts *that tree's* `agent/` first on `sys.path` and
  imports its `voice_agent`. It renders every profile, meaning voice instructions plus greeting,
  backend instructions and the tool list. It then builds every registered tool's schema. The two
  trees never share module state. An old or broken base fails in isolation and produces
  `{"ok": false}` instead of crashing the planner. The probe also runs from an empty working
  directory with a scrubbed environment and `Settings(_env_file=None)`, so a developer's `.env`
  can't leak into the fingerprint. Both probes run in parallel and never touch the network.
- **One converter for both sides.** Both trees are serialized with the *head* checkout's
  `evals.toolschema` and normalizers, so the two sides are always compared like for like.
- **Runtime variables become placeholders.** `today` renders as `<runtime:today>`, so base and
  head render identically on any day.

### Components and routing

A snapshot is a flat map of `{component_key: digest}`. The planner diffs the two maps and asks
`rules.affects(key, suite, tier)` for every changed key. The policy fits on one screen
(`evals/impact/rules.py`):

| component | derived from | re-runs |
|---|---|---|
| `prompt.voice:<profile>` | rendered voice instructions + greeting, normalized | **voice** tier of suites on that profile |
| `prompt.backend:<profile>` | rendered backend instructions, normalized | brain + voice of suites on that profile |
| `profile.tools:<profile>` | the profile's tool list | brain + voice of suites on that profile |
| `profile.error:<profile>` | a profile that fails to render on one side | brain + voice of suites on that profile |
| `tool.schema:<tool>` | the tool's JSON exactly as the backend model sees it | brain + voice of suites whose *profile exposes* the tool, even if no case calls it |
| `tool.impl:<tool>` | normalized AST of the files in the tool's `source_modules` | **brain** tier of suites that *declare* the tool in `tools` |
| `code.core` | `voice_agent/__init__.py`, `voice_agent/prompts/`, `tools/__init__.py`, `tools/registry.py` | everything |
| `code.voice_runtime` | `agent.py`, `model.py`, `main.py` | voice tier of every suite |
| `code.other:<path>` | any other agent module (e.g. `runtime.py`) | everything (unknown means conservative) |
| `config.voice:<field>` | `Settings` defaults for `gpt_live_model`, `gpt_live_voice` | voice tier of every suite |
| `config.shared:<field>` | every other behavioural `Settings` default | brain + voice of every suite |
| `config.logic` | the rest of `config.py` (validators, helpers) | brain + voice of every suite |
| `suite:<name>` | the suite YAML, parsed | that suite's tiers |
| `runner:brain` / `runner:voice` / `runner:shared` | eval runner code: `runners/brain.py`, `runners/voice.py`, the rest of `runners/` plus `schema.py`, `toolschema.py`, `cli.py` | that tier (shared: both) of every suite |
| `deps:<pkg>` | `uv.lock` versions of `livekit*` and `openai` | everything |
| anything new | | everything |

Two rows are less obvious than the others:

- **Tool implementation changes re-run only the brain tier.** Tools execute identically, against
  the same mock provider, in both tiers. The brain tier is where their outputs are asserted.
  Refactoring tool code can't change the voice model's accent, persona or timing.
- **A schema change re-runs every suite whose profile exposes the tool**, including suites that
  never call it. The backend model reads all tool descriptions on every request, so rewording
  one tool can change when the model reaches for a different one.

Settings in `config.py` are compared **field by field**. Credentials, URLs, `log_level`, `livekit_*`
and `opentable_*` fields (evals always use the mock) are ignored. A new field is treated as
behavioural until someone adds it to an allow-list.

### What does *not* trigger evals, and why that is safe

Normalization decides what counts as "no change" (`evals/impact/normalize.py`). Each normalizer
answers one question: *could this edit change behaviour?* When unsure, it answers yes.

| Edit | Triggers? | Why |
|---|---|---|
| HTML comments, trailing spaces, runs of spaces, extra blank lines between paragraphs | no | The fingerprint uses the rendered prompt with comments stripped and whitespace collapsed. |
| Re-wrapping a paragraph at a different column | no | Soft-wrapped lines are joined back together. This is the most common "no-op" prompt edit. |
| Turning a paragraph into a bullet list, or adding a blank line *between* list items | **yes** | Lines that start a markdown block (`-`, `1.`, `#`, `>`, `\|`, fences) keep their structure, and models respond to structure. |
| Any change to words, punctuation or casing | **yes** | Models are sensitive to all three. |
| A module's `version:` or `description:` | no | Not part of the rendered text. |
| Python comments and formatting | no | Code is compared as an AST dump, with no line numbers. |
| **Any** Python docstring | no in `tool.impl`/`code.*` | See below. |
| A tool's docstring or `Args:` text | **yes**, via `tool.schema` | LiveKit turns these into the tool description the model reads. |
| A settings default re-quoted (`"low"` → `'low'`) or re-wrapped `Field(...)` | no | Defaults are compared as AST. |
| Suite comments, key order, quoting, flow vs block style | no | YAML is compared as parsed data. |
| Changes to `evals/impact/`, `evals/tests/`, `evals/README.md`, `evals/report.py` | no | These decide what runs and how it is displayed, not how the agent behaves or how it is graded. |
| `docs/`, `frontend/`, CI files | no, fast path | Rendering is skipped entirely. |

**Why ignoring *all* docstrings is safe:** some docstrings are model-visible. The
`@function_tool` docstring and its `Args:` section become the tool's description and parameter
docs. Those, however, show up in the **rendered tool schema**, which is fingerprinted separately as
`tool.schema:*` and routed to both tiers. Every other docstring (helpers, providers, internals)
never reaches the model. The AST fingerprint therefore only has to answer "did the *executed* code
change?", and the schema fingerprint answers "did what the model *reads* change?". The same logic
applies to prompts: the detector hashes the rendered text, not the markdown file, so it can't be
fooled by edits that never reach the model.

### Real output

These plans were produced by cloning this repo into a scratch directory, making one commit per
scenario, and running `python -m evals.impact plan --base HEAD~1 --head HEAD --format text`.
Each run took about 2–3 seconds. The fingerprints are real, and the SHAs belong to the throwaway
clone.

**1. Comment, reflow and blank lines in a voice prompt module.** The diff adds an HTML comment,
re-wraps a bullet across two lines with extra spaces, and inserts two blank lines before a
heading in `prompts/modules/core/voice_style.md`:

```text
change-impact plan  base=90814b0ad07f  head=e4d0ffbd685e
  nothing to run
  skip brain conversation_style: no behaviour-affecting change for this tier
  skip voice conversation_style: no behaviour-affecting change for this tier
  skip brain restaurant_availability: no behaviour-affecting change for this tier
  skip voice restaurant_availability: no behaviour-affecting change for this tier
  skip brain web_search: no behaviour-affecting change for this tier
  skip voice web_search: no behaviour-affecting change for this tier
```

A first attempt at this commit also added a blank line *between two bullets*. The detector planned
the voice tier for it (`+1/-0 normalized lines`). That is correct: a blank line there turns a tight
list into a loose one, which is a structural change.

**2. Reword one voice rule** ("usually one or two sentences" → "usually one sentence, two at most").
Only the voice tier runs, because the backend never sees voice instructions:

```text
change-impact plan  base=e4d0ffbd685e  head=16f60ce5a98a
  RUN  voice conversation_style  [fp 968e581f670d39a8]
         - voice prompt of profile `concierge` changed (+1/-1 normalized lines)
  RUN  voice restaurant_availability  [fp ba711e9c94eea20c]
         - voice prompt of profile `concierge` changed (+1/-1 normalized lines)
  RUN  voice web_search  [fp 1b5ab42d470fe811]
         - voice prompt of profile `concierge` changed (+1/-1 normalized lines)
  skip brain conversation_style: no behaviour-affecting change for this tier
  skip brain restaurant_availability: no behaviour-affecting change for this tier
  skip brain web_search: no behaviour-affecting change for this tier
```

**3. Change the restaurant tool's `party_size` argument description** in its docstring. The
change is model-visible, so both tiers run for every suite on the profile, `web_search` included:

```text
change-impact plan  base=16f60ce5a98a  head=2bf76d990b59
  RUN  brain conversation_style  [fp d1e01e3a10c8125e]
         - model-visible schema of tool `check_restaurant_availability` changed
  RUN  voice conversation_style  [fp 5f3d71bd1e46cd96]
         - model-visible schema of tool `check_restaurant_availability` changed
  RUN  brain restaurant_availability  [fp 99e6df372e6d03dc]
         - model-visible schema of tool `check_restaurant_availability` changed
  ...  (voice restaurant_availability, brain + voice web_search: same reason)
```

**3b. For contrast, reword the docstring of the internal `check_availability` helper** in the same
file. The model never sees that text, so nothing runs:

```text
change-impact plan  base=2bf76d990b59  head=bc8cc380c64e
  nothing to run
  skip brain conversation_style: no behaviour-affecting change for this tier
  ...
```

**4. Change the mock provider's logic** (`restaurants/mock.py`: close restaurants on 1/10 of days
instead of 1/7). The brain tier runs for the two suites that *declare* the tool.
`web_search` is skipped, and so is every voice run:

```text
change-impact plan  base=bc8cc380c64e  head=119d5b81f22d
  RUN  brain conversation_style  [fp 55b1f68eab419844]
         - implementation of tool `check_restaurant_availability` changed (normalized AST)
  RUN  brain restaurant_availability  [fp 4356be91dd5a5475]
         - implementation of tool `check_restaurant_availability` changed (normalized AST)
  skip voice conversation_style: no behaviour-affecting change for this tier
  skip voice restaurant_availability: no behaviour-affecting change for this tier
  skip brain web_search: no behaviour-affecting change for this tier
  skip voice web_search: no behaviour-affecting change for this tier
```

**5. A docs-only change** (`README.md`, `docs/tools.md`) takes the fast path. Nothing is exported
or rendered:

```text
change-impact plan  base=119d5b81f22d  head=3e5a4db0b26a
  note: fast path: no behaviour-relevant files changed (2 file(s): README.md, docs/tools.md)
  nothing to run
  skip brain conversation_style: no behaviour-relevant files changed (2 file(s): README.md, docs/tools.md)
  ...
```

**6. Two settings defaults.** Changing the voice (`gpt_live_voice: "marin"` → `"cedar"`) runs the
voice tier only:

```text
  RUN  voice conversation_style  [fp 0ffea925873da0fc]
         - setting `gpt_live_voice` default: 'marin' → 'cedar'
  ...  (voice restaurant_availability, voice web_search)
  skip brain conversation_style: no behaviour-affecting change for this tier
```

Changing backend reasoning effort (`"low"` → `"medium"`) runs both tiers for every suite:

```text
  RUN  brain conversation_style  [fp b896c724ebde5fae]
         - setting `gpt_live_backend_reasoning_effort` default: 'low' → 'medium'
  RUN  voice conversation_style  [fp 91bc177bbc15dc3b]
         - setting `gpt_live_backend_reasoning_effort` default: 'low' → 'medium'
  ...  (all six suite x tier pairs)
```

In CI the same plan is written as matrices to `$GITHUB_OUTPUT`. This is the actual output for
scenario 4:

```text
brain_matrix={"suite":["conversation_style","restaurant_availability"]}
run_brain=true
brain_fingerprints={"conversation_style":"55b1f68eab419844","restaurant_availability":"4356be91dd5a5475"}
voice_matrix={"suite":[]}
run_voice=false
voice_fingerprints={}
any=true
```

### Fingerprints: an identity for "the behaviour under test"

Every planned run carries a **fingerprint**: a hash of every head component that can affect that
(suite, tier) pair. Compare the brain `conversation_style` fingerprint after scenario 4
(`55b1f68eab419844`) with a forced `evals:full` plan for the docs-only commit that followed it.
That plan printed the same value, because a docs change can't change the behaviour under test.
Two commits with equal fingerprints should produce the same eval results, apart from sampling
noise. The runner stores the fingerprint in every result file (`--fingerprints`) and in the JUnit
properties, so a result can always be traced to the exact behaviour it measured. The fingerprint
is also a natural cache key, but the current code does not use it to skip runs (§8).

### Fallbacks and overrides

| Situation | Behaviour |
|---|---|
| Base tree can't be rendered (e.g. the composer didn't exist yet) | Every suite × tier runs, with the reason in the plan. Real example: `note: base tree could not be fingerprinted (ModuleNotFoundError: No module named 'voice_agent.prompts'); treating everything as changed` |
| Head tree can't be rendered | Everything runs. The unit tier fails the PR anyway. |
| One profile fails to render on one side | `profile.error:<profile>` runs brain + voice for that profile's suites. |
| A suite enables a tier but none of its cases use it | Skipped with `suite has no <tier>-tier cases`. |
| A suite was deleted | Listed as `suite removed`. |
| Label **`evals:full`**, `--force-all`, `EVALS_FORCE_ALL=1`, the nightly schedule, `workflow_dispatch` (defaults to force) | Everything runs, with `forced: label \`evals:full\``-style reasons. The fast path is bypassed. |
| Label **`evals:skip`** | Nothing runs, and every pair lists `label \`evals:skip\``. It wins over `evals:full`. |
| `--suites a,b` / `--tiers brain` | Filters the plan. Skipped pairs say `not selected`. |
| `--no-merge-base`, `--no-fast-path` | Diff against the base tip, or always fingerprint both trees. |

Labels reach the detector through `EVALS_LABELS` or `--labels`, never through shell interpolation.
Suite names from the (PR-controlled) tree are checked against `^[a-z0-9][a-z0-9_\-]{0,63}$`
before they are allowed into a CI matrix.

---

## 5. CI workflows

### `evals.yml`: paid evals, gated by the plan

```mermaid
flowchart LR
    Trig["pull_request (paths: agent/, prompts/, evals/, uv.lock)<br/>workflow_dispatch<br/>schedule 07:17 UTC nightly"] --> Plan
    Plan["plan<br/>evals.impact plan --format github<br/>+ fork gate"] -- "run_brain and can_spend" --> Brain
    Plan -- "run_voice and can_spend" --> Voice
    Brain["brain matrix<br/>one job per suite<br/>max-parallel 3, env: evals"] -- "success or skipped" --> Voice
    Voice["voice matrix<br/>one job per suite<br/>max-parallel 2, env: evals<br/>TTS cache"] --> Report
    Brain --> Report
    Plan --> Report["report<br/>aggregate results + plan<br/>sticky PR comment"]
```

The gates run from cheapest to most expensive:

1. **`paths` filter.** A PR that touches nothing agent-related never starts the workflow.
2. **`plan` job.** It needs a full clone (`fetch-depth: 0`) to export the merge-base tree. It emits
   `brain_matrix`, `voice_matrix`, per-tier fingerprints and a step-summary table, and uploads
   `eval-plan.json`.
3. **Secrets gate.** Fork PRs get `can_spend=false`, and paid jobs skip with a note ("A
   maintainer can run them via workflow_dispatch"). If `OPENAI_API_KEY` isn't configured, the
   runner's `--skip-if-no-key` writes *skipped* results and exits 0.
4. **Brain before voice.** The voice job requires `needs.brain.result` to be `success` or
   `skipped`. `skipped` is allowed because a voice-only change plans no brain jobs. If the brain
   tier fails, no GPT-Live minutes are spent, and the report explains why.
5. **Budget.** Each matrix job runs with `--max-cost-usd`, taken from the `max_cost_usd` dispatch
   input, then the `EVALS_MAX_COST_USD` repository variable, and defaulting to **$3 per job**.

Other details:

- **`environment: evals`** holds the `OPENAI_API_KEY` secret. You can attach protection rules to
  it, for example required reviewers, so that no PR can spend money without a human approving
  the run.
- **Concurrency.** There is one run per PR, and a new push cancels the stale one. Label events for
  labels outside `evals:*` get a unique no-op group, so adding a `docs` label can never cancel a
  real run. The `plan` job skips those events too.
- **TTS audio cache.** `evals/.cache/tts` is cached with `actions/cache`, keyed on
  `hashFiles('evals/suites/*.yaml')` and falling back to older caches. Synthesized callers are
  paid for once, not on every run.
- **Artifacts.** Each job uploads its results (JSON, JUnit XML, Markdown) and appends its
  Markdown to the job summary. The artifacts are kept for 30 days.
- **Sticky PR comment.** `report` runs even when earlier jobs failed. It combines every
  result with the plan and posts one comment marked `<!-- gpt-live-evals-report -->`. Later runs
  update that comment instead of adding new ones. The comment shows a results table (pass rate,
  min pass^k, p50 latency, p50 first response, cost), a collapsible list of failures, and a
  collapsible **change-impact plan** that lists what ran, what was skipped, and why. Fork PRs get
  the report in the job summary instead, because their token is read-only.
- **Nightly full run.** This catches changes that no diff can show, such as OpenAI updating a
  model on its side.

### `ci.yml`: free checks on every PR

Nothing in `ci.yml` needs a secret. A `changes` job (`dorny/paths-filter`) decides which areas
changed, and then these jobs run:

- **`lint`**: `ruff check` and `ruff format --check`.
- **`test`**: `pytest` over `agent/tests` and `evals/tests`.
- **`prompts`**: renders every profile for both targets with the real composer, builds every tool
  schema (`evals.impact probe --strict`), runs `evals validate`, checks that `suite.schema.json`
  is up to date, and dry-runs both paid tiers.
- **`frontend`**: lint, typecheck and build.

---

## 6. Running evals locally

```bash
uv sync --group evals

# free: schema + cross-check against the agent
uv run python -m evals validate
# → 3 suite(s) valid: conversation_style, restaurant_availability, web_search

# free: what would run, with cost estimates (no network, no key)
uv run python -m evals run --tier brain --dry-run
# → conversation_style: profile=concierge cases=4 trials=12 threshold=66% est≈$0.54
#   ...
#   estimated total ≈ $1.42 (upper-bound estimate)

# free: which suites x tiers does my change need? (head defaults to the working tree,
# including uncommitted and untracked files)
uv run python -m evals.impact plan --base origin/main
uv run python -m evals.impact plan --base origin/main --head HEAD --format json --output plan.json
uv run python -m evals.impact probe --strict        # render all profiles + tool schemas

# paid (needs OPENAI_API_KEY)
uv run python -m evals run --tier brain --suite restaurant_availability --max-cost-usd 2
uv run python -m evals run --tier voice --suite conversation_style --trials 5 --max-cost-usd 5
uv run python -m evals run --tier brain --no-judge          # deterministic checks only
uv run python -m evals report --results-dir evals/.results --plan plan.json
```

Useful `run` flags:

- `--suite` can be repeated or comma-separated.
- `--trials` overrides trials per case.
- `--concurrency` defaults to 4 for brain and 2 for voice.
- `--no-early-stop` runs every trial even after a case can no longer pass.
- `--voice-input text` uses the unverified smoke-test path.
- `--skip-if-no-key` writes skipped results and exits 0 when no key is set.

**Results.** Each suite writes `evals/.results/<tier>__<suite>.json` (the source of truth: every
trial, check, judge verdict, transcript, latency and cost), `.xml` (JUnit, for any CI UI) and
`.md` (a summary table). `EVALS_RESULTS_DIR` or `--results-dir` moves them.

**Exit codes:**

| code | meaning |
|---|---|
| `0` | all passed, or skipped (e.g. no key with `--skip-if-no-key`) |
| `1` | at least one case failed its threshold |
| `2` | configuration error: invalid suite, unknown suite name, profile doesn't render, missing key |
| `3` | budget exhausted before the run completed |

**Environment knobs:**

- `EVALS_JUDGE_MODEL` (default `gpt-5.6-luna`).
- `EVALS_TTS_MODEL`, `EVALS_TTS_VOICE`, `EVALS_TTS_CACHE`.
- `EVALS_PRICING_JSON`, which overrides the price table, for example
  `'{"gpt-5.6-luna": {"input": 1.25, "output": 10.0}}'` (USD per 1M tokens).
- `EVALS_RESULTS_DIR`, `EVALS_LOG_LEVEL`.
- `EVALS_FORCE_ALL`, `EVALS_LABELS` for the planner.

Note that the runners load `Settings()` like the agent does. A local `.env` can therefore change
the backend model or voice under test. Only `RESTAURANT_PROVIDER` is forced (to `mock`).

---

## 7. Best practices for evaluating GPT-Live agents

These are opinionated, and each one is implemented in this repo.

1. **Test the brain as text; save audio for what only audio can break.** Under responses
   delegation, tool choice and arguments are decisions of a text model. Testing them through a
   live audio session costs roughly 50× more and adds audio noise to what is a text problem.
2. **Reuse the production builders, not a copy of the config.** The brain tier imports
   `build_responses_options` and the tool registry. A test harness that re-declares the model
   name and tool JSON drifts from production without anyone noticing.
3. **Make tools deterministic under eval.** The mock provider seeds its data from
   `sha256(salt|restaurant|date)`. The same question gets the same availability, so a failure is
   about the agent and not the fixture, and rubrics can assert specific facts. For live tools
   like web search, assert behaviour (grounded, brief, no URLs), never facts.
4. **Inject "today" and assert relative dates relatively.** `$days_from_today: 1` tests
   "tomorrow" on any day, in the agent's timezone.
5. **Put deterministic checks before the judge, and never ask a judge to excuse a wrong tool
   call.** Word limits and URL regexes are free and never flake. The judge is for things only
   judgment can grade.
6. **Give the judge only the transcript and a binary rubric, and pin its model.** A judge that
   sees the prompt grades intent. A judge on a 1–10 scale drifts. A judge that silently upgrades
   with the agent moves the goalposts.
7. **Report pass^k.** Every caller is one sample. Early-stop failing cases, but never stop a
   passing case early.
8. **Test precision as well as recall.** Every suite has a "don't call a tool" case
   (`small_talk_no_tool`, `stable_fact_no_search`, `off_topic_redirect`). An agent that searches
   the web for "how many minutes are in three hours" adds latency on every call.
9. **Mark what you can't observe as skipped, not passed.** Hosted tools are invisible from the
   session, and a green check on an assertion that never ran is worse than no check.
10. **Freeze the synthetic caller.** Cache TTS audio so input audio is byte-identical across
    commits, and record ASR similarity so a misheard input isn't blamed on the agent.
11. **Measure latency in the voice tier.** On a phone call, silence is a failure mode. Voice-to-voice
    latency belongs in the same report as correctness.
12. **Fingerprint what the model sees.** Hash rendered prompts and tool JSON, not source files.
    That is what lets a docstring or reflow edit cost nothing while a one-word guardrail change
    re-runs exactly the right tier.
13. **When unsure, run it.** Unknown modules, new settings, new component kinds and unrenderable
    bases all mean "run everything". A false positive costs a few cents. A false negative ships an
    untested behaviour change.
14. **Cheap gates before expensive ones, and a hard budget on each.** Order the stages as unit,
    then brain, then voice. Reserve the budget *before* each trial, and price unknown models as
    the most expensive one.
15. **Run everything nightly.** Your diff isn't the only thing that changes: the model behind the
    API can change too, and only a scheduled full run notices.

---

## 8. Trade-offs and limitations

### What the design costs

- **The brain tier is an approximation.** In production the backend receives the conversation
  through GPT-Live's delegation channel. In the brain tier it receives the caller's words
  directly. A regression in how GPT-Live *phrases* a delegation won't show up until the voice
  tier runs.
- **Voice cases are slow.** Real-time audio means about 20–60 s per case.
- **TTS callers are too clean.** They have no accents, background noise or disfluencies. For
  robustness evals, substitute recorded or noise-augmented audio in `TTSCache.synthesize`.
- **Tool-code changes skip the voice tier by design.** That's right for refactors, but a tool
  change that alters *latency* (a slower provider) could affect filler behaviour, which only the
  voice tier would catch. Use `evals:full` for those.
- **One failing brain suite blocks every voice leg**, not just the voice leg for that suite. This
  is the cheap-before-expensive gate working as designed, but it is blunt.
- **`tool.impl` for the restaurant tool covers the whole `restaurants/` package**, including
  `opentable.py`, which evals never execute. Editing the OpenTable client re-runs the brain tier.
  That's conservative and cheap, but not strictly necessary.
- **pass^k with k = n is all-or-nothing.** Reports compute pass^n from n trials, so with 3 trials
  pass^3 is 1.0 only if all three passed and 0 otherwise. Early stopping also means failing cases
  report fewer trials, so "min pass^k" in the summary can mix different values of k. For a
  graded reliability estimate, run more trials than the k you care about (for example 10 trials,
  then read pass^3 from the JSON).
- **Fingerprints are recorded but not yet used as a cache.** Re-running a PR whose fingerprints
  match an earlier green run still pays again.

### Known gaps in the detector

- **`model.py` is classified as voice-runtime code.** It also contains `build_responses_options`,
  which the brain tier uses directly. If you change the backend options there (reasoning,
  verbosity, `parallel_tool_calls`), only the voice tier is planned; we checked this with a
  scratch commit. Changes to the backend *settings defaults* in `config.py` are routed
  correctly to both tiers. Until the routing is fixed, label such PRs `evals:full`.
- **A GitHub `paths` filter applies to label events too.** Adding `evals:full` to a PR that
  touches no agent paths won't start `evals.yml`. Use `workflow_dispatch` instead.

### Unverified: built without API keys

The runners are covered by offline tests with fake clients and sessions
(`evals/tests/test_runners_offline.py`), but they were **not run against the live OpenAI
service** while this repo was built. Treat the following as the first things to check when you
add a key:

- **Voice settle timing.** `SETTLE_S = 2.5` s of quiet may cut off a slow tool lookup that comes
  after a filler, or wait too long if GPT-Live backchannels. Tune it from real runs.
- **Delegated function calls in `session.history`.** The voice tier's tool assertions assume that
  delegated calls to `check_restaurant_availability` appear as `function_call` items in the
  history.
- **`--voice-input text`**, the commentary path, which is marked unverified in the CLI help.
- **Prices** in `runners/common.py` are estimates: $0.05/min for voice, $0.01 per web search,
  $15 per 1M TTS characters, and a per-model token table. Budgets are only as accurate as those
  numbers, so override them with `EVALS_PRICING_JSON`. The dry-run estimate always prices the
  brain tier at `gpt-5.6-luna` rates, whatever backend model is configured.
- **Judge model default.** `EVALS_JUDGE_MODEL` defaults to `gpt-5.6-luna`, the same model as the
  default backend. The judge is pinned separately, but by default it comes from the same model
  family it grades. Consider pinning a different model and calibrating it on a few hand-labeled
  transcripts.
- **Latency numbers from a CI runner** reflect the runner's network, not your production region.

---

## 9. Extending the system

### Add a suite

1. Create `evals/suites/<name>.yaml` with `name: <name>`, a `profile`, `tiers`, and every tool
   you assert on listed in `tools`. The `# yaml-language-server: $schema=../suite.schema.json`
   header gives you autocomplete.
2. Start with `tiers: [brain]`. Add `voice` once the brain tier is green.
3. Run `uv run python -m evals validate`, then `run --tier brain --suite <name> --dry-run`
   to see the cost, then a real run with `--max-cost-usd`.
4. Include at least one precision case (no tool call), and put every must-never-happen rule in
   `must_not_match`, where it is free, as well as in the rubric.

A new suite is its own `suite:<name>` component, so the PR that adds it runs it.

### Add a tool

Follow [`tools.md` §10](tools.md#10-tutorial-add-your-own-custom-tool). The step that matters for
evals is **`source_modules`** on the `ToolSpec` in `tools/registry.py`:

```python
ToolSpec(
    name="lookup_restaurant_notes",
    description="...",
    factory=_restaurant_notes,
    source_modules=("voice_agent.tools.restaurant_notes",),  # module or package
)
```

The detector resolves those modules to files, and edits to those files map to
`tool.impl:lookup_restaurant_notes`, which runs the brain tier of suites that declare the tool.
If you forget `source_modules`, nothing breaks. The files fall through to `code.other:<path>`, and
every edit re-runs *everything*. That is safe, just expensive. The tool's schema is fingerprinted
automatically either way. Declare the tool in the `tools` list of every suite that exercises it.

### Add a setting

New `Settings` fields are treated as `config.shared` (both tiers, every suite) by default. If a
field only affects the voice model, add it to `CONFIG_VOICE_ONLY` in `evals/impact/rules.py`. If
it can never affect behaviour (credentials, endpoints), make sure its name matches
`CONFIG_IGNORED`. A unit test should pin the routing either way.

### Add a tier

Suppose you want a `telephony` tier that uses 8 kHz, noise-augmented audio. You would:

1. add it to `Tier`/`TIERS` in `evals/schema.py`, then regenerate `suite.schema.json`;
2. implement the `TierRunner` protocol from `runners/harness.py`, meaning `setup`,
   `estimate_trial_usd`, `converse` (returning a `Transcript`) and `aclose`. Trials, early stop,
   assertions, judge and budget all come from the harness for free;
3. wire it into `cli.py` (`--tier` choices and runner construction);
4. teach `rules.py` which components affect it (for example, `code.voice_runtime` and
   `config.voice` probably should), and add its runner file to `RUNNER_CODE`;
5. add a matrix job to `evals.yml` after `voice`, gated the same way.
