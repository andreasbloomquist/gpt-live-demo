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


_OPENTABLE_PROVIDERS: dict[tuple[str, str, str, str, float], OpenTableProvider] = {}


def build_reservation_provider(settings: Settings) -> ReservationProvider:
    """Pick the provider named by ``RESTAURANT_PROVIDER`` (``mock`` or ``opentable``)."""
    from ...config import ConfigurationError

    if settings.restaurant_provider == "mock":
        return MockReservationProvider()
    if settings.restaurant_provider == "opentable":
        secret = settings.opentable_client_secret
        if not settings.opentable_client_id or secret is None:
            raise ConfigurationError(
                "RESTAURANT_PROVIDER=opentable needs OPENTABLE_CLIENT_ID and "
                "OPENTABLE_CLIENT_SECRET (OpenTable partner credentials)."
            )
        # One provider per credential set per process, so the OAuth token cache survives
        # across sessions instead of paying a token round-trip on every call's first lookup.
        key = (
            settings.opentable_client_id,
            secret.get_secret_value(),
            settings.opentable_api_base_url,
            settings.opentable_oauth_url,
            settings.opentable_timeout_seconds,
        )
        if key not in _OPENTABLE_PROVIDERS:
            _OPENTABLE_PROVIDERS[key] = OpenTableProvider(
                client_id=key[0],
                client_secret=key[1],
                base_url=key[2],
                oauth_url=key[3],
                timeout_s=key[4],
            )
        return _OPENTABLE_PROVIDERS[key]
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
