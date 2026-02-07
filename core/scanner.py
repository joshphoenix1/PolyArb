"""Market scanner - periodically discovers NegRisk events via the Gamma API."""

from __future__ import annotations

import asyncio
import logging

from config.settings import settings
from data.gamma import GammaClient
from data.models import Event
from data.websocket import WebSocketClient

logger = logging.getLogger(__name__)

# Max token subscriptions to avoid overwhelming the WebSocket connection
MAX_SUBSCRIBED_TOKENS = 2000


class MarketScanner:
    """Periodically scans for new NegRisk events and subscribes to their orderbooks."""

    def __init__(self, gamma: GammaClient, ws_client: WebSocketClient):
        self._gamma = gamma
        self._ws = ws_client
        self._events: dict[str, Event] = {}  # event_id -> Event
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def events(self) -> dict[str, Event]:
        return self._events

    @property
    def event_count(self) -> int:
        return len(self._events)

    async def start(self):
        """Start periodic scanning."""
        self._running = True
        # Do initial scan immediately
        await self.scan_once()
        # Start periodic loop
        self._task = asyncio.create_task(self._scan_loop())

    async def stop(self):
        """Stop scanning."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _is_eligible(self, event: Event) -> bool:
        """Filter events: must have 2+ accepting-order markets."""
        active_markets = [
            m for m in event.markets
            if m.active and not m.closed and m.accepting_orders
        ]
        return len(active_markets) >= 2

    def _rank_events(self, events: list[Event]) -> list[Event]:
        """Rank events by arb potential. More outcomes = more mispricing potential."""
        return sorted(events, key=lambda e: e.num_outcomes, reverse=True)

    async def scan_once(self):
        """Perform a single scan for NegRisk events."""
        try:
            all_events = await self._gamma.fetch_negrisk_events()
            eligible = [e for e in all_events if self._is_eligible(e)]

            # Rank by potential and cap by token subscription limit
            ranked = self._rank_events(eligible)
            events: list[Event] = []
            total_tokens = 0
            for event in ranked:
                n_tokens = len(event.all_token_ids)
                if total_tokens + n_tokens > MAX_SUBSCRIBED_TOKENS:
                    break
                events.append(event)
                total_tokens += n_tokens

            new_count = 0
            removed_count = 0

            current_ids = set(self._events.keys())
            fetched_ids = {e.event_id for e in events}

            # Remove events that are no longer active
            for eid in current_ids - fetched_ids:
                event = self._events.pop(eid)
                await self._ws.unsubscribe(event.all_token_ids)
                removed_count += 1

            # Add/update events
            for event in events:
                if event.event_id not in self._events:
                    new_count += 1
                    await self._ws.subscribe(event.all_token_ids, event.event_id)
                self._events[event.event_id] = event

            logger.info(
                f"Scan complete: {len(self._events)} events monitored "
                f"({total_tokens} tokens, {len(eligible)} eligible of {len(all_events)} total, "
                f"+{new_count} new, -{removed_count} removed)"
            )

        except Exception as e:
            logger.error(f"Scan failed: {e}")

    async def _scan_loop(self):
        """Periodically re-scan for new events."""
        while self._running:
            await asyncio.sleep(settings.scan_interval)
            await self.scan_once()

    def get_event_for_token(self, token_id: str) -> Event | None:
        """Find the event that contains a given token ID."""
        event_id = self._ws.get_event_id_for_token(token_id)
        if event_id:
            return self._events.get(event_id)
        return None
