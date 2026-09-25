"""Runtime configuration, loaded from environment variables and an optional ``.env`` file.

Everything that differs between a laptop, CI, and production lives here, so the rest of the
code can take a :class:`Settings` object instead of reaching into ``os.environ``. Secrets are
typed as :class:`~pydantic.SecretStr` so they never show up in logs or reprs by accident, and
every secret is *optional*: importing the agent (for tests, evals, or ``--help``) must work
without keys. Missing credentials are reported only when something actually needs them, at
session start.
"""

from __future__ import annotations

from functools import lru_cache
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached so `.env` is parsed once)."""
    return Settings()
