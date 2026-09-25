"""Voice tier: real GPT-Live sessions through LiveKit's ``AgentSession``, driven by *audio*.

Why audio, not ``session.run(user_input=...)``
----------------------------------------------
LiveKit's usual text-driven testing (``session.run`` → ``RunResult.expect`` / ``.judge``) was
built for text-capable LLMs. GPT-Live is a ``DuplexModel``: it only speaks, and the
``DuplexRealtimeAdapter`` resolves each reply from the audio the model produces. LiveKit
1.8.3 therefore refuses a DuplexModel under a *text simulation*
(``agent_activity.py``: "a DuplexModel speaks only through audio … run
`lk agent simulate audio` instead"). Outside a simulation ``session.run`` does not raise —
the adapter forwards typed input to GPT-Live as a one-off commentary ask
(``GPTLiveSession._generate_reply``) — but that is a different code path from a caller
speaking, and it bypasses the model's own turn-taking. It is available here only as
``--voice-input text`` (unverified against the live service; use for smoke tests).

The default is the faithful path: each scripted user turn is synthesized with OpenAI TTS
(24 kHz mono PCM, cached on disk so inputs are identical across runs and paid for once) and
streamed in real time through a custom :class:`livekit.agents.voice.io.AudioInput` into a real
``AgentSession(llm=GPTLiveModel(...))`` built by production code (``build_gpt_live_model``,
``VoiceAgent``). Between turns the "microphone" streams silence, as a real line does, so the
full-duplex model decides turn-taking itself. The model's audio goes to an ``AudioOutput``
sink that paces playback like a speaker, so LiveKit's playout/transcript sync behaves as in a
room. After the conversation settles, grading runs on ``session.history`` — the same
``ChatContext`` LiveKit's own ``evals.JudgeGroup`` consumes.

Trade-offs (documented in evals/README.md): real-time pacing makes a case take roughly as long
as the conversation (~20–60 s); synthetic TTS voices are cleaner than real callers (no accents,
noise or disfluencies — swap in recorded/noised audio for robustness evals); provider-side
tools (``web_search``) run inside OpenAI and are not observable as function-call events, so
expectations about them are *skipped* in this tier (the brain tier asserts them).
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evals.paths import EVALS_DIR
from evals.runners import agent_bridge
from evals.runners.assertions import ToolCall, Transcript, parse_tool_arguments
from evals.runners.common import (
    TTS_USD_PER_1M_CHARS,
    VOICE_USD_PER_MINUTE,
    estimate_voice_trial_usd,
    text_cost,
)
from evals.runners.harness import Conversation
from evals.schema import Case, Suite, Tier

logger = logging.getLogger("evals.voice")

DEFAULT_CONCURRENCY = 2
"""Real-time audio sessions: keep concurrency (and rate limits) modest."""

SAMPLE_RATE = 24_000  # GPT-Live's native rate; OpenAI TTS "pcm" is 24 kHz s16le mono
BYTES_PER_SAMPLE = 2  # s16le
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = SAMPLES_PER_FRAME * BYTES_PER_SAMPLE

TTS_MODEL = os.environ.get("EVALS_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("EVALS_TTS_VOICE", "alloy")
TTS_CACHE_DIR = Path(os.environ.get("EVALS_TTS_CACHE", EVALS_DIR / ".cache" / "tts"))

SETTLE_S = 2.5
"""Quiet time (no speech, no tool activity) after which the agent's turn counts as done.
Long enough to cover "let me check…" → tool call → answer, short enough to keep cases fast."""
TURN_TIMEOUT_S = 60.0
LEAD_IN_SILENCE_S = 0.6
"""Silence played before each scripted turn, like the pause before a caller speaks."""
SETTLE_POLL_S = 0.2
MIC_RESYNC_AFTER_S = 1.0
"""A microphone that fell this far behind real time (event-loop stall) restarts its clock
instead of bursting the backlog."""
LATENCY_ONSET_SLACK_S = 0.5
"""Agent audio that starts this shortly before the caller finished still counts as the reply
(barge-in / end-of-speech detection racing the last frames)."""


# --------------------------------------------------------------------------------------------
# Audio I/O
# --------------------------------------------------------------------------------------------


class TTSCache:
    """Synthesizes user turns once and replays them forever after.

    Caching is not only about cost: identical input audio across runs removes one source of
    variance, so a pass-rate change between two commits is about the agent, not the TTS.
    """

    INSTRUCTIONS = "Speak naturally, like a person calling a restaurant concierge."

    def __init__(self, client: Any, *, model: str = TTS_MODEL, voice: str = TTS_VOICE) -> None:
        self.client = client
        self.model = model
        self.voice = voice
        self.cost_usd = 0.0
        self._locks: dict[Path, asyncio.Lock] = {}

    def _path(self, text: str) -> Path:
        # Everything that changes the audio is in the key (hex digest: no path traversal).
        material = f"{self.model}|{self.voice}|{self.INSTRUCTIONS}|{text}"
        return TTS_CACHE_DIR / f"{hashlib.sha256(material.encode()).hexdigest()[:24]}.pcm"

    async def synthesize(self, text: str) -> bytes:
        path = self._path(text)
        # Concurrent trials share user turns: synthesize (and pay for) each clip once.
        async with self._locks.setdefault(path, asyncio.Lock()):
            if path.is_file():
                return await asyncio.to_thread(path.read_bytes)
            response = await self.client.audio.speech.create(
                model=self.model,
                voice=self.voice,
                input=text,
                response_format="pcm",
                instructions=self.INSTRUCTIONS,
            )
            pcm: bytes = response.content
            self.cost_usd += len(text) * TTS_USD_PER_1M_CHARS / 1_000_000
            await asyncio.to_thread(_write_atomically, path, pcm)
            return pcm


def _write_atomically(path: Path, data: bytes) -> None:
    """Write-then-rename, so an interrupted run never leaves a truncated clip cached."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def build_microphone() -> Any:
    """A real-time paced ``AudioInput`` that plays scripted PCM, and silence in between."""
    from livekit import rtc
    from livekit.agents.voice import io

    class ScriptedMicrophone(io.AudioInput):
        def __init__(self) -> None:
            super().__init__(label="evals.scripted_microphone")
            self._pending = bytearray()
            self._drained = asyncio.Event()
            self._drained.set()
            self._next_at: float | None = None
            self._closed = False
            self.speech_ended_at: float | None = None

        def say(self, pcm: bytes) -> None:
            silence = b"\x00" * (int(SAMPLE_RATE * LEAD_IN_SILENCE_S) * BYTES_PER_SAMPLE)
            self._pending.extend(silence + pcm)
            self._drained.clear()

        async def wait_spoken(self) -> None:
            await self._drained.wait()

        def close(self) -> None:
            self._closed = True

        async def __anext__(self) -> rtc.AudioFrame:
            if self._closed:
                raise StopAsyncIteration
            now = time.monotonic()
            if self._next_at is None or self._next_at < now - MIC_RESYNC_AFTER_S:
                self._next_at = now
            if self._next_at > now:
                await asyncio.sleep(self._next_at - now)
            self._next_at += FRAME_MS / 1000
            chunk = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            if chunk and not self._pending:
                self.speech_ended_at = time.monotonic()
                self._drained.set()
            chunk = chunk.ljust(FRAME_BYTES, b"\x00")
            return rtc.AudioFrame(chunk, SAMPLE_RATE, 1, SAMPLES_PER_FRAME)

    return ScriptedMicrophone()


