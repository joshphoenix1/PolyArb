"""Risk manager - position limits, exposure caps, loss limits."""

from __future__ import annotations

import logging

from config.settings import settings
from core.portfolio import PortfolioTracker
from data.models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class RiskCheckResult:
    def __init__(self, allowed: bool, reason: str = "", max_size: float = 0.0):
        self.allowed = allowed
        self.reason = reason
        self.max_size = max_size


class RiskManager:
    """Enforces trading risk limits."""

    def __init__(self, portfolio: PortfolioTracker):
        self._portfolio = portfolio
        self._trading_halted = False
        self._halt_reason = ""

    @property
    def is_halted(self) -> bool:
        return self._trading_halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def halt_trading(self, reason: str):
        """Immediately halt all trading."""
        self._trading_halted = True
        self._halt_reason = reason
        logger.warning(f"TRADING HALTED: {reason}")

    def resume_trading(self):
        """Resume trading (manual override)."""
        self._trading_halted = False
        self._halt_reason = ""
        logger.info("Trading resumed")

    def check_opportunity(
        self, opp: ArbitrageOpportunity, usdc_balance: float
    ) -> RiskCheckResult:
        """Check if an opportunity passes all risk checks. Returns max allowed size."""

        # 1. Trading halt check
        if self._trading_halted:
            return RiskCheckResult(False, f"Trading halted: {self._halt_reason}")

        # 2. Daily loss limit
        daily_pnl = self._portfolio.get_daily_pnl()
        if daily_pnl <= -settings.daily_loss_limit:
            self.halt_trading(
                f"Daily loss limit reached: ${daily_pnl:.2f} <= -${settings.daily_loss_limit:.2f}"
            )
            return RiskCheckResult(False, self._halt_reason)

        # 3. Total exposure cap
        total_exposure = self._portfolio.get_total_exposure()
        remaining_exposure = settings.max_total_exposure - total_exposure
        if remaining_exposure <= 0:
            return RiskCheckResult(
                False,
                f"Max total exposure reached: ${total_exposure:.2f} >= ${settings.max_total_exposure:.2f}",
            )

        # 4. Per-event position limit
        event_exposure = self._portfolio.get_event_exposure(opp.event_id)
        remaining_event = settings.max_position_size - event_exposure
        if remaining_event <= 0:
            return RiskCheckResult(
                False,
                f"Max position for event reached: ${event_exposure:.2f} >= ${settings.max_position_size:.2f}",
            )

        # 5. Balance check
        if usdc_balance <= 0:
            return RiskCheckResult(False, "Insufficient USDC balance")

        # 6. Calculate max allowed sets
        cost_per_set = opp.total_yes_cost
        if cost_per_set <= 0:
            return RiskCheckResult(False, "Invalid cost per set")

        max_by_balance = usdc_balance / cost_per_set
        max_by_exposure = remaining_exposure / cost_per_set
        max_by_event = remaining_event / cost_per_set
        max_by_depth = opp.max_sets

        max_sets = min(max_by_balance, max_by_exposure, max_by_event, max_by_depth)

        if max_sets < 1.0:
            return RiskCheckResult(
                False,
                f"Max executable sets too small: {max_sets:.2f} "
                f"(balance={max_by_balance:.1f}, exposure={max_by_exposure:.1f}, "
                f"event={max_by_event:.1f}, depth={max_by_depth:.1f})",
            )

        # Floor to integer sets
        max_sets = float(int(max_sets))

        estimated_cost = max_sets * cost_per_set
        estimated_profit = max_sets * opp.profit_per_set

        logger.info(
            f"Risk check PASSED: {max_sets:.0f} sets | "
            f"cost=${estimated_cost:.2f} | profit=${estimated_profit:.4f}"
        )

        return RiskCheckResult(True, "", max_sets)
