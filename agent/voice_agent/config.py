"""Runtime configuration, loaded from environment variables and an optional ``.env`` file.

Everything that differs between a laptop, CI, and production lives here, so the rest of the
code can take a :class:`Settings` object instead of reaching into ``os.environ``. Secrets are
typed as :class:`~pydantic.SecretStr` so they never show up in logs or reprs by accident, and
every secret is *optional*: importing the agent (for tests, evals, or ``--help``) must work
without keys. Missing credentials are reported only when something actually needs them: when
the worker starts (``main.preflight``) and again at session start.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["minimal", "low", "medium", "high"]
Verbosity = Literal["low", "medium", "high"]
RestaurantProviderName = Literal["mock", "opentable"]


class ConfigurationError(RuntimeError):
    """Raised when a feature is used without the configuration it needs (e.g. an API key)."""


class Settings(BaseSettings):
    """All agent settings. Field names map 1:1 to upper-case environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LiveKit -------------------------------------------------------------------------
    livekit_url: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: SecretStr | None = None

    # --- OpenAI / GPT-Live ---------------------------------------------------------------
    openai_api_key: SecretStr | None = None
    gpt_live_model: str = "gpt-live-1"
    gpt_live_voice: str = "marin"
    gpt_live_backend_model: str = "gpt-5.6-luna"
    gpt_live_backend_reasoning_effort: ReasoningEffort = "low"
    # Low verbosity keeps backend answers short, which is what the voice model needs to
    # paraphrase quickly; long backend answers turn into long monologues.
    gpt_live_backend_verbosity: Verbosity = "low"
    gpt_live_backend_max_output_tokens: int | None = Field(default=None, ge=16)

    # --- Prompts -------------------------------------------------------------------------
    agent_profile: str = "concierge"
    # IANA timezone used to compute the `today` / `timezone` prompt variables. Relative dates
    # ("tomorrow", "Friday") are resolved by the backend model against this.
    agent_timezone: str = "America/Los_Angeles"

    # --- Tools ---------------------------------------------------------------------------
    web_search_context_size: Literal["low", "medium", "high"] = "low"
    restaurant_provider: RestaurantProviderName = "mock"
    opentable_api_base_url: str = "https://platform.opentable.com"
    opentable_oauth_url: str = "https://oauth.opentable.com/api/v2/oauth/token"
    opentable_client_id: str | None = None
    opentable_client_secret: SecretStr | None = None
    opentable_timeout_seconds: float = Field(default=6.0, gt=0)

    # --- Call recording ------------------------------------------------------------------
    # At session end the transcript + metadata is sent to the Call Analyzer service, or written
    # to CALL_RECORDS_DIR when no analyzer is configured (or it can't be reached).
    call_recording_enabled: bool = True
    call_analyzer_url: str | None = None
    call_analyzer_token: SecretStr | None = None
    call_records_dir: Path = Path(".call-records")

    # --- Misc ----------------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("agent_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        # Fail at startup, not by quietly resolving "tomorrow" against UTC on a live call.
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"AGENT_TIMEZONE {value!r} is not a known IANA timezone") from exc
        return value

    @field_validator("call_analyzer_url", "call_analyzer_token", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `CALL_ANALYZER_URL=` in a .env file means "not configured", not "the empty URL".
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("call_analyzer_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"CALL_ANALYZER_URL {value!r} must start with http:// or https://")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    def require_openai_api_key(self) -> str:
        """Return the OpenAI key or fail with an actionable message."""
        if self.openai_api_key is None or not self.openai_api_key.get_secret_value():
            raise ConfigurationError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        return self.openai_api_key.get_secret_value()

    def call_analyzer_endpoint(self) -> tuple[str, str] | None:
        """``(base_url, token)`` for the Call Analyzer, or ``None`` if no URL is configured.

        A URL without a token is a misconfiguration rather than "recording off": every analyzer
        request would be rejected with 401 and each call would silently land on disk instead.
        """
        if self.call_analyzer_url is None:
            return None
        token = self.call_analyzer_token.get_secret_value() if self.call_analyzer_token else ""
        if not token:
            raise ConfigurationError(
                "CALL_ANALYZER_URL is set but CALL_ANALYZER_TOKEN is not. Set the token to the "
                "analyzer's shared secret, or unset CALL_ANALYZER_URL to write call records to "
                "CALL_RECORDS_DIR instead."
            )
        return self.call_analyzer_url, token


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached so `.env` is parsed once)."""
    return Settings()
