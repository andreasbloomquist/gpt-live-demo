"""Provider-neutral data models for restaurant availability.

Providers (mock, OpenTable, or your own) translate their APIs into these models, so the tool,
prompts, and evals never depend on a vendor's schema.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AvailabilityStatus = Literal["available", "alternatives", "unavailable", "closed"]


class AvailabilityQuery(BaseModel):
    """A validated availability request. Dates/times are already absolute (resolved by the LLM)."""

    model_config = ConfigDict(frozen=True)

    restaurant: str = Field(min_length=1, max_length=120)
    date: dt.date
    time: dt.time
    party_size: int = Field(ge=1, le=20)
    city: str | None = Field(default=None, max_length=80)

    @field_validator("restaurant", "city")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        return value or None


class TimeSlot(BaseModel):
    """One bookable time. ``time`` is 24h ``HH:MM`` local to the restaurant."""

    model_config = ConfigDict(frozen=True)

    time: str
    area: str | None = None


class RestaurantAvailability(BaseModel):
    """Availability for one restaurant/date/party, as returned by a provider."""

    restaurant: str
    restaurant_id: str | None = None
    city: str | None = None
    date: dt.date
    requested_time: str
    party_size: int
    status: AvailabilityStatus
    slots: list[TimeSlot] = Field(default_factory=list)
    provider: str
    note: str | None = None

    def to_tool_output(self, max_slots: int = 3) -> dict[str, Any]:
        """A compact payload for the LLM: small outputs keep the backend fast and on-point.

        Only the few slots closest to the requested time are included; the backend model is
        instructed to offer at most three anyway.
        """
        requested = _minutes(self.requested_time)
        nearest = sorted(self.slots, key=lambda s: (abs(_minutes(s.time) - requested), s.time))
        out: dict[str, Any] = {
            "restaurant": self.restaurant,
            "date": self.date.isoformat(),
            "weekday": self.date.strftime("%A"),
            "requested_time": self.requested_time,
            "party_size": self.party_size,
            "status": self.status,
            "requested_time_available": self.status == "available",
            "nearest_available_times": [s.time for s in nearest[:max_slots]],
            "booking": "not booked - availability check only",
        }
        if self.city:
            out["city"] = self.city
        if self.note:
            out["note"] = self.note
        return out


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)
