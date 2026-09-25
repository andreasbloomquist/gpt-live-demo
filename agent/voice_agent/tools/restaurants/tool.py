"""The ``check_restaurant_availability`` function tool.

Under GPT-Live's ``delegation="responses"`` mode this tool is exposed to the *backend*
Responses model (not the voice model). The backend resolves "this Friday at seven" into ISO
arguments, calls the tool, and hands a short summary back to the voice model to speak.

Design choices:

* **Availability only.** There is deliberately no booking tool: a voice agent that can commit
  a reservation needs explicit confirmation flows, idempotency, and cancellation handling. The
  output even says ``"booking": "not booked - availability check only"`` so the model is
  reminded at the moment it reads the result.
* **Validate at the boundary.** Arguments are re-validated here (ISO date, 24h time, not in the
  past, sane party size) and violations raise :class:`~livekit.agents.ToolError` with a message
  aimed at the model, so it can self-correct (e.g. fix the date) instead of failing silently.
* **Provider injected via a factory.** The tool is built per session from a
  :class:`ReservationProvider`, which keeps it testable with the deterministic mock.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Callable

from livekit.agents import FunctionTool, ToolError, function_tool
from pydantic import ValidationError

from .base import ReservationError, ReservationProvider
from .models import AvailabilityQuery

logger = logging.getLogger(__name__)

TOOL_NAME = "check_restaurant_availability"
MAX_DAYS_AHEAD = 180
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")

Clock = Callable[[], dt.date]


def _parse_date(value: str, today: dt.date) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(
            f"Invalid date {value!r}. Pass an ISO date like {today.isoformat()}; resolve words "
            f"like 'tomorrow' or 'Friday' relative to today ({today.isoformat()})."
        ) from None
    if parsed < today:
        raise ToolError(
            f"{parsed.isoformat()} is in the past (today is {today.isoformat()}). "
            "Confirm the day with the caller."
        )
    if (parsed - today).days > MAX_DAYS_AHEAD:
        raise ToolError(
            f"Reservations can only be checked up to {MAX_DAYS_AHEAD} days ahead. "
            "Ask the caller for an earlier date."
        )
    return parsed


def _parse_time(value: str) -> dt.time:
    match = _TIME_RE.match(value)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ToolError(f"Invalid time {value!r}. Use 24-hour HH:MM, for example 19:30.")
    return dt.time(int(match.group(1)), int(match.group(2)))


async def check_availability(
    provider: ReservationProvider,
    *,
    restaurant: str,
    date: str,
    time: str,
    party_size: int,
    city: str | None = None,
    today: dt.date,
) -> str:
    """Validate arguments, query ``provider``, and return a compact JSON string for the LLM.

    Separated from the decorated tool so it can be unit-tested and reused by evals without a
    LiveKit session. Raises :class:`ToolError` for anything the model should hear about.
    """
    if not restaurant or not restaurant.strip():
        raise ToolError("Ask the caller which restaurant they'd like.")
    if party_size < 1:
        raise ToolError("Party size must be at least 1. Ask how many people are coming.")
    if party_size > 20:
        raise ToolError(
            "Parties larger than 20 can't be checked online. Suggest contacting the "
            "restaurant's events team."
        )
    try:
        query = AvailabilityQuery(
            restaurant=restaurant,
            date=_parse_date(date, today),
            time=_parse_time(time),
            party_size=party_size,
            city=city or None,
        )
    except ValidationError as exc:
        error = exc.errors()[0]
        field = ".".join(str(part) for part in error["loc"]) or "arguments"
        raise ToolError(f"Invalid {field}: {error['msg']}") from None

    try:
        result = await provider.search_availability(query)
    except ReservationError as exc:
        logger.warning(
            "reservation provider error",
            extra={"provider": provider.name, "detail": exc.detail or str(exc)},
        )
        raise ToolError(exc.user_message) from exc
    except Exception as exc:  # never leak internals (or a traceback) into the conversation
        logger.exception("unexpected reservation provider failure")
        raise ToolError(
            "The availability check failed unexpectedly. Suggest trying again or calling the "
            "restaurant directly."
        ) from exc

    return json.dumps(result.to_tool_output(), separators=(",", ":"))


def build_restaurant_availability_tool(
    provider: ReservationProvider, *, clock: Clock | None = None
) -> FunctionTool:
    """Create the ``check_restaurant_availability`` tool bound to ``provider``.

    ``clock`` returns "today" in the agent's timezone; it's injectable so tests and evals are
    date-independent.
    """
    get_today: Clock = clock or dt.date.today

    @function_tool(name=TOOL_NAME)
    async def check_restaurant_availability(
        restaurant: str,
        date: str,
        time: str,
        party_size: int,
        city: str | None = None,
    ) -> str:
        """Check whether a restaurant has a table for a party at a given date and time.

        This only checks availability. It does not book, hold, or confirm a reservation.
        Returns the status, whether the requested time is available, and up to three of the
        nearest available times.

        Args:
            restaurant: Restaurant name as the caller said it, e.g. "Nopa".
            date: Reservation date in ISO format YYYY-MM-DD. Resolve relative dates such as
                "tomorrow" or "this Friday" against today's date first.
            time: Desired time in 24-hour HH:MM format, e.g. "19:30".
            party_size: Number of people, including the caller.
            city: City the restaurant is in, if the caller mentioned one.
        """
        return await check_availability(
            provider,
            restaurant=restaurant,
            date=date,
            time=time,
            party_size=party_size,
            city=city,
            today=get_today(),
        )

    return check_restaurant_availability
