"""LLM-as-judge through the OpenAI SDK, against OpenAI or any OpenAI-compatible endpoint.

Why Chat Completions (``chat.completions.parse``) rather than the Responses API: it is the one
surface that OpenAI-compatible servers (Azure OpenAI, Gemini's compatibility endpoint, Groq,
Ollama, vLLM, LiteLLM...) actually implement, including ``response_format`` with a JSON schema.
The SDK's ``parse`` helper derives a strict JSON schema from our pydantic model and validates the
answer back into it, so there is no hand-written schema to drift.

Retries happen at two levels on purpose. The SDK retries individual requests (429, 5xx,
timeouts; it honours ``Retry-After``) with short backoff. If that still fails, we raise a
retryable :class:`ProviderError` and the worker re-queues the whole job minutes later, which
survives restarts and rides out longer rate-limit windows without holding a worker slot.
"""

from __future__ import annotations

import logging
from typing import Any

import openai
import pydantic

from ..config import ConfigurationError, Settings
from ..models import AssessmentDraft, CallRecord, Metrics, ProviderName
from ..prompt import build_prompt
from ..rubric import Rubric
from .base import ProviderError

logger = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 300


def _describe(exc: openai.APIStatusError) -> str:
    """A short, secret-free description of an API error for storage and the UI."""
    message = exc.message if isinstance(exc.message, str) else ""
    return f"{type(exc).__name__} (HTTP {exc.status_code}): {message[:_MAX_ERROR_CHARS]}"


class OpenAIProvider:
    name: ProviderName = "openai"

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        *,
        model: str,
        max_prompt_chars: int,
        max_output_tokens: int,
        reasoning_effort: str | None = None,
    ) -> None:
        self._client = client
        self.model = model
        self._max_prompt_chars = max_prompt_chars
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAIProvider:
        if settings.api_key is None:
            raise ConfigurationError(
                "The openai provider needs an API key: set ANALYZER_API_KEY (or OPENAI_API_KEY). "
                "Any non-empty value works for keyless local servers such as Ollama. "
                "Use ANALYZER_PROVIDER=heuristic to run offline."
            )
        client = openai.AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            timeout=settings.request_timeout_s,
            max_retries=settings.sdk_max_retries,
        )
        return cls(
            client,
            model=settings.model,
            max_prompt_chars=settings.max_prompt_chars,
            max_output_tokens=settings.max_output_tokens,
            reasoning_effort=settings.reasoning_effort,
        )

    async def assess(self, record: CallRecord, metrics: Metrics, rubric: Rubric) -> AssessmentDraft:
        prompt = build_prompt(record, metrics, rubric, budget=self._max_prompt_chars)
        if prompt.omitted_items:
            logger.info(
                "call too long for prompt budget; trimmed middle",
                extra={"call_id": record.call_id, "omitted_items": prompt.omitted_items},
            )
        extra: dict[str, Any] = {}
        if self._reasoning_effort is not None:
            extra["reasoning_effort"] = self._reasoning_effort
        try:
            completion = await self._client.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt.system},
                    {"role": "user", "content": prompt.user},
                ],
                response_format=AssessmentDraft,
                max_completion_tokens=self._max_output_tokens,
                **extra,
            )
        except openai.RateLimitError as exc:
            raise ProviderError(_describe(exc), retryable=True) from exc
        except openai.APIStatusError as exc:
            # 408/409/5xx are transient; other 4xx (bad key, unknown model, bad params) are not.
            retryable = exc.status_code in (408, 409) or exc.status_code >= 500
            raise ProviderError(_describe(exc), retryable=retryable) from exc
        except openai.APITimeoutError as exc:
            raise ProviderError("LLM request timed out", retryable=True) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError("could not connect to the LLM endpoint", retryable=True) from exc
        except openai.LengthFinishReasonError as exc:
            # Usually reasoning ate the token budget; a retry rarely helps without a config change.
            raise ProviderError(
                "LLM answer was cut off at max tokens; raise ANALYZER_MAX_OUTPUT_TOKENS or lower "
                "ANALYZER_REASONING_EFFORT",
                retryable=False,
            ) from exc
        except openai.ContentFilterFinishReasonError as exc:
            raise ProviderError("LLM answer was blocked by a content filter", retryable=False) from exc
        except pydantic.ValidationError as exc:
            # The server ignored or doesn't support strict JSON schema. Models are stochastic, so
            # a later attempt may well succeed.
            raise ProviderError(
                f"LLM returned output that doesn't match the schema ({exc.error_count()} errors)",
                retryable=True,
            ) from exc

        if not completion.choices:
            raise ProviderError("LLM returned no choices", retryable=True)
        message = completion.choices[0].message
        if message.refusal:
            raise ProviderError("LLM refused to grade the call", retryable=False)
        if message.parsed is None:
            raise ProviderError("LLM returned an empty answer", retryable=True)
        return message.parsed

    async def aclose(self) -> None:
        await self._client.close()
