# Detailed Summary — Real-Time PnL Engine Implementation

This document records the full reasoning process for building the Python PnL engine from the Coalesce Partners coding challenge. It is intended as a transparent audit trail of requirements gathering, design choices, implementation steps, and validation.

---

## 1. Requirements Gathering

### 1.1 Source Documents

Two documents define the exercise:

1. **Challenge.pdf** — primary specification (Part 1: exercise, Part 2: edge-case reference)
2. **README.md** — dataset layout and calculation window summary

Four CSV files provide input data:

| File | Purpose |
|------|---------|
| `opening_positions.csv` | Snapshot at 2026-08-01 00:00 UTC before any events |
| `trades.csv` | Futures trades (possibly out of order, with duplicates) |
| `funding.csv` | Perpetual funding payments |
| `prices.csv` | Mark prices for instruments + BNBUSDT |

### 1.2 Core Functional Requirements

1. **Maintain position state** per `(trader, venue, venue_account, symbol)`.
2. **Report aggregated PnL** per `(trader, symbol)`.
3. **Calculate components:**
   - Realized PnL (on position reduction/close)
   - Unrealized PnL (open position × mark price)
   - Funding PnL (sum of funding events)
   - Fees (USDT value, including BNB conversion)
   - Total PnL = Realized + Unrealized + Funding − Fees
4. **Incremental engine** with `process(event)` for live updates.
5. **Initial load:** chronological processing, deduplication.
6. **Late events:** detect and reject/defer — never silently misapply.
7. **Duplicates:** exact repeat = no-op.
8. **Missing prices:** report unavailable, not zero.

### 1.3 Non-Functional Requirements

- Use `Decimal` or equivalent; no intermediate rounding.
- Display rounding only (±0.01 USDT tolerance for totals).
- CLI or simple API — no frontend.
- Tests and README required.
- Production scaling discussion in README.

### 1.4 Priority Order (when time-constrained)

1. Correct end-of-period PnL ← **primary focus**
2. Incremental processing + duplicate/late handling ← **implemented**
3. As-of views, bounded replay ← **documented as future work**

---

## 2. Data Analysis

### 2.1 Opening Positions

14 rows covering 4 traders (A–D), multiple venues (BINANCE, BYBIT, OKX), and symbols (BTC, ETH, SOL, XRP). Mix of long (positive quantity) and short (negative quantity) positions.

Example:
- TRADER_A / BINANCE / XRPUSDT: -5000 (short)
- TRADER_C / OKX / BTCUSDT: +0.625 (long)

Each row provides `avg_entry_price` — treated as a single WAC lot per partition.

### 2.2 Trades

~759 rows (including header), **not in chronological order**. Notable data quality issues deliberately present:

- **Duplicates:** e.g. `T00731` appears twice (lines 37 and 96), `T00432` twice (lines 582 and 594), `T00463` twice (lines 593 and 652), `T00512` (lines 61 and 331), `T00499` (lines 115 and 432), `T00576` (lines 152 and 547), `T00411` (lines 362 and 709), `T00422` (lines 68 and 736), `T00654` (lines 219 and 466), `T00732` (lines 132 and 694).
- **BNB fees:** Many trades use `fee_asset=BNB` requiring conversion via `BNBUSDT`.
- **Position changes:** Buys, sells, increases, reductions, and flips through zero.
- **Multiple accounts per trader per symbol:** Positions must stay separate until report aggregation.

### 2.3 Funding

31 rows with duplicate funding events (e.g. `F0027`, `F0014`, `F0023` appear twice). All amounts in USDT. Positive = received, negative = paid.

### 2.4 Prices

244 mark-price ticks across BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, BNBUSDT. Includes closing marks at `2026-08-02T00:00:00Z` for all traded symbols. Multiple ticks per symbol per timestamp window (30-minute intervals plus some finer granularity).

---

## 3. Architecture Design

### 3.1 Module Structure

```
pnl_engine/
├── __init__.py      # Public API exports
├── models.py        # Events, keys, reports, exceptions
├── price_store.py   # As-of price lookup
├── position.py      # WAC logic, unrealized helper
├── loader.py        # CSV I/O, dedup, window filter
└── engine.py        # Orchestration, process(), report()

main.py              # CLI
tests/test_engine.py # Unit + integration tests
```

**Rationale:** Each module has a single responsibility. The engine orchestrates; it does not embed CSV parsing or cost-basis math inline. This mirrors how a production system would separate ingestion, pricing, position ledger, and reporting.

### 3.2 Event Model

Three frozen dataclass event types:

- `TradeEvent` — identity: `(venue, trade_id)`
- `FundingEvent` — identity: `(venue, event_id)`
- `PriceEvent` — identity: `(symbol, timestamp)`

Using dataclasses keeps events hashable/comparable for deduplication tests and makes the `process()` dispatch type-safe.

### 3.3 State Model

**Per-partition state (`AccountPositionState`):**
- `quantity` — signed (positive long, negative short)
- `avg_entry_price` — WAC for remaining position
- `realized_pnl` — cumulative realized
- `fees_usdt` — cumulative fees in USDT
- `last_event_timestamp` — for late-event detection

