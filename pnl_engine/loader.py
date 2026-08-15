"""CSV data loading utilities."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pnl_engine.logging_config import get_logger
from pnl_engine.models import FundingEvent, PriceEvent, Side, TradeEvent

logger = get_logger()

PERIOD_START = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 8, 2, 0, 0, 0, tzinfo=timezone.utc)


def parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _decimal(value: str) -> Decimal:
    return Decimal(value.strip())


def in_calculation_window(ts: datetime) -> bool:
    return PERIOD_START <= ts < PERIOD_END


def load_opening_positions(path: Path) -> dict[tuple[str, str, str, str], tuple[Decimal, Decimal]]:
    """Return map of position key -> (quantity, avg_entry_price)."""
    logger.info("Loading opening positions from %s", path)
    positions: dict[tuple[str, str, str, str], tuple[Decimal, Decimal]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (
                row["trader"],
                row["venue"],
                row["venue_account"],
                row["symbol"],
            )
            positions[key] = (
                _decimal(row["quantity"]),
                _decimal(row["avg_entry_price"]),
            )
            logger.debug(
                "Opening position loaded: trader=%s venue=%s account=%s symbol=%s "
                "quantity=%s avg_entry=%s",
                key[0],
                key[1],
                key[2],
                key[3],
                positions[key][0],
                positions[key][1],
            )
    logger.info("Loaded %d opening positions", len(positions))
    return positions


def load_trades(path: Path) -> list[TradeEvent]:
    logger.info("Loading trades from %s", path)
    seen: dict[tuple[str, str], TradeEvent] = {}
    trades: list[TradeEvent] = []
    skipped_window = 0
    skipped_duplicate = 0

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = parse_timestamp(row["timestamp"])
            if not in_calculation_window(ts):
                skipped_window += 1
                logger.debug("Trade skipped (outside window): trade_id=%s ts=%s", row["trade_id"], ts)
                continue
            event = TradeEvent(
                timestamp=ts,
                venue=row["venue"],
                trade_id=row["trade_id"],
                trader=row["trader"],
                venue_account=row["venue_account"],
                symbol=row["symbol"],
                side=Side(row["side"]),
                quantity=_decimal(row["quantity"]),
                price=_decimal(row["price"]),
                fee=_decimal(row["fee"]),
                fee_asset=row["fee_asset"],
            )
            identity = event.identity
            if identity in seen:
                existing = seen[identity]
                if _same_trade(existing, event):
                    skipped_duplicate += 1
                    logger.warning(
                        "Duplicate trade skipped on load: venue=%s trade_id=%s ts=%s",
                        event.venue,
                        event.trade_id,
                        event.timestamp,
                    )
                    continue
                raise ValueError(
                    f"Conflicting trade correction for {identity}: "
                    f"existing={existing}, new={event}"
                )
            seen[identity] = event
            trades.append(event)

    trades.sort(key=lambda t: (t.timestamp, t.venue, t.trade_id))
    logger.info(
        "Loaded %d trades (%d outside window, %d duplicates skipped)",
        len(trades),
        skipped_window,
        skipped_duplicate,
    )
    return trades


def load_funding(path: Path) -> list[FundingEvent]:
    logger.info("Loading funding events from %s", path)
    seen: dict[tuple[str, str], FundingEvent] = {}
    events: list[FundingEvent] = []
    skipped_window = 0
    skipped_duplicate = 0

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = parse_timestamp(row["timestamp"])
            if not in_calculation_window(ts):
                skipped_window += 1
                logger.debug(
                    "Funding skipped (outside window): event_id=%s ts=%s",
                    row["event_id"],
                    ts,
                )
                continue
            event = FundingEvent(
                timestamp=ts,
                event_id=row["event_id"],
                trader=row["trader"],
                venue=row["venue"],
                venue_account=row["venue_account"],
                symbol=row["symbol"],
                asset=row["asset"],
                amount=_decimal(row["amount"]),
            )
            identity = event.identity
            if identity in seen:
                existing = seen[identity]
                if _same_funding(existing, event):
                    skipped_duplicate += 1
                    logger.warning(
                        "Duplicate funding skipped on load: venue=%s event_id=%s ts=%s",
                        event.venue,
                        event.event_id,
                        event.timestamp,
                    )
                    continue
                raise ValueError(
                    f"Conflicting funding correction for {identity}"
                )
            seen[identity] = event
            events.append(event)

    events.sort(key=lambda e: (e.timestamp, e.venue, e.event_id))
    logger.info(
        "Loaded %d funding events (%d outside window, %d duplicates skipped)",
        len(events),
        skipped_window,
        skipped_duplicate,
    )
    return events


def load_prices(path: Path) -> list[PriceEvent]:
    logger.info("Loading prices from %s", path)
    seen: dict[tuple[str, datetime], PriceEvent] = {}
    events: list[PriceEvent] = []
    skipped_duplicate = 0

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = parse_timestamp(row["timestamp"])
            event = PriceEvent(
                timestamp=ts,
                symbol=row["symbol"],
                price=_decimal(row["price"]),
            )
            identity = event.identity
            if identity in seen:
                existing = seen[identity]
                if existing.price == event.price:
                    skipped_duplicate += 1
                    logger.debug(
                        "Duplicate price skipped on load: symbol=%s ts=%s price=%s",
                        event.symbol,
                        event.timestamp,
                        event.price,
                    )
                    continue
                raise ValueError(
                    f"Conflicting price correction for {identity}"
                )
            seen[identity] = event
            events.append(event)

    events.sort(key=lambda e: (e.timestamp, e.symbol))
    logger.info(
        "Loaded %d price ticks (%d duplicates skipped)",
        len(events),
        skipped_duplicate,
    )
    return events


def _same_trade(a: TradeEvent, b: TradeEvent) -> bool:
    return (
        a.timestamp == b.timestamp
        and a.trader == b.trader
        and a.venue_account == b.venue_account
        and a.symbol == b.symbol
        and a.side == b.side
        and a.quantity == b.quantity
        and a.price == b.price
        and a.fee == b.fee
        and a.fee_asset == b.fee_asset
    )


def _same_funding(a: FundingEvent, b: FundingEvent) -> bool:
    return (
        a.timestamp == b.timestamp
        and a.trader == b.trader
        and a.venue_account == b.venue_account
        and a.symbol == b.symbol
        and a.asset == b.asset
        and a.amount == b.amount
    )