def build_speaker() -> Any:
    """An ``AudioOutput`` that behaves like a speaker (real-time playout) and records when the
    agent's audio starts, for voice-to-voice latency."""
    from livekit.agents.voice import io

    class PacedSpeaker(io.AudioOutput):
        """Plays each segment in real time from its first frame. A segment stays *playing*
        after ``flush()`` until its audio has run out, and ``clear_buffer()`` interrupts it at
        any point before that, exactly like a room or console sink. (LiveKit always calls
        ``flush()`` before ``clear_buffer()`` when it interrupts a reply.)"""

        def __init__(self) -> None:
            super().__init__(
                label="evals.paced_speaker",
                capabilities=io.AudioOutputCapabilities(pause=False),
                sample_rate=None,
            )
            self._started_at: float | None = None  # monotonic start of the current segment
            self._captured_s = 0.0
            self._finish_timer: asyncio.TimerHandle | None = None  # set once flushed
            self._idle = asyncio.Event()
            self._idle.set()
            self.audio_starts: list[float] = []
            self.audio_seconds = 0.0

        @property
        def playing(self) -> bool:
            return self._started_at is not None

        async def capture_frame(self, frame: Any) -> None:
            if self._finish_timer is not None:
                # A speaker plays segments in order: the next starts when this one has played.
                await self._idle.wait()
            await super().capture_frame(frame)
            if self._started_at is None:
                self._started_at = time.monotonic()
                self._idle.clear()
                self.audio_starts.append(self._started_at)
                self.on_playback_started(created_at=time.time())
            self._captured_s += frame.duration
            self.audio_seconds += frame.duration

        def flush(self) -> None:
            super().flush()
            if self._started_at is None or self._finish_timer is not None:
                return
            remaining = max(0.0, self._started_at + self._captured_s - time.monotonic())
            self._finish_timer = asyncio.get_running_loop().call_later(
                remaining, self._end_segment, False
            )

        def clear_buffer(self) -> None:
            self._end_segment(True)

        def _end_segment(self, interrupted: bool) -> None:
            if self._started_at is None:
                return
            played = self._captured_s
            if interrupted:
                played = min(max(0.0, time.monotonic() - self._started_at), played)
            if self._finish_timer is not None:
                self._finish_timer.cancel()
            self._started_at, self._captured_s, self._finish_timer = None, 0.0, None
            self._idle.set()
            self.on_playback_finished(playback_position=played, interrupted=interrupted)

    return PacedSpeaker()


