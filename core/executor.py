"""Trade executor - places batch orders via the Polymarket CLOB API."""

from __future__ import annotations

import asyncio
import logging
import math
import time

from config.settings import settings
from core.portfolio import PortfolioTracker
from core.risk import RiskManager
from data.models import ArbitrageOpportunity, OpportunityStatus, TradeRecord

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Executes NegRisk arbitrage trades via the Polymarket CLOB client."""

    def __init__(
        self,
        clob_client,
        risk_manager: RiskManager,
        portfolio: PortfolioTracker,
    ):
        self._clob = clob_client
        self._risk = risk_manager
        self._portfolio = portfolio
        self._executing = False
        self._execution_lock = asyncio.Lock()
        self._recent_trades: list[TradeRecord] = []

    @property
    def is_executing(self) -> bool:
        return self._executing

    @property
    def recent_trades(self) -> list[TradeRecord]:
        return list(self._recent_trades[-100:])

    async def execute_opportunity(self, opp: ArbitrageOpportunity) -> TradeRecord | None:
        """Attempt to execute an arbitrage opportunity."""
        async with self._execution_lock:
            return await self._execute(opp)

    async def _execute(self, opp: ArbitrageOpportunity) -> TradeRecord | None:
        self._executing = True
        try:
            return await self._do_execute(opp)
        finally:
            self._executing = False

    async def _do_execute(self, opp: ArbitrageOpportunity) -> TradeRecord | None:
        # Paper trading mode
        if settings.paper_trading:
            return await self._paper_trade(opp)

        from py_clob_client.clob_types import (
            AssetType,
            BalanceAllowanceParams,
            OrderArgs,
            OrderType,
            PostOrdersArgs,
        )

        # 1. Get current balance
        try:
            balance_info = await asyncio.to_thread(
                self._clob.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            usdc_balance = float(balance_info.get("balance", 0)) / 1e6  # USDC has 6 decimals
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return self._failed_trade(opp, f"Balance check failed: {e}")

        # 2. Risk check
        risk_result = self._risk.check_opportunity(opp, usdc_balance)
        if not risk_result.allowed:
            logger.info(f"Risk check rejected: {risk_result.reason}")
            opp.status = OpportunityStatus.EXPIRED
            return None

        num_sets = int(risk_result.max_size)
        if num_sets < 1:
            return None

        # 3. Re-verify prices via REST (WebSocket may lag)
        verified = await self._verify_prices(opp)
        if not verified:
            logger.info("Price verification failed - opportunity may have closed")
            opp.status = OpportunityStatus.EXPIRED
            return None

        # 4. Get tick sizes and build orders - one BUY YES per outcome
        post_args_list: list[PostOrdersArgs] = []
        for token_id, ask_price in opp.prices.items():
            # Get tick size for this specific token
            try:
                tick_size_str = await asyncio.to_thread(
                    self._clob.get_tick_size, token_id
                )
                tick_size = float(tick_size_str)
            except Exception:
                tick_size = opp.tick_size

            # Align price to tick size
            aligned_price = self._align_to_tick(ask_price, tick_size)

            try:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=aligned_price,
                    size=float(num_sets),
                    side="BUY",
                )
                signed_order = await asyncio.to_thread(
                    self._clob.create_order, order_args
                )
                post_args_list.append(
                    PostOrdersArgs(
                        order=signed_order,
                        orderType=OrderType.FOK,
                    )
                )
            except Exception as e:
                logger.error(f"Failed to create order for {token_id}: {e}")
                return self._failed_trade(opp, f"Order creation failed: {e}")

        if len(post_args_list) != len(opp.prices):
            return self._failed_trade(opp, "Not all orders could be created")

        # 5. Submit orders in batches (max 15 per batch)
        opp.status = OpportunityStatus.EXECUTING
        all_responses = []
        batch_size = 15

        for i in range(0, len(post_args_list), batch_size):
            batch = post_args_list[i : i + batch_size]
            try:
                responses = await asyncio.to_thread(self._clob.post_orders, batch)
                if isinstance(responses, list):
                    all_responses.extend(responses)
                else:
                    all_responses.append(responses)
            except Exception as e:
                logger.error(f"Batch order submission failed: {e}")
                opp.status = OpportunityStatus.FAILED
                return self._failed_trade(opp, f"Order submission failed: {e}")

        # 6. Process results
        trade = self._process_fill_results(opp, all_responses, num_sets)
        self._recent_trades.append(trade)
        self._portfolio.record_trade(trade)

        # Update positions
        if trade.status == "filled":
            for token_id, price in opp.prices.items():
                self._portfolio.update_position(
                    opp.event_id,
                    opp.event_title,
                    token_id,
                    trade.filled_sets,
                    price,
                )

        return trade

    async def _paper_trade(self, opp: ArbitrageOpportunity) -> TradeRecord:
        """Simulate a trade without actually executing."""
        # Simple risk check with a simulated balance
        risk_result = self._risk.check_opportunity(opp, 10000.0)
        if not risk_result.allowed:
            logger.info(f"[PAPER] Risk rejected: {risk_result.reason}")
            return self._failed_trade(opp, f"[PAPER] {risk_result.reason}")

        num_sets = min(int(risk_result.max_size), int(opp.max_sets))
        if num_sets < 1:
            num_sets = 1

        total_cost = num_sets * opp.total_yes_cost
        expected_profit = num_sets * opp.profit_per_set

        trade = TradeRecord(
            opportunity_event_id=opp.event_id,
            event_title=opp.event_title,
            num_outcomes=len(opp.prices),
            intended_sets=num_sets,
            filled_sets=num_sets,
            total_cost=total_cost,
            expected_profit=expected_profit,
            realized_profit=expected_profit,  # Assume full fill in paper mode
            status="filled",
            timestamp=time.time(),
        )

        self._recent_trades.append(trade)
        self._portfolio.record_trade(trade)

        logger.info(
            f"[PAPER] Trade executed: {opp.event_title} | "
            f"{num_sets} sets | cost=${total_cost:.4f} | profit=${expected_profit:.4f}"
        )

        return trade

    async def _verify_prices(self, opp: ArbitrageOpportunity) -> bool:
        """Re-check prices via REST to confirm the opportunity is still valid."""
        try:
            total_cost = 0.0
            for token_id in opp.prices:
                book = await asyncio.to_thread(
                    self._clob.get_order_book, token_id
                )
                # OrderBookSummary has .asks as list of OrderSummary
                asks = book.asks if hasattr(book, "asks") else book.get("asks", [])
                if not asks:
                    return False
                best_ask = min(
                    float(a.price if hasattr(a, "price") else a["price"])
                    for a in asks
                )
                total_cost += best_ask

            if total_cost >= 1.0:
                return False
            profit = 1.0 - total_cost
            return profit >= settings.min_profit_threshold

        except Exception as e:
            logger.error(f"Price verification failed: {e}")
            return False

    def _align_to_tick(self, price: float, tick_size: float) -> float:
        """Round price up to the nearest tick size (we're buying, so round up)."""
        if tick_size <= 0:
            tick_size = 0.01
        # Round up to nearest tick
        aligned = math.ceil(price / tick_size) * tick_size
        # Clamp to [tick_size, 1.0 - tick_size]
        aligned = max(tick_size, min(aligned, 1.0 - tick_size))
        decimals = len(str(tick_size).rstrip("0").split(".")[-1])
        return round(aligned, decimals)

    def _process_fill_results(
        self, opp: ArbitrageOpportunity, responses: list, num_sets: int
    ) -> TradeRecord:
        """Process order fill responses into a TradeRecord."""
        order_ids = []
        fill_details = {}
        all_filled = True

        for resp in responses:
            if isinstance(resp, dict):
                oid = resp.get("orderID", resp.get("id", ""))
                status = resp.get("status", "")
                order_ids.append(oid)
                fill_details[oid] = resp
                if status not in ("MATCHED", "FILLED"):
                    all_filled = False
            else:
                all_filled = False

        total_cost = num_sets * opp.total_yes_cost
        expected_profit = num_sets * opp.profit_per_set

        if all_filled:
            status = "filled"
            filled_sets = num_sets
            realized_profit = expected_profit
            opp.status = OpportunityStatus.FILLED
        else:
            status = "failed"
            filled_sets = 0
            realized_profit = 0.0
            opp.status = OpportunityStatus.FAILED

        return TradeRecord(
            opportunity_event_id=opp.event_id,
            event_title=opp.event_title,
            num_outcomes=len(opp.prices),
            intended_sets=num_sets,
            filled_sets=filled_sets,
            total_cost=total_cost if all_filled else 0,
            expected_profit=expected_profit,
            realized_profit=realized_profit,
            status=status,
            order_ids=order_ids,
            fill_details=fill_details,
            timestamp=time.time(),
        )

    def _failed_trade(self, opp: ArbitrageOpportunity, reason: str) -> TradeRecord:
        opp.status = OpportunityStatus.FAILED
        trade = TradeRecord(
            opportunity_event_id=opp.event_id,
            event_title=opp.event_title,
            num_outcomes=len(opp.prices),
            status="failed",
            fill_details={"error": reason},
            timestamp=time.time(),
        )
        self._recent_trades.append(trade)
        return trade
