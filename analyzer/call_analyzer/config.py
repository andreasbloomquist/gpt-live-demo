"""Service configuration, read from environment variables and an optional ``.env`` file.

Secrets are :class:`~pydantic.SecretStr` so they never appear in logs or reprs. Nothing here is
required at import time: the ``analyze`` and ``seed`` CLI commands run without a bearer token,
and the heuristic provider runs without an API key. Each entry point checks what *it* needs and
fails fast with an actionable message (see :meth:`Settings.require_api_token`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderChoice = Literal["auto", "openai", "heuristic"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]

# The frontend's .env.example ships this value; it is fine on a laptop, never on a server.
PLACEHOLDER_TOKEN = "change-me-shared-secret"
MIN_TOKEN_LENGTH = 16


class ConfigurationError(RuntimeError):
    """Raised at startup when the configuration can't work. The message says how to fix it."""


class Settings(BaseSettings):
    """All analyzer settings. Environment variable names are given explicitly per field."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- HTTP API ----------------------------------------------------------------------------
    # Shared with the agent (which POSTs calls) and the frontend's server routes (which read).
    api_token: SecretStr | None = Field(default=None, validation_alias="CALL_ANALYZER_TOKEN")
    host: str = Field(default="127.0.0.1", validation_alias="ANALYZER_HOST")
    port: int = Field(default=8080, ge=1, le=65535, validation_alias="ANALYZER_PORT")
    # 2 MiB. A 30-minute call is ~50 KB of JSON, so this is generous yet bounds memory per request.
    max_body_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1024, validation_alias="ANALYZER_MAX_BODY_BYTES"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    # --- Storage -----------------------------------------------------------------------------
    db_path: Path = Field(default=Path("./data/calls.db"), validation_alias="ANALYZER_DB_PATH")

    # --- LLM provider ------------------------------------------------------------------------
    provider: ProviderChoice = Field(default="auto", validation_alias="ANALYZER_PROVIDER")
    base_url: str | None = Field(default=None, validation_alias="ANALYZER_BASE_URL")
    api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANALYZER_API_KEY", "OPENAI_API_KEY")
    )
    # A small, cheap model is enough to grade a transcript against a fixed rubric.
    model: str = Field(default="gpt-5.4-mini", validation_alias="ANALYZER_MODEL")
    # Sent as `reasoning_effort`. Non-reasoning models and many OpenAI-compatible servers reject
    # the parameter, so set ANALYZER_REASONING_EFFORT= (empty) for those.
    reasoning_effort: ReasoningEffort | None = Field(
        default="low", validation_alias="ANALYZER_REASONING_EFFORT"
    )
    # Includes reasoning tokens on reasoning models; the JSON answer itself is ~1-2k tokens.
    max_output_tokens: int = Field(
        default=8000, ge=256, le=64000, validation_alias="ANALYZER_MAX_OUTPUT_TOKENS"
    )
    request_timeout_s: float = Field(
        default=60.0, gt=0, le=600, validation_alias="ANALYZER_TIMEOUT_S"
    )
    # Request-level retries inside the OpenAI SDK (429/5xx/timeouts, honours Retry-After).
    sdk_max_retries: int = Field(default=2, ge=0, le=10, validation_alias="ANALYZER_SDK_RETRIES")
    # Transcript budget for the prompt (~4 chars/token). Longer calls keep their start and end.
    max_prompt_chars: int = Field(
        default=60_000, ge=2_000, le=1_000_000, validation_alias="ANALYZER_MAX_PROMPT_CHARS"
    )

    # --- Background worker -------------------------------------------------------------------
    concurrency: int = Field(default=2, ge=1, le=32, validation_alias="ANALYZER_CONCURRENCY")
    # Job-level attempts (each may include SDK retries). Also bounds crash loops on restart.
    max_attempts: int = Field(default=3, ge=1, le=10, validation_alias="ANALYZER_MAX_ATTEMPTS")
    retry_base_s: float = Field(default=30.0, ge=0, le=3600, validation_alias="ANALYZER_RETRY_BASE_S")
    job_timeout_s: float = Field(
        default=300.0, gt=0, le=3600, validation_alias="ANALYZER_JOB_TIMEOUT_S"
    )
    poll_interval_s: float = Field(
        default=2.0, gt=0, le=60, validation_alias="ANALYZER_POLL_INTERVAL_S"
    )

    @field_validator("reasoning_effort", "base_url", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        # `FOO=` in a .env file means "unset", not "the empty string".
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("api_key", "api_token", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    def resolved_provider(self) -> Literal["openai", "heuristic"]:
        """``auto`` means: use the LLM when a key is configured, otherwise stay offline."""
        if self.provider == "auto":
            return "openai" if self.api_key is not None else "heuristic"
        return self.provider

    def require_api_token(self) -> str:
        """Return the bearer token the HTTP API requires, or explain how to configure one."""
        if self.api_token is None:
            raise ConfigurationError(
                "CALL_ANALYZER_TOKEN is not set. The API refuses to start unauthenticated; set it "
                "to a long random string (e.g. `openssl rand -hex 32`) shared with the agent "
                "and the frontend."
            )
        token = self.api_token.get_secret_value()
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigurationError(
                f"CALL_ANALYZER_TOKEN must be at least {MIN_TOKEN_LENGTH} characters."
            )
        return token