**Global state:**
- `_funding: dict[ReportKey, Decimal]` — aggregated funding per trader/symbol
- `_price_store: PriceStore` — all mark prices
- `_processed_trades / _processed_funding` — idempotency registries

---

## 4. Cost-Basis Decision: Weighted Average Cost

### 4.1 Options Considered

| Method | Pros | Cons |
|--------|------|------|
| **FIFO** | Tax-standard, intuitive lots | Needs lot queue; opening snapshot is avg not lots |
| **WAC** | Matches opening snapshot; O(1) updates | Differs from FIFO in some flip scenarios |
| **LIFO** | Rare in practice | Non-standard for this use case |

### 4.2 Choice: WAC

Selected **Weighted Average Cost** because:

1. Opening positions provide `avg_entry_price` — naturally a WAC figure.
2. Challenge says "you may choose an appropriate methodology" — WAC is standard for futures margin accounts.
3. Implementation is compact and testable.
4. Realized PnL on reduction is unambiguous: closed quantity × (exit − entry).

### 4.3 WAC Algorithm (Detailed)

**Opening load:**
```
state.quantity = opening.quantity
state.avg_entry_price = opening.avg_entry_price
```

**Trade with signed_qty (+ for BUY, − for SELL):**

```
if quantity == 0:
    quantity = signed_qty; avg = price; return

if same_sign(quantity, signed_qty):  # adding
    new_qty = quantity + signed_qty
    avg = (quantity * avg + signed_qty * price) / new_qty
    quantity = new_qty

else:  # reducing or flipping
    closed = min(|quantity|, |signed_qty|)
    if quantity > 0:  realized += (price - avg) * closed
    else:             realized += (avg - price) * closed

    remaining = quantity + signed_qty
    if remaining == 0: quantity = 0; avg = 0
    elif same_sign(quantity, remaining): quantity = remaining  # partial close
    else: quantity = remaining; avg = price  # flip
```

**Unrealized:**
```
if quantity == 0: 0
else: (mark - avg) * quantity
```

For a short of −10 at avg 100 with mark 90:
`(90 - 100) * (-10) = +100` ✓ (profitable short)

---

## 5. Price Lookup Design

### 5.1 Requirements

- Latest price **at or before** query timestamp (inclusive).
- Never use future prices.
- Price at exactly `2026-08-02T00:00:00Z` valid for end-of-period mark.
- Prices before period start valid if they're the latest available.

### 5.2 Implementation

`PriceStore` maintains per symbol a sorted list of `(timestamp, price)` tuples. Lookup uses `bisect_right` to find the last tick ≤ as_of in O(log n).

On insert:
- Same `(symbol, timestamp)` with same price → skip (dedup).
- Same key with different price → `ConflictingEventError`.

This satisfies the spec's price correction policy.

---

## 6. Fee Conversion

For each trade:
```
if fee_asset == "USDT":
    fee_usdt = fee
else:
    rate = price_store.get(BNBUSDT, at_or_before=trade.timestamp)
    if rate is None: raise error (unavailable policy)
    fee_usdt = fee * rate
```

All BNB fees in the dataset have eligible BNBUSDT prices before their trade timestamps (verified implicitly by successful full-dataset run).

---

## 7. Incremental Processing & Event Ordering

### 7.1 Initial Load Sequence

1. Load opening positions into partition state.
2. Load all prices into PriceStore (no window filter — reference data).
3. Load trades and funding, filter to `[start, end)`, deduplicate.
4. Merge and sort by `(timestamp, event_kind, tie_breaker)`.
5. Call `process()` for each event.

Trades sort before funding at the same timestamp (kind=0 vs kind=1) for deterministic ordering.

### 7.2 Live `process(event)` Behavior

| Event | Changed? | Notes |
|-------|----------|-------|
| Duplicate trade/funding | `False` | Full field comparison |
| Conflicting correction | Exception | Same ID, different payload |
| Late trade | Exception | `timestamp < last_event_timestamp` for partition |
| New trade | `True` | Updates position + fees |
| New funding | `True` | Adds to funding aggregate |
| New/changed price | `True/False` | Upsert into price store |

### 7.3 Late Event Policy

**Trades:** Strict rejection via `LateEventError`. The partition tracks `last_event_timestamp` updated after each processed trade.

**Funding:** Not checked for lateness against partition timestamps. Spec notes funding can be incorporated after idempotency checks since amount is self-contained. Funding duplicates are still deduplicated.

**Prices:** Always accepted (reference data). A late price could invalidate cached valuations in a production system; here unrealized is computed on demand at report time, so late prices automatically take effect.

### 7.4 Why Not Bounded Replay (Stretch)?

Bounded replay requires per-partition event logs and checkpoint/recompute logic — valuable but time-intensive. Documented as the next production step. Current policy (reject late trades) satisfies the **minimum required behavior**.

---

## 8. Report Aggregation

For each `(trader, symbol)`:

