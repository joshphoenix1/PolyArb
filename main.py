"""PolyArb - Polymarket NegRisk Arbitrage Bot

Entry point that orchestrates market scanning, price monitoring,
arbitrage detection, trade execution, and the web dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass, field

import uvicorn

from config.settings import settings
from core.detector import ArbitrageDetector
from core.executor import TradeExecutor
from core.portfolio import PortfolioTracker
from core.risk import RiskManager
from core.scanner import MarketScanner
from dashboard.app import create_app
from data.gamma import GammaClient
from data.websocket import WebSocketClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("polyarb")


@dataclass
class BotState:
    """Shared state accessible by all components including the dashboard."""
    running: bool = False
    paper_trading: bool = True
    start_time: float = field(default_factory=time.time)
    ws_client: WebSocketClient | None = None
    scanner: MarketScanner | None = None
    detector: ArbitrageDetector | None = None
    executor: TradeExecutor | None = None
    risk_manager: RiskManager | None = None
    portfolio: PortfolioTracker | None = None
    clob_client: object = None


async def run_bot():
    """Main bot coroutine."""
    state = BotState(paper_trading=settings.paper_trading)

    # ── 1. Initialize portfolio tracker ──
    state.portfolio = PortfolioTracker()
    logger.info("Portfolio tracker initialized")

    # ── 2. Initialize CLOB client ──
    clob_client = None
    if not settings.paper_trading:
        try:
            from py_clob_client.client import ClobClient

            clob_client = ClobClient(
                settings.clob_host,
                key=settings.private_key,
                chain_id=settings.chain_id,
            )
            # Derive API credentials (HMAC keys)
            clob_client.set_api_creds(clob_client.derive_api_key())
            state.clob_client = clob_client
            logger.info("CLOB client initialized (LIVE mode)")
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            logger.info("Falling back to paper trading mode")
            state.paper_trading = True
    else:
        logger.info("Running in PAPER TRADING mode")

    # ── 3. Initialize risk manager ──
    state.risk_manager = RiskManager(state.portfolio)

    # ── 4. Initialize executor ──
    state.executor = TradeExecutor(clob_client, state.risk_manager, state.portfolio)

    # ── 5. Initialize WebSocket client ──
    state.ws_client = WebSocketClient()

    # ── 6. Initialize detector ──
    state.detector = ArbitrageDetector(state.ws_client)
    state.detector.set_on_opportunity(state.executor.execute_opportunity)

    # Wire up WebSocket -> Detector
    state.ws_client.set_on_update(state.detector.on_orderbook_update)

    # ── 7. Initialize Gamma client and scanner ──
    gamma = GammaClient()
    state.scanner = MarketScanner(gamma, state.ws_client)

    # ── 8. Start the dashboard server ──
    app = create_app(state)
    config = uvicorn.Config(
        app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    # ── 9. Set up graceful shutdown ──
    shutdown_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # ── 10. Start all components ──
    state.running = True
    state.start_time = time.time()

    logger.info("=" * 60)
    logger.info("  PolyArb NegRisk Arbitrage Bot")
    logger.info(f"  Mode: {'PAPER' if state.paper_trading else 'LIVE'}")
    logger.info(f"  Min profit threshold: ${settings.min_profit_threshold}")
    logger.info(f"  Max position size: ${settings.max_position_size}")
    logger.info(f"  Max total exposure: ${settings.max_total_exposure}")
    logger.info(f"  Scan interval: {settings.scan_interval}s")
    logger.info(f"  Dashboard: http://localhost:{settings.dashboard_port}")
    logger.info("=" * 60)

    # Start scanner first (performs initial scan to discover events and their tokens)
    # The scanner will register tokens with the WS client before we connect
    await state.scanner.start()

    # Update detector with discovered events
    state.detector.update_events(state.scanner.events)

    # Now start WebSocket — it will subscribe to all registered tokens on connect
    await state.ws_client.start()

    # Start periodic tasks
    tasks = [
        asyncio.create_task(server.serve(), name="dashboard"),
        asyncio.create_task(_event_sync_loop(state), name="event_sync"),
        asyncio.create_task(shutdown_event.wait(), name="shutdown_wait"),
    ]

    # Wait for shutdown
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # ── Graceful shutdown ──
    logger.info("Shutting down...")
    state.running = False

    # Stop scanner
    await state.scanner.stop()

    # Stop WebSocket
    await state.ws_client.stop()

    # Close Gamma client
    await gamma.close()

    # Close portfolio DB
    state.portfolio.close()

    # Cancel remaining tasks
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    server.should_exit = True

    logger.info("Shutdown complete")


async def _event_sync_loop(state: BotState):
    """Periodically sync scanner events into the detector."""
    while state.running:
        await asyncio.sleep(5)
        if state.scanner and state.detector:
            state.detector.update_events(state.scanner.events)


def main():
    """Entry point."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
