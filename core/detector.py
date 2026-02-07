"""Arbitrage detector - monitors price sums across NegRisk event outcomes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from config.settings import settings
from data.models import ArbitrageOpportunity, Event, OpportunityStatus
from data.websocket import WebSocketClient

logger = logging.getLogger(__name__)


class ArbitrageDetector:
    """Detects NegRisk arbitrage opportunities by monitoring YES price sums."""

    def __init__(self, ws_client: WebSocketClient):
        self._ws = ws_client
        self._events: dict[str, Event] = {}  # event_id -> Event
        self._opportunities: deque[ArbitrageOpportunity] = deque(maxlen=500)
        self._on_opportunity = None  # callback
        self._price_sums: dict[str, float] = {}  # event_id -> current YES sum
        self._lock = asyncio.Lock()

    @property
    def recent_opportunities(self) -> list[ArbitrageOpportunity]:
        return list(self._opportunities)

    @property
    def price_sums(self) -> dict[str, float]:
        return dict(self._price_sums)

    def set_on_opportunity(self, callback):
        """Set the callback for when an opportunity is detected.
        Signature: async def callback(opp: ArbitrageOpportunity) -> None
        """
        self._on_opportunity = callback

    def update_events(self, events: dict[str, Event]):
        """Update the set of events to monitor."""
        self._events = events

    async def check_event(self, event_id: str) -> ArbitrageOpportunity | None:
        """Check a single event for arbitrage opportunities."""
        event = self._events.get(event_id)
        if not event:
            return None

        prices: dict[str, float] = {}
        sizes: dict[str, float] = {}
        tick_size = 0.01

        for i, outcome in enumerate(event.outcomes):
            ob = self._ws.get_orderbook(outcome.token_id)
            if ob is None or ob.best_ask is None:
                # No orderbook data for this outcome, can't compute
                self._price_sums[event_id] = float("nan")
                return None

            if ob.is_stale:
                logger.debug(
                    f"Stale data for {outcome.name} in {event.title}, skipping"
                )
                self._price_sums[event_id] = float("nan")
                return None

            prices[outcome.token_id] = ob.best_ask
            sizes[outcome.token_id] = ob.best_ask_size

            # Use the tick size from the market if available
            if i < len(event.markets):
                tick_size = event.markets[i].min_tick_size

        total_yes_cost = sum(prices.values())
        self._price_sums[event_id] = total_yes_cost

        if total_yes_cost >= 1.0:
            return None

        profit_per_set = 1.0 - total_yes_cost

        if profit_per_set < settings.min_profit_threshold:
            return None

        # Max sets is limited by the smallest ask depth across all outcomes
        if not sizes:
            return None
        max_sets = min(sizes.values())
        if max_sets <= 0:
            return None

        opp = ArbitrageOpportunity(
            event_id=event_id,
            event_title=event.title,
            total_yes_cost=total_yes_cost,
            profit_per_set=profit_per_set,
            max_sets=max_sets,
            prices=prices,
            sizes=sizes,
            tick_size=tick_size,
        )

        logger.info(
            f"OPPORTUNITY: {event.title} | "
            f"YES sum={total_yes_cost:.4f} | "
            f"profit/set=${profit_per_set:.4f} | "
            f"max_sets={max_sets:.1f} | "
            f"est_profit=${opp.estimated_profit:.4f}"
        )

        async with self._lock:
            self._opportunities.append(opp)

        if self._on_opportunity:
            await self._on_opportunity(opp)

        return opp

    async def on_orderbook_update(self, token_id: str):
        """Called by WebSocketClient when an orderbook updates."""
        event_id = self._ws.get_event_id_for_token(token_id)
        if event_id:
            await self.check_event(event_id)

    async def check_all_events(self):
        """Check all monitored events for arbitrage opportunities."""
        for event_id in list(self._events.keys()):
            await self.check_event(event_id)
