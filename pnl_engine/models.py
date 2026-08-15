"""Domain models and typed events for the PnL engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class EventType(str, Enum):
    TRADE = "trade"
    FUNDING = "funding"
    PRICE = "price"


@dataclass(frozen=True)
class PositionKey:
    """Account-level position identity."""

    trader: str
    venue: str
    venue_account: str
    symbol: str


@dataclass(frozen=True)
class ReportKey:
    """Reporting grain: trader + symbol."""

    trader: str
    symbol: str


@dataclass(frozen=True)
class TradeEvent:
    timestamp: datetime
    venue: str
    trade_id: str
    trader: str
    venue_account: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str

    @property
    def event_type(self) -> EventType:
        return EventType.TRADE

    @property
    def position_key(self) -> PositionKey:
        return PositionKey(self.trader, self.venue, self.venue_account, self.symbol)

    @property
    def identity(self) -> tuple[str, str]:
        return (self.venue, self.trade_id)

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side == Side.BUY else -self.quantity


@dataclass(frozen=True)
class FundingEvent:
    timestamp: datetime
    event_id: str
    trader: str
    venue: str
    venue_account: str
    symbol: str
    asset: str
    amount: Decimal

    @property
    def event_type(self) -> EventType:
        return EventType.FUNDING

    @property
    def position_key(self) -> PositionKey:
        return PositionKey(self.trader, self.venue, self.venue_account, self.symbol)

    @property
    def identity(self) -> tuple[str, str]:
        return (self.venue, self.event_id)

    @property
    def report_key(self) -> ReportKey:
        return ReportKey(self.trader, self.symbol)


@dataclass(frozen=True)
class PriceEvent:
    timestamp: datetime
    symbol: str
    price: Decimal

    @property
    def event_type(self) -> EventType:
        return EventType.PRICE

    @property
    def identity(self) -> tuple[str, datetime]:
        return (self.symbol, self.timestamp)


@dataclass
class AccountPositionState:
    """Mutable position state for one account-level key."""

    quantity: Decimal = Decimal("0")
    avg_entry_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fees_usdt: Decimal = Decimal("0")
    last_event_timestamp: Optional[datetime] = None


@dataclass
class SymbolReport:
    """Aggregated PnL report for one trader and symbol."""

    trader: str
    symbol: str
    final_quantity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Optional[Decimal]
    funding_pnl: Decimal
    fees: Decimal
    total_pnl: Optional[Decimal]
    mark_price: Optional[Decimal]

    def format_report(self) -> str:
        lines = [
            self.trader,
            self.symbol,
            f"Final Quantity\t{self.final_quantity:.8f}".rstrip("0").rstrip(".")
            if "." in f"{self.final_quantity:.8f}"
            else f"Final Quantity\t{self.final_quantity:.8f}",
            f"Realized PnL\t{_fmt_signed(self.realized_pnl)}",
            f"Unrealized PnL\t{_fmt_optional_signed(self.unrealized_pnl)}",
            f"Funding PnL\t{_fmt_signed(self.funding_pnl)}",
            f"Fees\t{_fmt_usdt(self.fees)}",
            "--------------------------------",
            f"Total PnL\t{_fmt_optional_signed(self.total_pnl)}",
        ]
        return "\n".join(lines)


class PnLError(Exception):
    """Base error for PnL engine."""


class LateEventError(PnLError):
    """Raised when an event predates already-processed state for its partition."""


class ConflictingEventError(PnLError):
    """Raised when an event repeats an identity with different content."""


def _fmt_usdt(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _fmt_signed(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"))
    sign = "+" if rounded >= 0 else ""
    return f"{sign}{rounded:.2f}"


def _fmt_optional_signed(value: Optional[Decimal]) -> str:
    if value is None:
        return "unavailable"
    return _fmt_signed(value)
