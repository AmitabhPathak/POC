"""Core PnL engine with incremental event processing."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Union

from pnl_engine.config import AppConfig, DataFilesConfig
from pnl_engine.loader import (
    PERIOD_END,
    load_funding,
    load_opening_positions,
    load_prices,
    load_trades,
)
from pnl_engine.logging_config import get_logger
from pnl_engine.models import (
    AccountPositionState,
    ConflictingEventError,
    FundingEvent,
    LateEventError,
    PositionKey,
    PriceEvent,
    ReportKey,
    SymbolReport,
    TradeEvent,
)
from pnl_engine.position import (
    apply_opening_position,
    apply_trade,
    check_late_event,
    compute_unrealized,
    mark_processed,
)
from pnl_engine.price_store import PriceStore

ProcessableEvent = Union[TradeEvent, FundingEvent, PriceEvent]

logger = get_logger()

DEFAULT_DATA_FILES = DataFilesConfig(
    opening_positions="opening_positions.csv",
    trades="trades.csv",
    funding="funding.csv",
    prices="prices.csv",
)


class PnLEngine:
    """Maintains position state and computes PnL incrementally."""

    def __init__(self) -> None:
        self._positions: dict[PositionKey, AccountPositionState] = {}
        self._funding: dict[ReportKey, Decimal] = {}
        self._price_store = PriceStore()
        self._processed_trades: dict[tuple[str, str], TradeEvent] = {}
        self._processed_funding: dict[tuple[str, str], FundingEvent] = {}
        self._global_last_trade_ts: datetime | None = None
        self._global_last_funding_ts: datetime | None = None

    @property
    def price_store(self) -> PriceStore:
        return self._price_store

    def load_from_config(self, config: AppConfig) -> None:
        """Bootstrap engine using paths from application config."""
        self.load_from_directory(config.data.directory, config.data.files)

    def load_from_directory(
        self,
        data_dir: Path,
        files: DataFilesConfig | None = None,
    ) -> None:
        """Bootstrap engine from CSV files."""
        file_names = files or DEFAULT_DATA_FILES
        logger.info("Starting data load from directory: %s", data_dir.resolve())
        opening = load_opening_positions(data_dir / file_names.opening_positions)
        for (trader, venue, venue_account, symbol), (qty, avg) in opening.items():
            key = PositionKey(trader, venue, venue_account, symbol)
            state = self._get_or_create_position(key)
            apply_opening_position(state, qty, avg)
            logger.info(
                "Applied opening position: trader=%s venue=%s account=%s symbol=%s "
                "quantity=%s avg_entry=%s",
                trader,
                venue,
                venue_account,
                symbol,
                qty,
                avg,
            )

        prices = load_prices(data_dir / file_names.prices)
        loaded_prices = 0
        for price in prices:
            if self._price_store.upsert(price):
                loaded_prices += 1
                logger.debug(
                    "Price stored: symbol=%s ts=%s price=%s",
                    price.symbol,
                    price.timestamp,
                    price.price,
                )
        logger.info("Stored %d price ticks", loaded_prices)

        trades = load_trades(data_dir / file_names.trades)
        funding = load_funding(data_dir / file_names.funding)

        merged: list[ProcessableEvent] = []
        merged.extend(trades)
        merged.extend(funding)
        merged.sort(key=self._event_sort_key)
        logger.info("Processing %d trade and funding events in chronological order", len(merged))

        processed = 0
        skipped = 0
        for event in merged:
            if self.process(event):
                processed += 1
            else:
                skipped += 1

        logger.info(
            "Data load complete: %d events applied, %d duplicate no-ops, "
            "%d account partitions tracked",
            processed,
            skipped,
            len(self._positions),
        )

    def process(self, event: ProcessableEvent) -> bool:
        """
        Process one event incrementally.

        Returns True if state changed, False if duplicate no-op.
        Raises LateEventError or ConflictingEventError on policy violations.
        """
        if isinstance(event, TradeEvent):
            return self._process_trade(event)
        if isinstance(event, FundingEvent):
            return self._process_funding(event)
        if isinstance(event, PriceEvent):
            return self._process_price(event)
        raise TypeError(f"Unsupported event type: {type(event)}")

    def report(
        self, as_of: datetime | None = None, trader: str | None = None
    ) -> list[SymbolReport]:
        valuation_ts = as_of or PERIOD_END
        logger.info(
            "Generating PnL report as_of=%s trader_filter=%s",
            valuation_ts.isoformat(),
            trader or "ALL",
        )
        aggregated: dict[ReportKey, SymbolReport] = {}

        report_keys: set[ReportKey] = set()
        for key in self._positions:
            report_keys.add(ReportKey(key.trader, key.symbol))
        for key in self._funding:
            report_keys.add(key)

        if trader is not None:
            report_keys = {k for k in report_keys if k.trader == trader}

        for report_key in sorted(report_keys, key=lambda k: (k.trader, k.symbol)):
            final_qty = Decimal("0")
            realized = Decimal("0")
            fees = Decimal("0")
            unrealized_components: list[Decimal | None] = []

            for pos_key, state in self._positions.items():
                if pos_key.trader != report_key.trader or pos_key.symbol != report_key.symbol:
                    continue
                final_qty += state.quantity
                realized += state.realized_pnl
                fees += state.fees_usdt

                mark = self._price_store.get_price_at_or_before(
                    report_key.symbol, valuation_ts
                )
                unrealized_components.append(compute_unrealized(state, mark))

            funding = self._funding.get(report_key, Decimal("0"))
            mark_price = self._price_store.get_price_at_or_before(
                report_key.symbol, valuation_ts
            )

            unrealized: Decimal | None
            if final_qty == 0:
                unrealized = Decimal("0")
            elif any(u is None for u in unrealized_components):
                unrealized = None
            else:
                unrealized = sum(
                    (u for u in unrealized_components if u is not None),
                    Decimal("0"),
                )

            total: Decimal | None
            if unrealized is None:
                total = None
            else:
                total = realized + unrealized + funding - fees

            aggregated[report_key] = SymbolReport(
                trader=report_key.trader,
                symbol=report_key.symbol,
                final_quantity=final_qty,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                funding_pnl=funding,
                fees=fees,
                total_pnl=total,
                mark_price=mark_price,
            )
            logger.info(
                "Report row: trader=%s symbol=%s qty=%s realized=%s unrealized=%s "
                "funding=%s fees=%s total=%s mark=%s",
                report_key.trader,
                report_key.symbol,
                final_qty,
                realized,
                unrealized if unrealized is not None else "unavailable",
                funding,
                fees,
                total if total is not None else "unavailable",
                mark_price if mark_price is not None else "unavailable",
            )

        logger.info("Report generated with %d trader/symbol rows", len(aggregated))
        return list(aggregated.values())

    def format_report(
        self, as_of: datetime | None = None, trader: str | None = None
    ) -> str:
        reports = self.report(as_of=as_of, trader=trader)
        blocks = [r.format_report() for r in reports]
        return "\n\n".join(blocks)

    def _process_trade(self, trade: TradeEvent) -> bool:
        identity = trade.identity
        if identity in self._processed_trades:
            existing = self._processed_trades[identity]
            if self._same_trade(existing, trade):
                logger.info(
                    "Duplicate trade ignored (no-op): venue=%s trade_id=%s ts=%s",
                    trade.venue,
                    trade.trade_id,
                    trade.timestamp,
                )
                return False
            logger.error(
                "Conflicting trade correction rejected: venue=%s trade_id=%s",
                trade.venue,
                trade.trade_id,
            )
            raise ConflictingEventError(
                f"Conflicting trade correction for {identity}"
            )

        key = trade.position_key
        state = self._get_or_create_position(key)
        try:
            check_late_event(state, trade.timestamp)
        except LateEventError:
            logger.error(
                "Late trade rejected: venue=%s trade_id=%s ts=%s partition_last=%s",
                trade.venue,
                trade.trade_id,
                trade.timestamp,
                state.last_event_timestamp,
            )
            raise

        qty_before = state.quantity
        realized_before = state.realized_pnl
        fee_usdt = self._convert_fee_to_usdt(trade)
        apply_trade(state, trade)
        state.fees_usdt += fee_usdt
        mark_processed(state, trade.timestamp)

        self._processed_trades[identity] = trade
        if (
            self._global_last_trade_ts is None
            or trade.timestamp >= self._global_last_trade_ts
        ):
            self._global_last_trade_ts = trade.timestamp

        logger.info(
            "Trade processed: venue=%s trade_id=%s trader=%s symbol=%s side=%s "
            "qty=%s price=%s fee=%s %s fee_usdt=%s | position %s -> %s "
            "realized_pnl %s -> %s",
            trade.venue,
            trade.trade_id,
            trade.trader,
            trade.symbol,
            trade.side.value,
            trade.quantity,
            trade.price,
            trade.fee,
            trade.fee_asset,
            fee_usdt,
            qty_before,
            state.quantity,
            realized_before,
            state.realized_pnl,
        )
        return True

    def _process_funding(self, funding: FundingEvent) -> bool:
        identity = funding.identity
        if identity in self._processed_funding:
            existing = self._processed_funding[identity]
            if self._same_funding(existing, funding):
                return False
            raise ConflictingEventError(
                f"Conflicting funding correction for {identity}"
            )

        report_key = funding.report_key
        current = self._funding.get(report_key, Decimal("0"))
        self._funding[report_key] = current + funding.amount
        self._processed_funding[identity] = funding

        if (
            self._global_last_funding_ts is None
            or funding.timestamp >= self._global_last_funding_ts
        ):
            self._global_last_funding_ts = funding.timestamp
        return True

    def _process_price(self, price: PriceEvent) -> bool:
        return self._price_store.upsert(price)

    def _convert_fee_to_usdt(self, trade: TradeEvent) -> Decimal:
        if trade.fee_asset == "USDT":
            return trade.fee
        conversion_symbol = f"{trade.fee_asset}USDT"
        rate = self._price_store.get_price_at_or_before(
            conversion_symbol, trade.timestamp
        )
        if rate is None:
            raise ValueError(
                f"No eligible {conversion_symbol} price at or before "
                f"{trade.timestamp} for fee conversion"
            )
        return trade.fee * rate

    def _get_or_create_position(self, key: PositionKey) -> AccountPositionState:
        if key not in self._positions:
            self._positions[key] = AccountPositionState()
        return self._positions[key]

    @staticmethod
    def _event_sort_key(event: ProcessableEvent) -> tuple:
        if isinstance(event, TradeEvent):
            kind = 0
            tie = event.trade_id
        elif isinstance(event, FundingEvent):
            kind = 1
            tie = event.event_id
        else:
            kind = 2
            tie = event.symbol
        return (event.timestamp, kind, tie)

    @staticmethod
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

    @staticmethod
    def _same_funding(a: FundingEvent, b: FundingEvent) -> bool:
        return (
            a.timestamp == b.timestamp
            and a.trader == b.trader
            and a.venue_account == b.venue_account
            and a.symbol == b.symbol
            and a.asset == b.asset
            and a.amount == b.amount
        )
