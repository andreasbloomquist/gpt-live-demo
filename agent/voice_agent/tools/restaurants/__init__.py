"""Restaurant availability: provider-neutral models, pluggable providers, and the LLM tool."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import TYPE_CHECKING

from .base import (
    InvalidQueryError,
    ProviderUnavailableError,
    ReservationError,
    ReservationProvider,
    RestaurantNotFoundError,
)
from .mock import MockReservationProvider
from .models import AvailabilityQuery, RestaurantAvailability, TimeSlot
from .opentable import OpenTableProvider
from .tool import TOOL_NAME, build_restaurant_availability_tool, check_availability

if TYPE_CHECKING:
    from ...config import Settings


def build_reservation_provider(settings: Settings) -> ReservationProvider:
    """Pick the provider named by ``RESTAURANT_PROVIDER`` (``mock`` or ``opentable``).

    Called once per session, and deliberately not cached per process: the provider's OAuth
    token lock is an :class:`asyncio.Lock`, which binds to the event loop that first waits on
    it. LiveKit's thread executor (used by ``console`` and on Windows) runs each job on its own
    loop, so a shared provider would fail later sessions with "bound to a different event loop".
    The default process executor runs one job per process, so a cache would never be hit there.
    """
    from ...config import ConfigurationError

    if settings.restaurant_provider == "mock":
        return MockReservationProvider()
    if settings.restaurant_provider == "opentable":
        secret = settings.opentable_client_secret
        if not settings.opentable_client_id or secret is None or not secret.get_secret_value():
            raise ConfigurationError(
                "RESTAURANT_PROVIDER=opentable needs OPENTABLE_CLIENT_ID and "
                "OPENTABLE_CLIENT_SECRET (OpenTable partner credentials)."
            )
        return OpenTableProvider(
            client_id=settings.opentable_client_id,
            client_secret=secret.get_secret_value(),
            base_url=settings.opentable_api_base_url,
            oauth_url=settings.opentable_oauth_url,
            timeout_s=settings.opentable_timeout_seconds,
        )
    raise ConfigurationError(f"unknown RESTAURANT_PROVIDER {settings.restaurant_provider!r}")


def local_today(timezone: str) -> Callable[[], dt.date]:
    """Clock returning today's date in ``timezone`` (falls back to UTC if unknown)."""
    from ...runtime import zone

    tz = zone(timezone)
    return lambda: dt.datetime.now(tz).date()


__all__ = [
    "TOOL_NAME",
    "AvailabilityQuery",
    "InvalidQueryError",
    "MockReservationProvider",
    "OpenTableProvider",
    "ProviderUnavailableError",
    "ReservationError",
    "ReservationProvider",
    "RestaurantAvailability",
    "RestaurantNotFoundError",
    "TimeSlot",
    "build_reservation_provider",
    "build_restaurant_availability_tool",
    "check_availability",
    "local_today",
]
