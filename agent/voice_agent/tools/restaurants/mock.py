"""A deterministic, offline reservation provider.

It's the default so the demo works with zero partner credentials, and it's what evals use: the
same restaurant + date always yields the same slots (seeded from a SHA-256 hash, not Python's
randomized ``hash()``), so an eval that passes today passes tomorrow for the same reasons.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random

from .base import InvalidQueryError
from .models import AvailabilityQuery, RestaurantAvailability, TimeSlot

# Dinner service, in 15-minute steps; last seating 21:45.
_SERVICE_START = dt.time(17, 0)
_SERVICE_END = dt.time(21, 45)
_STEP_MINUTES = 15
_WINDOW_MINUTES = 90
_LARGE_PARTY = 9
_AREAS = ("dining room", "bar", "patio")


def _slug(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


class MockReservationProvider:
    """Fake but plausible availability. Rules (all deterministic per restaurant + date):

    * Service is 17:00-21:45. Requests outside it get the nearest in-service alternatives.
    * Roughly one in seven restaurant/date combinations is "closed" (e.g. a private event).
    * Parties of 9+ are never bookable online (a common real-world rule), so the note tells the
      caller to phone the restaurant.
    * Otherwise about half of the slots within ±90 minutes of the request are open.
    """

    name = "mock"

    def __init__(self, *, seed_salt: str = "v1") -> None:
        # Bumping the salt reshuffles all fake data, e.g. if eval fixtures need regenerating.
        self._salt = seed_salt

    def _rng(self, query: AvailabilityQuery) -> random.Random:
        key = f"{self._salt}|{_slug(query.restaurant)}|{query.date.isoformat()}"
        seed = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
        return random.Random(seed)

    async def search_availability(self, query: AvailabilityQuery) -> RestaurantAvailability:
        if query.restaurant.strip().lower() in {"", "unknown"}:
            raise InvalidQueryError("I need the restaurant's name to check availability.")

        rng = self._rng(query)
        requested = query.time.strftime("%H:%M")
        base = RestaurantAvailability(
            restaurant=query.restaurant.title() if query.restaurant.islower() else query.restaurant,
            restaurant_id=f"mock-{_slug(query.restaurant)}",
            city=query.city,
            date=query.date,
            requested_time=requested,
            party_size=query.party_size,
            status="unavailable",
            provider=self.name,
        )

        if rng.random() < 1 / 7:
            return base.model_copy(
                update={"status": "closed", "note": "The restaurant is closed on this date."}
            )
        if query.party_size >= _LARGE_PARTY:
            return base.model_copy(
                update={
                    "note": "Parties of 9 or more must be arranged directly with the restaurant."
                }
            )

        # Draw availability for every service slot so results for different requested times on
        # the same day are mutually consistent (the 19:00 slot is open or not, regardless of
        # whether the caller asked for 18:30 or 19:15).
        open_slots: list[TimeSlot] = []
        minute = _to_minutes(_SERVICE_START)
        while minute <= _to_minutes(_SERVICE_END):
            is_open = rng.random() < 0.5
            area = rng.choice(_AREAS)
            if is_open:
                open_slots.append(TimeSlot(time=_fmt(minute), area=area))
            minute += _STEP_MINUTES

        # Big parties get fewer options: drop every other slot for 5-8 people.
        if query.party_size >= 5:
            open_slots = open_slots[::2]

        target = _to_minutes(query.time)
        clamped = min(max(target, _to_minutes(_SERVICE_START)), _to_minutes(_SERVICE_END))
        nearby = [
            s for s in open_slots if abs(_hhmm_to_minutes(s.time) - clamped) <= _WINDOW_MINUTES
        ]

        if any(s.time == requested for s in nearby):
            status = "available"
        elif nearby:
            status = "alternatives"
        else:
            status = "unavailable"
        note = None
        if target != clamped:
            note = "Requested time is outside dinner service (5:00 pm to 9:45 pm)."
        return base.model_copy(update={"status": status, "slots": nearby, "note": note})


def _to_minutes(value: dt.time) -> int:
    return value.hour * 60 + value.minute


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)