```
final_quantity = Σ partition.quantity
realized_pnl   = Σ partition.realized_pnl
fees           = Σ partition.fees_usdt
funding_pnl    = funding[trader, symbol]  (or 0)

mark = price_store.get(symbol, PERIOD_END)
unrealized = Σ partition_unrealized(mark)
  - if any partition has qty ≠ 0 and mark is None → unavailable

total = realized + unrealized + funding - fees
  - if unrealized unavailable → total unavailable
```

Partitions with zero quantity contribute 0 to unrealized (not unavailable).

---

## 9. Implementation Steps (Chronological)

1. **Read Challenge.pdf and README** — extracted all rules, priorities, and edge cases.
2. **Inspect CSV files** — confirmed duplicates, BNB fees, multi-trader/account complexity.
3. **Define models** — events, keys, reports, exceptions.
4. **Build PriceStore** — bisect-based as-of lookup with conflict detection.
5. **Implement WAC position logic** — separate pure functions for testability.
6. **Build CSV loader** — parse, filter window, deduplicate on load.
7. **Implement PnLEngine** — wire opening → prices → events → report.
8. **Add CLI** — `main.py` with `--data-dir` and `--trader` filters.
9. **Write tests** — unit tests for each component + full dataset integration.
10. **Run validation** — 14/14 tests pass; full dataset loads and reconciles.

---

## 10. Test Strategy

| Test | What it validates |
|------|-------------------|
| `test_as_of_lookup_inclusive` | Same-timestamp price preferred |
| `test_no_future_price` | No lookahead |
| `test_long_reduction_realized_pnl` | WAC realized on sell |
| `test_short_position_unrealized` | Signed quantity unrealized |
| `test_bnb_fee_conversion` | BNB → USDT at as-of rate |
| `test_duplicate_trade_is_noop` | Idempotency |
| `test_late_trade_rejected` | LateEventError |
| `test_conflicting_trade_rejected` | ConflictingEventError |
| `test_loads_without_error` | Full CSV integration |
| `test_all_traders_present` | A, B, C, D in report |
| `test_total_pnl_reconciles` | Accounting identity |
| `test_end_of_period_marks_available` | Closing marks exist |
| `test_deduplication_in_dataset` | Real duplicate T00731 |
| `test_funding_dedup_in_dataset` | Real duplicate F0027 |

---

## 11. Sample Output (TRADER_A, End of Period)

```
TRADER_A / BTCUSDT  — Final Qty: 2.125,  Total PnL: +34.93
TRADER_A / ETHUSDT  — Final Qty: 5.5,    Total PnL: -18.57
TRADER_A / SOLUSDT  — Final Qty: -20,     Total PnL: -72.05
TRADER_A / XRPUSDT  — Final Qty: 4750,    Total PnL: +207.60
```

All components reconcile: `Total = Realized + Unrealized + Funding − Fees`.

---

## 12. Production Scaling Reflection

### What to change first

1. **Event sourcing per partition** — append-only log + periodic snapshots for recovery.
2. **External price feed service** — decouple mark ingestion from PnL compute.
3. **Late event replay** — bounded recompute instead of reject, for exchanges with delayed fills.

### What to keep simple

- WAC cost basis (unless regulatory requirement for FIFO).
- Synchronous partition updates (parallelize across partitions, not within).
- Report-on-demand (not continuous push) until latency requirements demand streaming.

### Reconciliation

- Nightly pull of exchange position snapshots via API.
- Compare `final_quantity` per partition; alert on |diff| > ε.
- Rebuild from event log if drift persists.

### Scale estimates (from spec)

100 traders × 10 exchanges × ~500 positions ≈ 50K partitions. At millions of events/day, partition sharding + checkpoint every N events keeps replay bounded. Price store fits in memory (~5 symbols × 48 ticks/day is trivial); at scale, use Redis ZSET or TimescaleDB.

---

## 13. Known Limitations & Future Work

1. **No bounded replay** — late trades rejected, not replayed.
2. **No as-of API endpoint** — `report(as_of=...)` exists but not exposed via HTTP.
3. **Funding late events** — not partition-guarded (by design per spec guidance).
4. **No golden reference comparison** — internal reconciliation only; no official expected values provided in dataset.
5. **Single-threaded** — adequate for challenge scope.

---

## 14. Files Delivered

| File | Description |
|------|-------------|
| `pnl_engine/` | Core package (6 modules) |
| `main.py` | CLI entry point |
| `tests/test_engine.py` | 14 automated tests |
| `requirements.txt` | pytest dependency |
| `README.md` | Run instructions, methodology, assumptions |
| `SUMMARY.md` | This document |

Original challenge files (`Challenge.pdf`, CSVs, original README) are unchanged.

---

## 15. Conclusion

The implementation satisfies the challenge's priority 1 and 2 requirements:

- **Correct end-of-period PnL** with WAC, multi-account aggregation, BNB fee conversion, funding, long/short/flip handling, deduplication, and chronological load.
- **Incremental processing** via `engine.process(event)` with idempotent duplicates and explicit late-event rejection.
- **Priority 3** (as-of views beyond report API, bounded replay) is designed but not implemented — documented with clear trade-offs.

The codebase prioritizes clarity, testability, and financial correctness over premature optimization, while the README and this summary provide the reasoning expected for a senior backend evaluation.
