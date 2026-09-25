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

import time
from typing import Any

from evals.runners import agent_bridge
from evals.runners.assertions import ToolCall, Transcript, parse_tool_arguments
from evals.runners.common import WEB_SEARCH_CALL_USD, estimate_brain_trial_usd, text_cost
from evals.runners.harness import Conversation
from evals.schema import Case, Suite, Tier
from evals.toolschema import schema_tool_name, tools_to_responses_schemas

DEFAULT_CONCURRENCY = 4
MAX_STEPS_PER_TURN = 6
"""Tool-loop guard: a model stuck calling tools is a failure, not an infinite bill."""
TOOL_LOOP_LIMIT_REPLY = "[tool loop limit reached]"
# Optional ``build_responses_options`` keys forwarded to every backend request when set.
_FORWARDED_OPTIONS = (
    "reasoning",
    "text",
    "parallel_tool_calls",
    "max_output_tokens",
    "tool_choice",
)


class BrainRunner:
    tier: Tier = "brain"

    def __init__(self, *, client: Any, concurrency: int = DEFAULT_CONCURRENCY) -> None:
        self.client = client
        self.concurrency = concurrency
        self._settings: Any = None
        self._options: dict[str, Any] = {}
        self._schemas: list[dict[str, Any]] = []
        self._tool_context: Any = None

    async def setup(self, suite: Suite) -> dict[str, Any]:
        from livekit.agents import llm

        self._settings = agent_bridge.load_settings()
        bundle = agent_bridge.compose_bundle(suite.profile, self._settings)
        tools = agent_bridge.resolve_tools(bundle, self._settings)
        self._options = agent_bridge.responses_options(self._settings, bundle)
        self._schemas = tools_to_responses_schemas(tools)
        self._tool_context = llm.ToolContext(
            [t for t in tools if isinstance(t, (llm.FunctionTool, llm.RawFunctionTool))]
        )
        return {
            "backend_model": self._options.get("model"),
            "reasoning": self._options.get("reasoning"),
            "text": self._options.get("text"),
            "prompt_fingerprint": getattr(bundle, "fingerprint", None),
            "tools": [schema_tool_name(s) for s in self._schemas],
        }

    def estimate_trial_usd(self, suite: Suite, case: Case) -> float:
        model = str(self._options.get("model") or "")
        return estimate_brain_trial_usd(model, len(case.user_turns))

    async def converse(self, suite: Suite, case: Case, trial: int) -> Conversation:
        transcript = Transcript()
        cost = 0.0
        first_latency: float | None = None
        started = time.monotonic()
        previous_id: str | None = None
        incomplete: list[str] = []
        opts = self._options
        request: dict[str, Any] = {
            "model": opts["model"],
            "instructions": opts.get("instructions"),
            "tools": self._schemas,
        }
        request.update({k: opts[k] for k in _FORWARDED_OPTIONS if opts.get(k) is not None})

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
                if getattr(response, "status", None) == "incomplete":
                    # e.g. max_output_tokens hit: the reply may be cut off, which explains
                    # a failure that would otherwise look like the model's choice.
                    details = getattr(response, "incomplete_details", None)
                    incomplete.append(str(getattr(details, "reason", None) or "unknown"))
                if response.usage is not None:
                    cost += text_cost(
                        opts["model"], response.usage.input_tokens, response.usage.output_tokens
                    )
                pending, tool_cost = await self._handle_tool_calls(response.output, transcript)
                cost += tool_cost
                if not pending:
                    transcript.messages.append(("assistant", response.output_text or ""))
                    break
            else:
                transcript.messages.append(("assistant", TOOL_LOOP_LIMIT_REPLY))
                # The last response still has unanswered function calls, so the API would
                # reject chaining another user turn onto it: end the conversation here.
                break

        return Conversation(
            transcript=transcript,
            cost_usd=cost,
            latency_s=time.monotonic() - started,
            first_response_latency_s=first_latency,
            meta={"response_id": previous_id, "incomplete_responses": incomplete},
        )

    async def _handle_tool_calls(
        self, output_items: list[Any], transcript: Transcript
    ) -> tuple[list[dict[str, Any]], float]:
        """Record the response's tool calls and execute its function calls.

        Returns the ``function_call_output`` items for the next request (empty when the model
        answered without calling a function) and the cost of hosted web searches.
        """
        outputs: list[dict[str, Any]] = []
        cost = 0.0
        for item in output_items:
            if item.type == "web_search_call":
                query = getattr(getattr(item, "action", None), "query", None)
                transcript.tool_calls.append(ToolCall("web_search", {"query": query}))
                cost += WEB_SEARCH_CALL_USD
            elif item.type == "function_call":
                output, is_error = await self._execute(item.name, item.arguments, item.call_id)
                args = parse_tool_arguments(item.arguments)
                transcript.tool_calls.append(ToolCall(item.name, args, output, is_error))
                outputs.append(
                    {"type": "function_call_output", "call_id": item.call_id, "output": output}
                )
        return outputs, cost

    async def _execute(self, name: str, arguments: str, call_id: str = "eval") -> tuple[str, bool]:
        """Run a function tool through LiveKit's own ``execute_function_call``: the same argument
        parsing/validation (``prepare_function_arguments``) and the same output text for
        results, ``ToolError`` messages, unknown tools and unexpected exceptions ("An internal
        error occurred") that LiveKit reports to the model in production."""
        from livekit.agents import llm

        result = await llm.utils.execute_function_call(
            llm.FunctionToolCall(name=name, arguments=arguments, call_id=call_id),
            self._tool_context,
        )
        return result.fnc_call_out.output, result.fnc_call_out.is_error

    async def aclose(self) -> None:
        return None
