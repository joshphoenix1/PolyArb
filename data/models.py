from __future__ import annotations

import time
from enum import Enum

from pydantic import BaseModel, Field


class OrderBookLevel(BaseModel):
    price: float
    size: float


class OrderBookSnapshot(BaseModel):
    token_id: str
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)

    @property
    def best_bid(self) -> float | None:
        if not self.bids:
            return None
        return max(b.price for b in self.bids)

    @property
    def best_ask(self) -> float | None:
        if not self.asks:
            return None
        return min(a.price for a in self.asks)

    @property
    def best_ask_size(self) -> float:
        if not self.asks:
            return 0.0
        best = min(self.asks, key=lambda a: a.price)
        return best.size

    def ask_depth_at_price(self, max_price: float) -> float:
        """Total size available at or below max_price."""
        return sum(a.size for a in self.asks if a.price <= max_price)

    @property
    def is_stale(self) -> bool:
        from config.settings import settings
        return (time.time() - self.timestamp) > settings.stale_data_threshold


class Outcome(BaseModel):
    """A single outcome within a NegRisk event."""
    token_id: str  # YES token clob ID
    no_token_id: str = ""  # NO token clob ID
    market_id: str = ""  # CLOB market slug / condition_id
    name: str = ""
    orderbook: OrderBookSnapshot | None = None


class Market(BaseModel):
    """A single market (one outcome) in the Gamma API response."""
    condition_id: str = ""
    question: str = ""
    slug: str = ""
    token_id_yes: str = ""
    token_id_no: str = ""
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    min_tick_size: float = 0.01
    open_interest: float = 0.0


class Event(BaseModel):
    """A NegRisk event with multiple outcomes."""
    event_id: str
    title: str = ""
    slug: str = ""
    neg_risk: bool = True
    markets: list[Market] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)

    @property
    def num_outcomes(self) -> int:
        return len(self.outcomes)

    @property
    def all_token_ids(self) -> list[str]:
        return [o.token_id for o in self.outcomes]


class OpportunityStatus(str, Enum):
    DETECTED = "detected"
    EXECUTING = "executing"
    FILLED = "filled"
    FAILED = "failed"
    EXPIRED = "expired"


class ArbitrageOpportunity(BaseModel):
    """A detected NegRisk arbitrage opportunity."""
    event_id: str
    event_title: str = ""
    total_yes_cost: float  # Sum of best ask prices across all outcomes
    profit_per_set: float  # 1.0 - total_yes_cost
    max_sets: float  # Max number of complete sets executable (limited by depth)
    estimated_profit: float = 0.0  # profit_per_set * max_sets
    prices: dict[str, float] = Field(default_factory=dict)  # token_id -> ask price
    sizes: dict[str, float] = Field(default_factory=dict)  # token_id -> available size
    status: OpportunityStatus = OpportunityStatus.DETECTED
    timestamp: float = Field(default_factory=time.time)
    tick_size: float = 0.01

    def model_post_init(self, __context):
        if self.estimated_profit == 0.0:
            self.estimated_profit = self.profit_per_set * self.max_sets


class TradeRecord(BaseModel):
    """Record of an executed (or attempted) trade."""
    opportunity_event_id: str
    event_title: str = ""
    num_outcomes: int = 0
    intended_sets: float = 0.0
    filled_sets: float = 0.0
    total_cost: float = 0.0
    expected_profit: float = 0.0
    realized_profit: float = 0.0
    status: str = "pending"  # pending, filled, partial, failed
    order_ids: list[str] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)
    fill_details: dict = Field(default_factory=dict)
