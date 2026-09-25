"""OpenTable partner-API provider (illustrative).

.. important::
   OpenTable's APIs are **partner-gated**: you need an approved partner agreement to get
   credentials and the real API reference. The endpoint paths, query parameters, and response
   fields below are **illustrative placeholders** modelled on a typical OAuth2 + REST partner
   API. Adapt ``_find_restaurant`` / ``_fetch_availability`` / ``_parse_*`` to the actual
   contract you receive; the rest of the agent only depends on the provider-neutral
   :class:`~voice_agent.tools.restaurants.base.ReservationProvider` interface.

What *is* production-shaped here and worth keeping:

* OAuth2 **client-credentials** flow with an in-memory token cache that refreshes shortly before
  expiry and once on a 401 (tokens can be revoked early).
* Tight **timeouts**: on a live call, a slow answer is a bad answer. The voice model keeps the
  caller company with a filler phrase, but only for a few seconds.
* Every HTTP/network failure is mapped to a :class:`ReservationError` whose message is written
  for the caller, so the LLM can say something useful instead of reading out a stack trace.
* An injectable ``httpx.AsyncClient`` so tests run against ``httpx.MockTransport`` with no
  network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .base import (
    InvalidQueryError,
    ProviderUnavailableError,
    RestaurantNotFoundError,
)
from .models import AvailabilityQuery, RestaurantAvailability, TimeSlot

logger = logging.getLogger(__name__)

# Placeholder paths -- replace with the ones from your OpenTable partner documentation.
RESTAURANT_SEARCH_PATH = "/v1/restaurants/search"
AVAILABILITY_PATH = "/v1/restaurants/{rid}/availability"

_TOKEN_REFRESH_MARGIN_S = 60.0


class OpenTableProvider:
    """Availability lookups against the OpenTable partner API. Never books."""

    name = "opentable"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: str = "https://platform.opentable.com",
        oauth_url: str = "https://oauth.opentable.com/api/v2/oauth/token",
        timeout_s: float = 6.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("OpenTable client_id and client_secret are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._oauth_url = oauth_url
        self._timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0))
        self._http_client = http_client
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------ public
    async def search_availability(self, query: AvailabilityQuery) -> RestaurantAvailability:
        async with self._client() as client:
            restaurant = await self._find_restaurant(client, query)
            payload = await self._fetch_availability(client, restaurant["id"], query)
        return self._parse_availability(query, restaurant, payload)

    # ------------------------------------------------------------------ HTTP plumbing
    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        # Reuse an injected client (tests, connection pooling); otherwise open a short-lived one.
        if self._http_client is not None:
            yield self._http_client
            return
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            yield client

    async def _access_token(self, client: httpx.AsyncClient, *, force: bool = False) -> str:
        async with self._token_lock:
            now = time.monotonic()
            if not force and self._token and now < self._token_expires_at:
                return self._token
            try:
                resp = await client.post(
                    self._oauth_url,
                    data={"grant_type": "client_credentials"},
                    auth=(self._client_id, self._client_secret),
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                raise ProviderUnavailableError(detail=f"oauth timeout: {exc}") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(detail=f"oauth transport error: {exc}") from exc
            if resp.status_code != 200:
                # Bad credentials are an operator problem; the caller just hears "unavailable".
                raise ProviderUnavailableError(
                    detail=f"oauth failed with HTTP {resp.status_code}: {resp.text[:200]}"
                )
            try:
                body = resp.json()
                token = str(body["access_token"])
                expires_in = float(body.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError) as exc:
                raise ProviderUnavailableError(detail=f"malformed oauth response: {exc}") from exc
            self._token = token
            self._token_expires_at = now + max(expires_in - _TOKEN_REFRESH_MARGIN_S, 0.0)
            return token

    async def _get(
        self, client: httpx.AsyncClient, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        for attempt in range(2):
            token = await self._access_token(client, force=attempt > 0)
            try:
                resp = await client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                raise ProviderUnavailableError(
                    "The reservation system is taking too long to respond. Suggest trying again "
                    "in a moment or calling the restaurant.",
                    detail=f"GET {path} timed out: {exc}",
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(detail=f"GET {path} failed: {exc}") from exc
            if resp.status_code == 401 and attempt == 0:
                continue  # token revoked/expired early: refresh once and retry
            return self._check_response(resp, path)
        raise ProviderUnavailableError(detail=f"GET {path}: unauthorized after token refresh")

    @staticmethod
    def _check_response(resp: httpx.Response, path: str) -> dict[str, Any]:
        status = resp.status_code
        detail = f"GET {path} -> HTTP {status}: {resp.text[:200]}"
        if status == 404:
            raise RestaurantNotFoundError(detail=detail)
        if status in (400, 422):
            raise InvalidQueryError(detail=detail)
        if status == 429:
            raise ProviderUnavailableError(
                "The reservation system is busy right now. Suggest trying again in a minute.",
                detail=detail,
            )
        if status >= 400:
            raise ProviderUnavailableError(detail=detail)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderUnavailableError(detail=f"{detail} (invalid JSON)") from exc
        if not isinstance(data, dict):
            raise ProviderUnavailableError(detail=f"{detail} (unexpected JSON shape)")
        return data

    # ------------------------------------------------------------------ API calls (placeholders)
    async def _find_restaurant(
        self, client: httpx.AsyncClient, query: AvailabilityQuery
    ) -> dict[str, Any]:
        """Resolve a spoken restaurant name to a restaurant id. PLACEHOLDER endpoint/fields."""
        params: dict[str, Any] = {"name": query.restaurant, "limit": 1}
        if query.city:
            params["city"] = query.city
        data = await self._get(client, RESTAURANT_SEARCH_PATH, params)
        items = data.get("restaurants") or []
        if not items or not isinstance(items[0], dict) or "id" not in items[0]:
            raise RestaurantNotFoundError(
                f"I couldn't find a restaurant called {query.restaurant}"
                + (f" in {query.city}" if query.city else "")
                + ". Ask the caller to confirm the name or the city."
            )
        return items[0]

    async def _fetch_availability(
        self, client: httpx.AsyncClient, rid: str, query: AvailabilityQuery
    ) -> dict[str, Any]:
        """Fetch slots around the requested time. PLACEHOLDER endpoint/fields."""
        start = dt.datetime.combine(query.date, query.time)
        params = {
            "start_date_time": start.strftime("%Y-%m-%dT%H:%M"),
            "party_size": query.party_size,
            "forward_minutes": 90,
            "backward_minutes": 90,
        }
        return await self._get(client, AVAILABILITY_PATH.format(rid=rid), params)

    def _parse_availability(
        self, query: AvailabilityQuery, restaurant: dict[str, Any], payload: dict[str, Any]
    ) -> RestaurantAvailability:
        """Map the (placeholder) response shape to the provider-neutral model."""
        requested = query.time.strftime("%H:%M")
        slots: list[TimeSlot] = []
        for raw in payload.get("times") or []:
            try:
                when = dt.datetime.fromisoformat(str(raw["date_time"]))
            except (KeyError, TypeError, ValueError):
                logger.debug("skipping malformed slot: %r", raw)
                continue
            if when.date() != query.date:
                continue
            slots.append(TimeSlot(time=when.strftime("%H:%M"), area=raw.get("seating_area")))

        if payload.get("closed"):
            status = "closed"
        elif any(s.time == requested for s in slots):
            status = "available"
        elif slots:
            status = "alternatives"
        else:
            status = "unavailable"
        return RestaurantAvailability(
            restaurant=str(restaurant.get("name") or query.restaurant),
            restaurant_id=str(restaurant["id"]),
            city=restaurant.get("city") or query.city,
            date=query.date,
            requested_time=requested,
            party_size=query.party_size,
            status=status,
            slots=slots,
            provider=self.name,
        )


__all__ = ["OpenTableProvider"]