# --------------------------------------------------------------------------------------------
# Turn tracking
# --------------------------------------------------------------------------------------------


@dataclass
class _Tracker:
    """Watches session events to decide when the agent has finished responding."""

    last_activity: float = 0.0
    agent_state: str = "initializing"
    assistant_items: int = 0
    closed: str | None = None
    """Why the session closed on its own (e.g. a GPT-Live error), if it did."""

    def attach(self, session: Any) -> None:
        self.last_activity = time.monotonic()

        def touch(*_: Any) -> None:
            self.last_activity = time.monotonic()

        def on_state(ev: Any) -> None:
            self.agent_state = ev.new_state
            touch()

        def on_item(ev: Any) -> None:
            if getattr(ev.item, "role", None) == "assistant":
                self.assistant_items += 1
            touch()

        def on_close(ev: Any) -> None:
            error = getattr(ev, "error", None)
            self.closed = f"{ev.reason}" + (f": {error}" if error else "")

        session.on("close", on_close)
        session.on("agent_state_changed", on_state)
        session.on("conversation_item_added", on_item)
        session.on("function_tools_executed", touch)
        session.on("user_state_changed", touch)

    async def wait_settled(self, speaker: Any, *, since_items: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.closed is None:
            await asyncio.sleep(SETTLE_POLL_S)
            replied = self.assistant_items > since_items
            quiet = time.monotonic() - self.last_activity >= SETTLE_S
            if replied and quiet and not speaker.playing and self.agent_state != "speaking":
                return True
        return False


def _raise_if_closed(tracker: _Tracker) -> None:
    """A session that closed mid-conversation (GPT-Live error, disconnect) is a crashed trial
    with a clear reason, not a slow timeout followed by grading an empty transcript."""
    if tracker.closed is not None:
        raise RuntimeError(f"GPT-Live session closed: {tracker.closed}")


def _transcript_from_history(history: Any, unobservable: frozenset[str]) -> Transcript:
    """Reduce LiveKit's ``ChatContext`` to the tier-agnostic :class:`Transcript`."""
    transcript = Transcript(unobservable_tools=unobservable)
    outputs = {item.call_id: item for item in history.items if item.type == "function_call_output"}
    for item in history.items:
        if item.type == "message" and item.role in ("user", "assistant"):
            text = (item.text_content or "").strip()
            if text:
                transcript.messages.append((item.role, text))
        elif item.type == "function_call":
            out = outputs.get(item.call_id)
            transcript.tool_calls.append(
                ToolCall(
                    item.name,
                    parse_tool_arguments(item.arguments),
                    getattr(out, "output", None),
                    bool(getattr(out, "is_error", False)),
                )
            )
    return transcript


def _asr_similarity(scripted: list[str], heard: list[str]) -> list[float]:
    """How well GPT-Live's transcription of our TTS matches the script. Low values mean the
    *input* was misheard — a failure then says little about the agent's reasoning."""
    return [
        round(difflib.SequenceMatcher(a=s.lower(), b=h.lower()).ratio(), 2)
        for s, h in zip(scripted, heard, strict=False)
    ]


# --------------------------------------------------------------------------------------------


class VoiceRunner:
    tier: Tier = "voice"

    def __init__(
        self,
        *,
        client: Any,
        concurrency: int = DEFAULT_CONCURRENCY,
        input_mode: Literal["audio", "text"] = "audio",
    ) -> None:
        self.client = client
        self.concurrency = concurrency
        self.input_mode = input_mode
        self.tts = TTSCache(client)
        self._settings: Any = None
        self._unobservable: frozenset[str] = frozenset()

    async def setup(self, suite: Suite) -> dict[str, Any]:
        from evals.toolschema import tools_to_responses_schemas

        self._settings = agent_bridge.load_settings()
        bundle = agent_bridge.compose_bundle(suite.profile, self._settings)
        schemas = tools_to_responses_schemas(agent_bridge.resolve_tools(bundle, self._settings))
        # Hosted tools (type != "function") execute inside OpenAI: no function_call events.
        provider_types = {s.get("type") for s in schemas if s.get("type") != "function"}
        self._unobservable = frozenset(str(t) for t in provider_types if t)
        return {
            "voice_model": self._settings.gpt_live_model,
            "voice": self._settings.gpt_live_voice,
            "backend_model": self._settings.gpt_live_backend_model,
            "input_mode": self.input_mode,
            "tts": f"{self.tts.model}/{self.tts.voice}" if self.input_mode == "audio" else None,
            "prompt_fingerprint": getattr(bundle, "fingerprint", None),
        }

    def estimate_trial_usd(self, suite: Suite, case: Case) -> float:
        return estimate_voice_trial_usd(len(case.user_turns))

    async def converse(self, suite: Suite, case: Case, trial: int) -> Conversation:
        from livekit.agents import AgentSession

        from voice_agent.agent import VoiceAgent
        from voice_agent.model import build_gpt_live_model

        settings = self._settings
        # A fresh bundle per trial: runtime variables (today's date) exactly as in production.
        bundle = agent_bridge.compose_bundle(suite.profile, settings)
        tools = agent_bridge.resolve_tools(bundle, settings)
        session: AgentSession[None] = AgentSession(llm=build_gpt_live_model(settings, bundle))
        speaker = build_speaker()
        session.output.audio = speaker
        mic = build_microphone() if self.input_mode == "audio" else None
        if mic is not None:
            session.input.audio = mic
        tracker = _Tracker()
        tracker.attach(session)

        started = time.monotonic()
        latencies: list[float] = []
        timeouts = 0
        tts_cost_before = self.tts.cost_usd
        try:
            await session.start(VoiceAgent(bundle, tools))
            # Let the greeting (VoiceAgent.on_enter) finish so turn 1 isn't barge-in.
            await tracker.wait_settled(speaker, since_items=0, timeout=TURN_TIMEOUT_S)
            for turn in case.user_turns:
                _raise_if_closed(tracker)
                before_items = tracker.assistant_items
                if mic is not None:
                    pcm = await self.tts.synthesize(turn)
                    mic.say(pcm)
                    await mic.wait_spoken()
                    spoke_at = mic.speech_ended_at or time.monotonic()
                else:
                    spoke_at = time.monotonic()
                    await session.run(user_input=turn)
                settled = await tracker.wait_settled(
                    speaker, since_items=before_items, timeout=TURN_TIMEOUT_S
                )
                timeouts += not settled
                starts = [s for s in speaker.audio_starts if s >= spoke_at - LATENCY_ONSET_SLACK_S]
                if starts:
                    latencies.append(round(max(0.0, starts[0] - spoke_at), 3))
            _raise_if_closed(tracker)
            history = session.history.copy()
            usage = session.usage
        finally:
            if mic is not None:
                mic.close()
            try:
                await session.aclose()
            except Exception:
                logger.warning("closing the voice session failed", exc_info=True)
        elapsed = time.monotonic() - started

        transcript = _transcript_from_history(history, self._unobservable)
        heard = [text for role, text in transcript.messages if role == "user"]
        cost = elapsed / 60 * VOICE_USD_PER_MINUTE + (self.tts.cost_usd - tts_cost_before)
        cost += _backend_cost(usage, exclude_model=settings.gpt_live_model)
        return Conversation(
            transcript=transcript,
            cost_usd=cost,
            latency_s=elapsed,
            first_response_latency_s=latencies[0] if latencies else None,
            meta={
                "voice_to_voice_latency_s": latencies,
                "turn_timeouts": timeouts,
                "agent_audio_s": round(speaker.audio_seconds, 2),
                "asr_similarity": _asr_similarity(case.user_turns, heard)
                if mic is not None
                else None,
            },
        )

    async def aclose(self) -> None:
        return None


def _backend_cost(usage: Any, *, exclude_model: str) -> float:
    """Best-effort cost of backend/tool tokens from ``AgentSession.usage`` (the voice model
    itself is billed per minute and accounted from wall-clock time)."""
    total = 0.0
    for entry in getattr(usage, "model_usage", None) or []:
        model = getattr(entry, "model", "") or ""
        if model == exclude_model:
            continue
        tokens_in = getattr(entry, "input_tokens", 0) or 0
        tokens_out = getattr(entry, "output_tokens", 0) or 0
        if tokens_in or tokens_out:
            total += text_cost(model, tokens_in, tokens_out)
    return total
