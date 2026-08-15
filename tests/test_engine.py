"""Tests for the PnL engine."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pnl_engine.engine import PnLEngine
from pnl_engine.loader import PERIOD_END, parse_timestamp
from pnl_engine.models import (
    ConflictingEventError,
    FundingEvent,
    LateEventError,
    PriceEvent,
    Side,
    TradeEvent,
)

DATA_DIR = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def _trade(**kwargs) -> TradeEvent:
    defaults = dict(
        timestamp=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
        venue="BINANCE",
        trade_id="T99999",
        trader="TRADER_A",
        venue_account="TRADER_A_BINANCE",
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("60000"),
        fee=Decimal("10"),
        fee_asset="USDT",
    )
    defaults.update(kwargs)
    return TradeEvent(**defaults)


class TestPriceStore:
    def test_as_of_lookup_inclusive(self):
        engine = PnLEngine()
        ts = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        engine.process(
            PriceEvent(datetime(2026, 8, 1, 9, 0, tzinfo=UTC), "BTCUSDT", Decimal("100"))
        )
        engine.process(PriceEvent(ts, "BTCUSDT", Decimal("105")))

        assert engine.price_store.get_price_at_or_before("BTCUSDT", ts) == Decimal("105")
        assert engine.price_store.get_price_at_or_before(
            "BTCUSDT", datetime(2026, 8, 1, 9, 30, tzinfo=UTC)
        ) == Decimal("100")

    def test_no_future_price(self):
        engine = PnLEngine()
        engine.process(
            PriceEvent(datetime(2026, 8, 1, 12, 0, tzinfo=UTC), "BTCUSDT", Decimal("100"))
        )
        assert (
            engine.price_store.get_price_at_or_before(
                "BTCUSDT", datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
            )
            is None
        )


class TestPositionLogic:
    def test_long_reduction_realized_pnl(self):
        engine = PnLEngine()
        engine.process(
            PriceEvent(datetime(2026, 8, 1, 0, 0, tzinfo=UTC), "BTCUSDT", Decimal("60000"))
        )
        engine.process(
            _trade(
                trade_id="T1",
                side=Side.BUY,
                quantity=Decimal("2"),
                price=Decimal("60000"),
                timestamp=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
            )
        )
        engine.process(
            _trade(
                trade_id="T2",
                side=Side.SELL,
                quantity=Decimal("0.5"),
                price=Decimal("61000"),
                timestamp=datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
            )
        )
        report = engine.report()[0]
        assert report.final_quantity == Decimal("1.5")
        assert report.realized_pnl == Decimal("500")  # (61000-60000)*0.5

    def test_short_position_unrealized(self):
        engine = PnLEngine()
        engine.process(
            PriceEvent(datetime(2026, 8, 1, 0, 0, tzinfo=UTC), "BTCUSDT", Decimal("60000"))
        )
        engine.process(
            PriceEvent(datetime(2026, 8, 2, 0, 0, tzinfo=UTC), "BTCUSDT", Decimal("59000"))
        )
        engine.process(
            _trade(
                trade_id="T1",
                side=Side.SELL,
                quantity=Decimal("1"),
                price=Decimal("60000"),
            )
        )
        report = engine.report()[0]
        assert report.final_quantity == Decimal("-1")
        assert report.unrealized_pnl == Decimal("1000")  # (59000-60000)*-1

    def test_bnb_fee_conversion(self):
        engine = PnLEngine()
        engine.process(
            PriceEvent(datetime(2026, 8, 1, 0, 0, tzinfo=UTC), "BNBUSDT", Decimal("800"))
        )
        engine.process(
            _trade(
                trade_id="T1",
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("60000"),
                fee=Decimal("0.01"),
                fee_asset="BNB",
                timestamp=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
            )
        )
        report = engine.report()[0]
        assert report.fees == Decimal("8")  # 0.01 * 800


class TestIncrementalProcessing:
    def test_duplicate_trade_is_noop(self):
        engine = PnLEngine()
        engine.process(
            PriceEvent(datetime(2026, 8, 1, 0, 0, tzinfo=UTC), "BTCUSDT", Decimal("60000"))
        )
        trade = _trade(trade_id="T1")
        assert engine.process(trade) is True
        assert engine.process(trade) is False
        report = engine.report()[0]
        assert report.final_quantity == Decimal("1")

    def test_late_trade_rejected(self):
        engine = PnLEngine()
        engine.process(
            PriceEvent(datetime(2026, 8, 1, 0, 0, tzinfo=UTC), "BTCUSDT", Decimal("60000"))
        )
        engine.process(
            _trade(
                trade_id="T1",
                timestamp=datetime(2026, 8, 1, 3, 0, tzinfo=UTC),
            )
        )
        with pytest.raises(LateEventError):
            engine.process(
                _trade(
                    trade_id="T2",
                    timestamp=datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
                )
            )

    def test_conflicting_trade_rejected(self):
        engine = PnLEngine()
        engine.process(
            PriceEvent(datetime(2026, 8, 1, 0, 0, tzinfo=UTC), "BTCUSDT", Decimal("60000"))
        )
        engine.process(_trade(trade_id="T1", quantity=Decimal("1")))
        with pytest.raises(ConflictingEventError):
            engine.process(_trade(trade_id="T1", quantity=Decimal("2")))


class TestFullDataset:
    @pytest.fixture
    def engine(self) -> PnLEngine:
        eng = PnLEngine()
        eng.load_from_directory(DATA_DIR)
        return eng

    def test_loads_without_error(self, engine: PnLEngine):
        reports = engine.report()
        assert len(reports) > 0

    def test_all_traders_present(self, engine: PnLEngine):
        traders = {r.trader for r in engine.report()}
        assert traders == {"TRADER_A", "TRADER_B", "TRADER_C", "TRADER_D"}

    def test_total_pnl_reconciles(self, engine: PnLEngine):
        for r in engine.report():
            if r.total_pnl is not None:
                expected = r.realized_pnl + r.unrealized_pnl + r.funding_pnl - r.fees
                assert abs(r.total_pnl - expected) < Decimal("1e-10")

    def test_end_of_period_marks_available(self, engine: PnLEngine):
        for r in engine.report(as_of=PERIOD_END):
            assert r.unrealized_pnl is not None
            assert r.total_pnl is not None
            assert r.mark_price is not None

    def test_deduplication_in_dataset(self, engine: PnLEngine):
        """Dataset contains duplicate rows; engine should dedupe on load."""
        reports_before = engine.report()
        # Re-processing a known duplicate trade from CSV should be no-op.
        dup = _trade(
            trade_id="T00731",
            venue="BINANCE",
            trader="TRADER_A",
            venue_account="TRADER_A_BINANCE",
            symbol="XRPUSDT",
            side=Side.BUY,
            quantity=Decimal("1500"),
            price=Decimal("0.62691"),
            fee=Decimal("0.00036216"),
            fee_asset="BNB",
            timestamp=parse_timestamp("2026-08-01T15:03:35Z"),
        )
        assert engine.process(dup) is False
        assert engine.report() == reports_before

    def test_funding_dedup_in_dataset(self, engine: PnLEngine):
        dup = FundingEvent(
            timestamp=parse_timestamp("2026-08-01T16:00:00Z"),
            event_id="F0027",
            trader="TRADER_D",
            venue="BYBIT",
            venue_account="TRADER_D_BYBIT",
            symbol="XRPUSDT",
            asset="USDT",
            amount=Decimal("0.04850806"),
        )
        assert engine.process(dup) is False
