"""Account-level position tracking with weighted-average cost basis."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pnl_engine.models import AccountPositionState, Side, TradeEvent


def apply_opening_position(
    state: AccountPositionState,
    quantity: Decimal,
    avg_entry_price: Decimal,
) -> None:
    """Initialize from opening snapshot."""
    state.quantity = quantity
    state.avg_entry_price = avg_entry_price


def apply_trade(state: AccountPositionState, trade: TradeEvent) -> None:
    """Apply a trade using weighted-average cost basis."""
    signed_qty = trade.signed_quantity
    price = trade.price
    current_qty = state.quantity

    if current_qty == 0:
        state.quantity = signed_qty
        state.avg_entry_price = price
        return

    if _same_sign(current_qty, signed_qty):
        # Adding to existing position.
        new_qty = current_qty + signed_qty
        state.avg_entry_price = (
            current_qty * state.avg_entry_price + signed_qty * price
        ) / new_qty
        state.quantity = new_qty
        return

    # Reducing or flipping position.
    closed_qty = min(abs(current_qty), abs(signed_qty))
    if current_qty > 0:
        # Closing/reducing long.
        state.realized_pnl += (price - state.avg_entry_price) * closed_qty
    else:
        # Closing/reducing short.
        state.realized_pnl += (state.avg_entry_price - price) * closed_qty

    remaining_qty = current_qty + signed_qty
    if remaining_qty == 0:
        state.quantity = Decimal("0")
        state.avg_entry_price = Decimal("0")
    elif _same_sign(current_qty, remaining_qty):
        # Partial reduction; average entry unchanged.
        state.quantity = remaining_qty
    else:
        # Position flipped to opposite side; remainder opens at trade price.
        state.quantity = remaining_qty
        state.avg_entry_price = price


def compute_unrealized(
    state: AccountPositionState, mark_price: Decimal | None
) -> Decimal | None:
    if state.quantity == 0:
        return Decimal("0")
    if mark_price is None:
        return None
    return (mark_price - state.avg_entry_price) * state.quantity


def _same_sign(a: Decimal, b: Decimal) -> bool:
    return (a > 0 and b > 0) or (a < 0 and b < 0)


def check_late_event(state: AccountPositionState, timestamp: datetime) -> None:
    from pnl_engine.models import LateEventError

    if (
        state.last_event_timestamp is not None
        and timestamp < state.last_event_timestamp
    ):
        raise LateEventError(
            f"Late event at {timestamp} predates last processed "
            f"event at {state.last_event_timestamp}"
        )


def mark_processed(state: AccountPositionState, timestamp: datetime) -> None:
    if state.last_event_timestamp is None or timestamp >= state.last_event_timestamp:
        state.last_event_timestamp = timestamp
