"""WebSocket client for real-time Polymarket orderbook data."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Coroutine

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State as WsState

from config.settings import settings
from data.models import OrderBookLevel, OrderBookSnapshot

logger = logging.getLogger(__name__)

# Type for the callback when an orderbook updates
OnUpdateCallback = Callable[[str], Coroutine]  # token_id -> async callback


class WebSocketClient:
    """Manages WebSocket connections to the Polymarket CLOB for real-time orderbook data."""

    def __init__(self):
        self.ws_url = settings.ws_host
        self._ws = None
        self._orderbooks: dict[str, OrderBookSnapshot] = {}
        self._subscribed_tokens: set[str] = set()
        self._token_to_event: dict[str, str] = {}  # token_id -> event_id
        self._on_update: OnUpdateCallback | None = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._initial_connect = True  # Track if this is the first subscribe after connect

    def set_on_update(self, callback: OnUpdateCallback):
        """Set callback invoked when any orderbook updates."""
        self._on_update = callback

    def get_orderbook(self, token_id: str) -> OrderBookSnapshot | None:
        return self._orderbooks.get(token_id)

    def get_event_id_for_token(self, token_id: str) -> str | None:
        return self._token_to_event.get(token_id)

    def _is_ws_open(self) -> bool:
        """Check if the WebSocket connection is open (compatible with websockets v15+)."""
        if self._ws is None:
            return False
        return self._ws.state == WsState.OPEN

    @property
    def is_connected(self) -> bool:
        return self._is_ws_open()

    @property
    def subscribed_count(self) -> int:
        return len(self._subscribed_tokens)

    async def subscribe(self, token_ids: list[str], event_id: str):
        """Subscribe to orderbook updates for a list of token IDs."""
        new_tokens = [t for t in token_ids if t not in self._subscribed_tokens]
        if not new_tokens:
            return

        for tid in new_tokens:
            self._subscribed_tokens.add(tid)
            self._token_to_event[tid] = event_id
            self._orderbooks[tid] = OrderBookSnapshot(token_id=tid)

        if self._ws and self._is_ws_open():
            await self._send_subscribe(new_tokens, is_initial=False)

    async def unsubscribe(self, token_ids: list[str]):
        """Unsubscribe from orderbook updates."""
        to_unsub = [t for t in token_ids if t in self._subscribed_tokens]
        for tid in to_unsub:
            self._subscribed_tokens.discard(tid)
            self._token_to_event.pop(tid, None)
            self._orderbooks.pop(tid, None)

        if self._ws and self._is_ws_open() and to_unsub:
            await self._send_unsubscribe(to_unsub)

    async def start(self):
        """Start the WebSocket connection loop."""
        self._running = True
        self._task = asyncio.create_task(self._connection_loop())

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _connection_loop(self):
        """Main connection loop with automatic reconnection."""
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=None,  # We handle pings manually
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    logger.info("WebSocket connected")

                    # Start application-level ping task
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))

                    # Re-subscribe to all tokens on reconnect
                    if self._subscribed_tokens:
                        await self._send_subscribe(
                            list(self._subscribed_tokens), is_initial=True
                        )

                    await self._receive_loop(ws)

            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
            finally:
                if self._ping_task:
                    self._ping_task.cancel()
                    self._ping_task = None

            if self._running:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    async def _ping_loop(self, ws):
        """Send application-level PING every 10 seconds to keep connection alive."""
        try:
            while True:
                await asyncio.sleep(10)
                if self._is_ws_open():
                    await ws.send("PING")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Ping error: {e}")

    async def _send_subscribe(self, token_ids: list[str], is_initial: bool = True):
        """Send subscription messages for token IDs."""
        if not self._ws or not self._is_ws_open():
            return

        if is_initial:
            # Initial subscription: one message with ALL token IDs
            # The server treats each "type: market" as the full subscription set
            msg = {
                "assets_ids": token_ids,
                "type": "market",
            }
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as e:
                logger.error(f"Failed to send initial subscribe: {e}")
                return
        else:
            # Dynamic subscription: add tokens incrementally
            batch_size = 100
            for i in range(0, len(token_ids), batch_size):
                batch = token_ids[i : i + batch_size]
                msg = {
                    "assets_ids": batch,
                    "operation": "subscribe",
                }
                try:
                    await self._ws.send(json.dumps(msg))
                except Exception as e:
                    logger.error(f"Failed to subscribe batch: {e}")
                if i + batch_size < len(token_ids):
                    await asyncio.sleep(0.1)

        logger.info(f"Subscribed to {len(token_ids)} token orderbooks")

    async def _send_unsubscribe(self, token_ids: list[str]):
        """Send unsubscribe messages."""
        if not self._ws or not self._is_ws_open():
            return

        batch_size = 50
        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i : i + batch_size]
            msg = {
                "assets_ids": batch,
                "operation": "unsubscribe",
            }
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as e:
                logger.error(f"Failed to unsubscribe: {e}")

    async def _receive_loop(self, ws):
        """Process incoming WebSocket messages."""
        async for raw_msg in ws:
            if raw_msg == "PONG":
                continue
            try:
                parsed = json.loads(raw_msg)
                # Server may send a list of messages or a single message
                if isinstance(parsed, list):
                    for msg in parsed:
                        if isinstance(msg, dict):
                            await self._handle_message(msg)
                elif isinstance(parsed, dict):
                    await self._handle_message(parsed)
            except json.JSONDecodeError:
                logger.debug(f"Non-JSON message: {str(raw_msg)[:100]}")
            except Exception as e:
                logger.error(f"Error handling WS message: {e}")

    async def _handle_message(self, msg: dict):
        """Handle a parsed WebSocket message, routing on event_type."""
        event_type = msg.get("event_type", "")

        if event_type == "book":
            await self._handle_book_snapshot(msg)
        elif event_type == "price_change":
            await self._handle_price_change(msg)
        elif event_type == "last_trade_price":
            pass  # Informational only
        elif event_type == "tick_size_change":
            pass  # Could update tick sizes
        elif event_type == "best_bid_ask":
            await self._handle_best_bid_ask(msg)

    async def _handle_book_snapshot(self, msg: dict):
        """Handle a full orderbook snapshot."""
        asset_id = msg.get("asset_id", "")
        if asset_id not in self._subscribed_tokens:
            return

        bids = [
            OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in msg.get("bids", [])
        ]
        asks = [
            OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in msg.get("asks", [])
        ]

        self._orderbooks[asset_id] = OrderBookSnapshot(
            token_id=asset_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )

        if self._on_update:
            await self._on_update(asset_id)

    async def _handle_price_change(self, msg: dict):
        """Handle price_change events - contains changes array with per-asset updates."""
        changes = msg.get("price_changes", msg.get("changes", []))

        for change in changes:
            asset_id = change.get("asset_id", "")
            if asset_id not in self._subscribed_tokens:
                continue

            ob = self._orderbooks.get(asset_id)
            if ob is None:
                continue

            price = float(change.get("price", 0))
            size = float(change.get("size", 0))
            side = change.get("side", "")

            if side == "BUY":
                ob.bids = [b for b in ob.bids if abs(b.price - price) > 1e-9]
                if size > 0:
                    ob.bids.append(OrderBookLevel(price=price, size=size))
            elif side == "SELL":
                ob.asks = [a for a in ob.asks if abs(a.price - price) > 1e-9]
                if size > 0:
                    ob.asks.append(OrderBookLevel(price=price, size=size))

            ob.timestamp = time.time()

            if self._on_update:
                await self._on_update(asset_id)

    async def _handle_best_bid_ask(self, msg: dict):
        """Handle best_bid_ask events if available."""
        asset_id = msg.get("asset_id", "")
        if asset_id not in self._subscribed_tokens:
            return

        ob = self._orderbooks.get(asset_id)
        if ob is None:
            return

        # Update best bid/ask from the message
        best_bid = msg.get("best_bid")
        best_ask = msg.get("best_ask")

        if best_bid is not None:
            bid_price = float(best_bid)
            # Keep existing depth but ensure best bid is current
            ob.bids = [b for b in ob.bids if b.price < bid_price - 1e-9]
            ob.bids.append(OrderBookLevel(price=bid_price, size=1.0))

        if best_ask is not None:
            ask_price = float(best_ask)
            ob.asks = [a for a in ob.asks if a.price > ask_price + 1e-9]
            ob.asks.append(OrderBookLevel(price=ask_price, size=1.0))

        ob.timestamp = time.time()

        if self._on_update:
            await self._on_update(asset_id)
