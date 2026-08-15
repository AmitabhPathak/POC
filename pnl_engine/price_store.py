"""Mark price storage with as-of lookup."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pnl_engine.models import ConflictingEventError, PriceEvent


class PriceStore:
    """Venue-agnostic mark price series per symbol."""

    def __init__(self) -> None:
        self._series: dict[str, list[tuple[datetime, Decimal]]] = {}
        self._index: dict[str, dict[datetime, Decimal]] = {}

    def upsert(self, event: PriceEvent) -> bool:
        """Insert or update a price tick. Returns True if state changed."""
        symbol = event.symbol
        ts = event.timestamp
        price = event.price

        if symbol not in self._index:
            self._index[symbol] = {}
            self._series[symbol] = []

        existing = self._index[symbol].get(ts)
        if existing is not None:
            if existing == price:
                return False
            raise ConflictingEventError(
                f"Conflicting price correction for {symbol} at {ts}: "
                f"existing={existing}, new={price}"
            )

        self._index[symbol][ts] = price
        series = self._series[symbol]
        insert_at = bisect_right(series, (ts, Decimal("-1")))
        series.insert(insert_at, (ts, price))
        return True

    def get_price_at_or_before(
        self, symbol: str, as_of: datetime
    ) -> Optional[Decimal]:
        series = self._series.get(symbol)
        if not series:
            return None
        idx = bisect_right(series, (as_of, Decimal("Inf"))) - 1
        if idx < 0:
            return None
        return series[idx][1]

    def load_events(self, events: list[PriceEvent]) -> None:
        for event in sorted(events, key=lambda e: e.timestamp):
            self.upsert(event)
