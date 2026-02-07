"""Gamma API client for Polymarket market discovery."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config.settings import settings
from data.models import Event, Market, Outcome

logger = logging.getLogger(__name__)


class GammaClient:
    """Client for the Polymarket Gamma API to discover NegRisk events."""

    def __init__(self):
        self.base_url = settings.gamma_host
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def fetch_negrisk_events(self) -> list[Event]:
        """Fetch all active NegRisk events from the Gamma API."""
        events: list[Event] = []
        offset = 0
        limit = 100

        while True:
            try:
                raw_events = await self._get(
                    "/events",
                    params={
                        "closed": "false",
                        "active": "true",
                        "neg_risk": "true",
                        "limit": str(limit),
                        "offset": str(offset),
                    },
                )
            except httpx.HTTPError as e:
                logger.error(f"Failed to fetch events from Gamma API: {e}")
                break

            if not raw_events:
                break

            for raw_event in raw_events:
                event = self._parse_event(raw_event)
                if event and event.outcomes:
                    events.append(event)

            if len(raw_events) < limit:
                break
            offset += limit

        logger.info(f"Discovered {len(events)} active NegRisk events")
        return events

    def _parse_event(self, raw: dict) -> Event | None:
        """Parse a raw Gamma API event response into our Event model."""
        try:
            event_id = raw.get("id", "")
            if not event_id:
                return None

            markets: list[Market] = []
            outcomes: list[Outcome] = []
            raw_markets = raw.get("markets", [])

            for m in raw_markets:
                if m.get("closed", False):
                    continue
                if not m.get("active", True):
                    continue

                raw_clob_ids = m.get("clobTokenIds", [])
                # clobTokenIds may be a JSON string or already a list
                if isinstance(raw_clob_ids, str):
                    try:
                        import json
                        clob_token_ids = json.loads(raw_clob_ids)
                    except (json.JSONDecodeError, TypeError):
                        continue
                else:
                    clob_token_ids = raw_clob_ids
                if not clob_token_ids or len(clob_token_ids) < 2:
                    continue

                token_id_yes = clob_token_ids[0]
                token_id_no = clob_token_ids[1] if len(clob_token_ids) > 1 else ""
                condition_id = m.get("conditionId", m.get("condition_id", ""))

                # Parse tick size
                tick_str = m.get("minimum_tick_size", m.get("minimumTickSize", "0.01"))
                try:
                    tick_size = float(tick_str)
                except (ValueError, TypeError):
                    tick_size = 0.01

                market = Market(
                    condition_id=condition_id,
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    token_id_yes=token_id_yes,
                    token_id_no=token_id_no,
                    active=m.get("active", True),
                    closed=m.get("closed", False),
                    accepting_orders=m.get("acceptingOrders", True),
                    min_tick_size=tick_size,
                    open_interest=float(m.get("openInterest", 0) or 0),
                )
                markets.append(market)

                outcome_name = m.get("groupItemTitle", m.get("question", ""))
                outcomes.append(
                    Outcome(
                        token_id=token_id_yes,
                        no_token_id=token_id_no,
                        market_id=condition_id,
                        name=outcome_name,
                    )
                )

            if len(outcomes) < 2:
                return None

            return Event(
                event_id=event_id,
                title=raw.get("title", ""),
                slug=raw.get("slug", ""),
                neg_risk=raw.get("negRisk", True),
                markets=markets,
                outcomes=outcomes,
            )
        except Exception as e:
            logger.warning(f"Failed to parse event: {e}")
            return None

    async def fetch_market_prices(self, token_ids: list[str]) -> dict[str, float]:
        """Fetch current mid-market prices for a list of token IDs via CLOB."""
        prices: dict[str, float] = {}
        for token_id in token_ids:
            try:
                data = await self._get(
                    f"/markets/{token_id}",
                )
                price = float(data.get("bestAsk", data.get("lastTradePrice", 0)) or 0)
                prices[token_id] = price
            except Exception as e:
                logger.debug(f"Failed to fetch price for {token_id}: {e}")
        return prices
