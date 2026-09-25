"""Brain tier: the backend delegation model, text-only, via the OpenAI Responses API.

Under ``delegation="responses"`` GPT-Live hands every reasoning/tool decision to a backend
Responses model. This tier calls that model *directly* with exactly what GPT-Live would send
it — the composed backend instructions and ``build_responses_options`` (model, reasoning,
verbosity, parallel tool calls), plus tool schemas converted with the same function GPT-Live
uses — and runs the tool loop itself:

* function tools execute **for real** (the registry's tool, bound to the deterministic
  ``MockReservationProvider``), including argument validation and ``ToolError`` messages, so
  the model sees the exact errors it would see in production;
* ``web_search`` is an OpenAI hosted tool (``{"type": "web_search"}``) and runs server-side.

What this tier cannot see: how the *voice* model phrases, times, or interrupts. In production
the backend receives the conversation through GPT-Live's delegation channel; here it receives
the caller's words directly. That approximation is the price of being ~50x cheaper and fully
deterministic in its tool execution — the voice tier covers the rest.
"""

from __future__ import annotations

import json
import time
from typing import Any

from evals.runners import agent_bridge
from evals.runners.assertions import ToolCall, Transcript
from evals.runners.common import WEB_SEARCH_CALL_USD, text_cost
from evals.runners.harness import Conversation
from evals.schema import Case, Suite, Tier
from evals.toolschema import tools_to_responses_schemas

MAX_STEPS_PER_TURN = 6
"""Tool-loop guard: a model stuck calling tools is a failure, not an infinite bill."""


class BrainRunner:
    tier: Tier = "brain"

    def __init__(self, *, client: Any, concurrency: int = 4) -> None:
        self.client = client
        self.concurrency = concurrency
        self._settings: Any = None
        self._options: dict[str, Any] = {}
        self._schemas: list[dict[str, Any]] = []
        self._function_tools: dict[str, Any] = {}

    async def setup(self, suite: Suite) -> dict[str, Any]:
        from livekit.agents import llm

        self._settings = agent_bridge.load_settings()
        bundle = agent_bridge.compose_bundle(suite.profile, self._settings)
        tools = agent_bridge.resolve_tools(bundle, self._settings)
        self._options = agent_bridge.responses_options(self._settings, bundle)
        self._schemas = tools_to_responses_schemas(tools)
        self._function_tools = {
            t.info.name: t for t in tools if isinstance(t, (llm.FunctionTool, llm.RawFunctionTool))
        }
        return {
            "backend_model": self._options.get("model"),
            "reasoning": self._options.get("reasoning"),
            "text": self._options.get("text"),
            "prompt_fingerprint": getattr(bundle, "fingerprint", None),
            "tools": [s.get("name") or s.get("type") for s in self._schemas],
        }

    def estimate_trial_usd(self, suite: Suite, case: Case) -> float:
        turns = len(case.user_turns)
        model = str(self._options.get("model") or "")
        # ~2 model calls per turn with a few thousand tokens of instructions each, plus judge.
        return text_cost(model, 8_000 * turns, 800 * turns) + WEB_SEARCH_CALL_USD * turns + 0.01

    async def converse(self, suite: Suite, case: Case, trial: int) -> Conversation:
        transcript = Transcript()
        cost = 0.0
        first_latency: float | None = None
        started = time.monotonic()
        previous_id: str | None = None
        opts = self._options
        request: dict[str, Any] = {
            "model": opts["model"],
            "instructions": opts.get("instructions"),
            "tools": self._schemas,
        }
        for key in ("reasoning", "text", "parallel_tool_calls", "max_output_tokens"):
            if opts.get(key) is not None:
                request[key] = opts[key]
        if opts.get("tool_choice") is not None:
            request["tool_choice"] = opts["tool_choice"]

        for turn in case.user_turns:
            transcript.messages.append(("user", turn))
            pending: list[dict[str, Any]] = [{"role": "user", "content": turn}]
            for _step in range(MAX_STEPS_PER_TURN):
                t0 = time.monotonic()
                # Instructions are re-sent every call: previous_response_id does not carry them.
                chain = {"previous_response_id": previous_id} if previous_id else {}
                response = await self.client.responses.create(**request, **chain, input=pending)
                if first_latency is None:
                    first_latency = round(time.monotonic() - t0, 3)
                previous_id = response.id
                if response.usage is not None:
                    cost += text_cost(
                        opts["model"], response.usage.input_tokens, response.usage.output_tokens
                    )
                pending = []
                for item in response.output:
                    if item.type == "web_search_call":
                        action = getattr(item, "action", None)
                        query = getattr(action, "query", None)
                        transcript.tool_calls.append(ToolCall("web_search", {"query": query}))
                        cost += WEB_SEARCH_CALL_USD
                    elif item.type == "function_call":
                        output, is_error = await self._execute(item.name, item.arguments)
                        args = _safe_json(item.arguments)
                        transcript.tool_calls.append(ToolCall(item.name, args, output, is_error))
                        pending.append(
                            {
                                "type": "function_call_output",
                                "call_id": item.call_id,
                                "output": output,
                            }
                        )
                if not pending:
                    transcript.messages.append(("assistant", response.output_text or ""))
                    break
            else:
                transcript.messages.append(("assistant", "[tool loop limit reached]"))

        return Conversation(
            transcript=transcript,
            cost_usd=cost,
            latency_s=time.monotonic() - started,
            first_response_latency_s=first_latency,
            meta={"response_id": previous_id},
        )

    async def _execute(self, name: str, arguments: str) -> tuple[str, bool]:
        """Run a function tool the way LiveKit would: validate/coerce args with LiveKit's own
        ``prepare_function_arguments`` and surface ``ToolError`` text to the model."""
        from livekit.agents import ToolError
        from livekit.agents.llm.utils import prepare_function_arguments

        tool = self._function_tools.get(name)
        if tool is None:
            return f"Unknown function: {name}", True
        try:
            args, kwargs = prepare_function_arguments(fnc=tool, json_arguments=arguments)
            output = await tool(*args, **kwargs)
        except ToolError as exc:
            return exc.message, True
        except Exception as exc:
            return f"error: {type(exc).__name__}", True
        return output if isinstance(output, str) else json.dumps(output, default=str), False

    async def aclose(self) -> None:
        return None


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__raw__": raw}
    return value if isinstance(value, dict) else {"__value__": value}
