from __future__ import annotations

import datetime as dt
import json

import pytest
from livekit.agents import ToolError

from voice_agent.tools.restaurants import (
    AvailabilityQuery,
    MockReservationProvider,
    ProviderUnavailableError,
    RestaurantAvailability,
    RestaurantNotFoundError,
    build_restaurant_availability_tool,
    check_availability,
)

TODAY = dt.date(2026, 9, 25)


def _query(**kw: object) -> AvailabilityQuery:
    base: dict[str, object] = {
        "restaurant": "Nopa",
        "date": dt.date(2026, 10, 2),
        "time": dt.time(19, 30),
        "party_size": 4,
    }
    base.update(kw)
    return AvailabilityQuery.model_validate(base)


# --------------------------------------------------------------------------- mock provider
async def test_mock_is_deterministic() -> None:
    a = await MockReservationProvider().search_availability(_query())
    b = await MockReservationProvider().search_availability(_query())
    assert a == b
    # Case/whitespace in the name doesn't change the seed.
    c = await MockReservationProvider().search_availability(_query(restaurant="  nopa "))
    assert [s.time for s in c.slots] == [s.time for s in a.slots]


async def test_mock_varies_across_restaurants_and_dates() -> None:
    provider = MockReservationProvider()
    results = {
        tuple(
            s.time for s in (await provider.search_availability(_query(restaurant=r, date=d))).slots
        )
        for r in ("Nopa", "Zuni Cafe", "Tartine", "Delfina")
        for d in (dt.date(2026, 10, 2), dt.date(2026, 10, 3))
    }
    assert len(results) > 3


async def test_mock_slots_are_consistent_within_a_day() -> None:
    # Asking for 18:30 vs 19:00 must agree on every slot both windows cover (17:30-20:00).
    provider = MockReservationProvider()
    for name in ("Nopa", "Zuni Cafe", "Tartine"):
        early = await provider.search_availability(_query(restaurant=name, time=dt.time(18, 30)))
        late = await provider.search_availability(_query(restaurant=name, time=dt.time(19, 0)))

        def shared(times: set[str]) -> set[str]:
            return {t for t in times if "17:30" <= t <= "20:00"}

        assert shared({s.time for s in early.slots}) == shared({s.time for s in late.slots})


async def test_mock_status_matches_slots() -> None:
    provider = MockReservationProvider()
    for name in ("Nopa", "Zuni Cafe", "Tartine", "Delfina", "Flour + Water", "Kin Khao"):
        res = await provider.search_availability(_query(restaurant=name))
        times = [s.time for s in res.slots]
        if res.status == "available":
            assert "19:30" in times
        elif res.status == "alternatives":
            assert times and "19:30" not in times
        else:
            assert not times


async def test_mock_large_party_needs_direct_contact() -> None:
    provider = MockReservationProvider()
    for name in ("Nopa", "Zuni Cafe", "Tartine"):
        res = await provider.search_availability(_query(restaurant=name, party_size=10))
        assert res.status in {"unavailable", "closed"}
        assert res.note


def test_tool_output_is_compact_and_says_not_booked() -> None:
    result = RestaurantAvailability(
        restaurant="Nopa",
        date=dt.date(2026, 10, 2),
        requested_time="19:30",
        party_size=2,
        status="alternatives",
        slots=[{"time": t} for t in ("17:00", "19:00", "19:45", "20:00", "21:30")],  # type: ignore[misc]
        provider="test",
    )
    out = result.to_tool_output()
    assert out["nearest_available_times"] == ["19:45", "19:00", "20:00"]
    assert out["weekday"] == "Friday"
    assert "not booked" in out["booking"]


def test_tool_output_lists_each_time_once() -> None:
    result = RestaurantAvailability(
        restaurant="Nopa",
        date=dt.date(2026, 10, 2),
        requested_time="19:30",
        party_size=2,
        status="available",
        slots=[{"time": "19:30", "area": a} for a in ("bar", "patio")] + [{"time": "20:00"}],  # type: ignore[misc]
        provider="test",
    )
    assert result.to_tool_output()["nearest_available_times"] == ["19:30", "20:00"]


# --------------------------------------------------------------------------- tool function
class _StubProvider:
    name = "stub"

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.queries: list[AvailabilityQuery] = []

    async def search_availability(self, query: AvailabilityQuery) -> RestaurantAvailability:
        self.queries.append(query)
        if self.exc:
            raise self.exc
        return await MockReservationProvider().search_availability(query)


def _tool(provider: object) -> object:
    return build_restaurant_availability_tool(provider, clock=lambda: TODAY)  # type: ignore[arg-type]


async def test_decorated_tool_is_directly_callable() -> None:
    tool = _tool(MockReservationProvider())
    raw = await tool(restaurant="Nopa", date="2026-10-02", time="19:30", party_size=4)  # type: ignore[operator]
    payload = json.loads(raw)
    assert payload["restaurant"] == "Nopa"
    assert payload["date"] == "2026-10-02"
    assert payload["party_size"] == 4
    assert payload["status"] in {"available", "alternatives", "unavailable", "closed"}
    # Deterministic for evals.
    assert raw == await tool(restaurant="Nopa", date="2026-10-02", time="19:30", party_size=4)  # type: ignore[operator]


async def test_tool_passes_city_and_parses_arguments() -> None:
    stub = _StubProvider()
    await _tool(stub)(  # type: ignore[operator]
        restaurant="Nopa", date="2026-10-02", time="7:05", party_size=2, city="San Francisco"
    )
    (query,) = stub.queries
    assert query.time == dt.time(7, 5)
    assert query.city == "San Francisco"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"date": "tomorrow"}, "ISO date"),
        ({"date": "2026-09-24"}, "in the past"),
        ({"date": "2027-09-24"}, "days ahead"),
        ({"time": "7pm"}, "HH:MM"),
        ({"time": "25:00"}, "HH:MM"),
        ({"party_size": 0}, "at least 1"),
        ({"party_size": 40}, "larger than 20"),
        ({"restaurant": "  "}, "which restaurant"),
        ({"restaurant": "x" * 121}, "Invalid restaurant"),
    ],
)
async def test_tool_validation_errors(kwargs: dict[str, object], match: str) -> None:
    args: dict[str, object] = {
        "restaurant": "Nopa",
        "date": "2026-10-02",
        "time": "19:30",
        "party_size": 2,
    }
    args.update(kwargs)
    stub = _StubProvider()
    with pytest.raises(ToolError, match=match):
        await _tool(stub)(**args)  # type: ignore[operator]
    assert stub.queries == []  # validation happens before the provider is called


async def test_today_is_allowed() -> None:
    stub = _StubProvider()
    await check_availability(
        stub, restaurant="Nopa", date="2026-09-25", time="19:00", party_size=2, today=TODAY
    )
    assert stub.queries


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (RestaurantNotFoundError("No such place."), "No such place."),
        (ProviderUnavailableError(), "isn't responding"),
        (RuntimeError("secret internal detail"), "failed unexpectedly"),
    ],
)
async def test_provider_errors_become_friendly_tool_errors(exc: Exception, message: str) -> None:
    with pytest.raises(ToolError, match=message) as info:
        await _tool(_StubProvider(exc))(  # type: ignore[operator]
            restaurant="Nopa", date="2026-10-02", time="19:30", party_size=2
        )
    assert "secret internal detail" not in info.value.message
