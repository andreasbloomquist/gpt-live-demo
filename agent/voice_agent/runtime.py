"""Per-session runtime values that are injected into prompts (``today``, ``timezone``).

GPT-Live voice instructions can't change after the session starts, and the backend model has
no clock of its own, so the current date must be baked into both prompts at session start.
Without it, "book something for Friday" is ambiguous and models will happily guess a year.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings

logger = logging.getLogger(__name__)


def zone(name: str) -> dt.tzinfo:
    """Resolve an IANA timezone name, falling back to UTC (with a warning) if unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown timezone %r, falling back to UTC", name)
        return dt.timezone.utc


def runtime_prompt_variables(
    settings: Settings, *, now: dt.datetime | None = None
) -> dict[str, str]:
    """Values for the manifest's ``runtime_variables``, computed in the agent's timezone.

    ``today`` includes the weekday ("Friday, 2026-09-25") because resolving "this Saturday"
    is much more reliable when the model doesn't have to compute the weekday itself.
    """
    tz = zone(settings.agent_timezone)
    local = (now or dt.datetime.now(tz)).astimezone(tz)
    return {
        "today": f"{local:%A}, {local.date().isoformat()}",
        "timezone": settings.agent_timezone,
    }
