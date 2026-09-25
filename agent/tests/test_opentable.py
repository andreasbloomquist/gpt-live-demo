"""OpenTable provider against an in-memory fake of the (placeholder) partner API."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Callable, Coroutine

import httpx
import pytest

from voice_agent.tools.restaurants import (
    AvailabilityQuery,
    InvalidQueryError,
    OpenTableProvider,
    ProviderUnavailableError,
    ReservationError,
    RestaurantNotFoundError,
)

BASE = "https://partner.example.test"
OAUTH = "https://oauth.example.test/token"

QUERY = AvailabilityQuery(
    restaurant="Nopa",
    date=dt.date(2026, 10, 2),
    time=dt.time(19, 30),
    party_size=4,
    city="San Francisco",
)

# MockTransport accepts sync and async handlers.
Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)


class FakeOpenTable:
    """A tiny in-memory stand-in for the (placeholder) partner API."""

    def __init__(self) -> None:
        self.token_calls = 0
        self.requests: list[httpx.Request] = []
        self.search_response: httpx.Response | None = None
        self.availability_response: httpx.Response | None = None
        self.expire_first_token = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == OAUTH:
            self.token_calls += 1
            assert request.headers["authorization"].startswith("Basic ")
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(
                200, json={"access_token": f"tok{self.token_calls}", "expires_in": 3600}
            )
        if self.expire_first_token and request.headers["authorization"] == "Bearer tok1":
            return httpx.Response(401)
        if request.url.path == "/v1/restaurants/search":
            return self.search_response or httpx.Response(
                200, json={"restaurants": [{"id": "rid-1", "name": "Nopa", "city": "SF"}]}
            )
        if request.url.path == "/v1/restaurants/rid-1/availability":
            return self.availability_response or httpx.Response(
                200,
                json={
                    "times": [
                        {"date_time": "2026-10-02T19:00", "seating_area": "bar"},
                        {"date_time": "2026-10-02T19:30"},
                        {"date_time": "2026-10-03T19:30"},  # other day: ignored
                        {"bogus": True},  # malformed: ignored
                    ]
                },
            )
        return httpx.Response(500)


def _provider(api: Handler) -> OpenTableProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return OpenTableProvider(
        client_id="id",
        client_secret="secret",
        base_url=BASE,
        oauth_url=OAUTH,
        http_client=client,
    )


async def test_happy_path_and_token_caching() -> None:
    api = FakeOpenTable()
    provider = _provider(api)
    result = await provider.search_availability(QUERY)
    assert result.status == "available"
    assert [s.time for s in result.slots] == ["19:00", "19:30"]
    assert result.restaurant_id == "rid-1"
    assert result.provider == "opentable"

    avail = api.requests[-1]
    assert avail.headers["authorization"] == "Bearer tok1"
    assert avail.url.params["party_size"] == "4"
    assert avail.url.params["start_date_time"] == "2026-10-02T19:30"

    await provider.search_availability(QUERY)
    assert api.token_calls == 1  # cached


async def test_401_refreshes_token_once() -> None:
    api = FakeOpenTable()
    api.expire_first_token = True
    result = await _provider(api).search_availability(QUERY)
    assert result.status == "available"
    assert api.token_calls == 2


def _yielding(api: FakeOpenTable) -> Handler:
    """Wrap ``api`` so every request yields to the event loop, letting lookups interleave."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return api(request)

    return handler


async def test_concurrent_lookups_share_one_token_fetch() -> None:
    api = FakeOpenTable()
    provider = _provider(_yielding(api))
    await asyncio.gather(*(provider.search_availability(QUERY) for _ in range(3)))
    assert api.token_calls == 1


async def test_concurrent_401s_trigger_a_single_refresh() -> None:
    api = FakeOpenTable()
    api.expire_first_token = True
    provider = _provider(_yielding(api))
    results = await asyncio.gather(*(provider.search_availability(QUERY) for _ in range(3)))
    assert all(r.status == "available" for r in results)
    assert api.token_calls == 2  # tok1 (rejected everywhere) + one shared refresh


async def test_restaurant_id_is_quoted_into_the_path() -> None:
    api = FakeOpenTable()
    api.search_response = httpx.Response(200, json={"restaurants": [{"id": "../admin"}]})
    with pytest.raises(ProviderUnavailableError):  # the fake 500s on unknown paths
        await _provider(api).search_availability(QUERY)
    assert api.requests[-1].url.raw_path.startswith(b"/v1/restaurants/..%2Fadmin/availability")


async def test_malformed_search_response_is_not_found() -> None:
    api = FakeOpenTable()
    api.search_response = httpx.Response(200, json={"restaurants": {"id": "rid-1"}})
    with pytest.raises(RestaurantNotFoundError):
        await _provider(api).search_availability(QUERY)


async def test_no_matching_restaurant() -> None:
    api = FakeOpenTable()
    api.search_response = httpx.Response(200, json={"restaurants": []})
    with pytest.raises(RestaurantNotFoundError, match="Nopa in San Francisco"):
        await _provider(api).search_availability(QUERY)


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (404, RestaurantNotFoundError, "couldn't find"),
        (422, InvalidQueryError, "invalid"),
        (429, ProviderUnavailableError, "busy"),
        (503, ProviderUnavailableError, "isn't responding"),
    ],
)
async def test_http_errors_map_to_friendly_errors(
    status: int, error: type[ReservationError], message: str
) -> None:
    api = FakeOpenTable()
    api.availability_response = httpx.Response(status, text="upstream says no")
    with pytest.raises(error) as info:
        await _provider(api).search_availability(QUERY)
    assert message in info.value.user_message
    assert "upstream says no" not in info.value.user_message


async def test_oauth_failure_is_unavailable() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    with pytest.raises(ProviderUnavailableError):
        await _provider(api).search_availability(QUERY)


async def test_timeout_is_mapped() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if str(request.url) == OAUTH:
            return httpx.Response(200, content=json.dumps({"access_token": "t"}))
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderUnavailableError) as info:
        await _provider(api).search_availability(QUERY)
    assert "too long" in info.value.user_message


def test_requires_credentials() -> None:
    with pytest.raises(ValueError):
        OpenTableProvider(client_id="", client_secret="x")
