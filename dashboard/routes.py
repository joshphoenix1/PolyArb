"""Dashboard API routes and SSE stream."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

if TYPE_CHECKING:
    from main import BotState

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def create_router(bot_state: "BotState") -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def index():
        html_path = TEMPLATE_DIR / "index.html"
        return HTMLResponse(content=html_path.read_text())

    @router.get("/api/status")
    async def status():
        return _build_status(bot_state)

    @router.get("/api/events")
    async def events():
        scanner = bot_state.scanner
        detector = bot_state.detector
        if not scanner:
            return []

        result = []
        price_sums = detector.price_sums if detector else {}

        for eid, event in scanner.events.items():
            yes_sum = price_sums.get(eid)
            outcome_prices = []
            for outcome in event.outcomes:
                ob = bot_state.ws_client.get_orderbook(outcome.token_id) if bot_state.ws_client else None
                outcome_prices.append({
                    "name": outcome.name,
                    "token_id": outcome.token_id[:12] + "...",
                    "best_ask": ob.best_ask if ob else None,
                    "best_bid": ob.best_bid if ob else None,
                    "ask_size": ob.best_ask_size if ob else 0,
                })

            result.append({
                "event_id": eid,
                "title": event.title,
                "num_outcomes": event.num_outcomes,
                "yes_sum": round(yes_sum, 4) if yes_sum and yes_sum == yes_sum else None,
                "spread_from_1": round(1.0 - yes_sum, 4) if yes_sum and yes_sum == yes_sum else None,
                "outcomes": outcome_prices,
            })

        # Sort by spread (most profitable first)
        result.sort(key=lambda x: x.get("spread_from_1") or -999, reverse=True)
        return result

    @router.get("/api/opportunities")
    async def opportunities():
        detector = bot_state.detector
        if not detector:
            return []

        return [
            {
                "event_id": o.event_id,
                "event_title": o.event_title,
                "total_yes_cost": round(o.total_yes_cost, 4),
                "profit_per_set": round(o.profit_per_set, 4),
                "max_sets": o.max_sets,
                "estimated_profit": round(o.estimated_profit, 4),
                "status": o.status.value,
                "timestamp": o.timestamp,
                "age_seconds": round(time.time() - o.timestamp, 1),
            }
            for o in reversed(detector.recent_opportunities)
        ]

    @router.get("/api/trades")
    async def trades():
        portfolio = bot_state.portfolio
        if not portfolio:
            return []
        return portfolio.get_recent_trades(limit=100)

    @router.get("/api/positions")
    async def positions():
        portfolio = bot_state.portfolio
        if not portfolio:
            return []
        return portfolio.get_open_positions()

    @router.get("/api/pnl_history")
    async def pnl_history():
        portfolio = bot_state.portfolio
        if not portfolio:
            return []
        return portfolio.get_pnl_history()

    @router.get("/api/stream")
    async def stream(request: Request):
        """Server-Sent Events stream for live dashboard updates."""

        async def event_generator():
            while True:
                if await request.is_disconnected():
                    break

                data = _build_status(bot_state)
                yield {"event": "status", "data": json.dumps(data)}

                await asyncio.sleep(1)

        return EventSourceResponse(event_generator())

    @router.post("/api/halt")
    async def halt_trading():
        if bot_state.risk_manager:
            bot_state.risk_manager.halt_trading("Manual halt via dashboard")
        return {"status": "halted"}

    @router.post("/api/resume")
    async def resume_trading():
        if bot_state.risk_manager:
            bot_state.risk_manager.resume_trading()
        return {"status": "resumed"}

    return router


def _build_status(bot_state: "BotState") -> dict:
    portfolio = bot_state.portfolio
    risk = bot_state.risk_manager
    ws = bot_state.ws_client
    scanner = bot_state.scanner
    detector = bot_state.detector

    return {
        "running": bot_state.running,
        "paper_trading": bot_state.paper_trading,
        "uptime_seconds": round(time.time() - bot_state.start_time, 1),
        "ws_connected": ws.is_connected if ws else False,
        "ws_subscriptions": ws.subscribed_count if ws else 0,
        "events_monitored": scanner.event_count if scanner else 0,
        "opportunities_detected": len(detector.recent_opportunities) if detector else 0,
        "total_exposure": round(portfolio.get_total_exposure(), 2) if portfolio else 0,
        "daily_pnl": round(portfolio.get_daily_pnl(), 4) if portfolio else 0,
        "cumulative_pnl": round(portfolio.get_cumulative_pnl(), 4) if portfolio else 0,
        "trade_count": portfolio.get_trade_count() if portfolio else 0,
        "trading_halted": risk.is_halted if risk else False,
        "halt_reason": risk.halt_reason if risk else "",
        "timestamp": time.time(),
    }
