"""The provider interface every reservation backend implements, plus its error types.

Errors carry a ``user_message`` written for the *caller* (via the LLM), so providers decide how
to phrase failures once and the tool layer just forwards them. Keeping this module free of
LiveKit imports lets providers be unit-tested and reused (e.g. by evals) in isolation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import AvailabilityQuery, RestaurantAvailability


class ReservationError(Exception):
    """Base class for provider failures; ``user_message`` is safe to show to the model."""

    default_message = "The reservation system couldn't complete that check right now."

    def __init__(self, user_message: str | None = None, *, detail: str | None = None) -> None:
        self.user_message = user_message or self.default_message
        self.detail = detail
        super().__init__(detail or self.user_message)


class RestaurantNotFoundError(ReservationError):
    default_message = (
        "I couldn't find that restaurant. Ask the caller to confirm the name or the city."
    )


class InvalidQueryError(ReservationError):
    default_message = "The request details look invalid. Check the date, time, and party size."


class ProviderUnavailableError(ReservationError):
    default_message = (
        "The reservation system isn't responding right now. Suggest trying again shortly or "
        "calling the restaurant directly."
    )


@runtime_checkable
class ReservationProvider(Protocol):
    """Anything that can answer "is there a table?". It must never create a booking."""

    name: str

    async def search_availability(self, query: AvailabilityQuery) -> RestaurantAvailability:
        """Return availability for ``query`` or raise a :class:`ReservationError`."""
        ...
