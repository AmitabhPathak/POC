# Real-Time PnL Engine

Python implementation of the Coalesce Partners coding challenge: a simplified real-time PnL engine that maintains position state and calculates profit/loss from trading, funding, and mark-price events.

## Project layout

```
├── config.yaml          # Data paths, logging, and result settings
├── data/                # Input CSV files
│   ├── opening_positions.csv
│   ├── trades.csv
│   ├── funding.csv
│   └── prices.csv
├── result/              # Generated PnL report output
├── logs/                # Engine log file
├── main.py              # CLI entry point
├── pnl_engine/          # Core package
└── tests/
```

## Quick Start

```bash
pip install -r requirements.txt
python main.py
python main.py --trader TRADER_B
python main.py --config /path/to/config.yaml
python -m pytest tests/ -v
```

Configuration lives in `config.yaml`:

- **data** — input directory and CSV file names
- **logging** — logger name, log file path, level, console flag
- **results** — output directory, target trader (`null` = all traders), output file name

CLI `--trader` overrides `results.trader`. The report is printed to stdout and written to `result/` (default: `result/pnl_report.txt`).

## Approach

The solution is organized as a small Python package (`pnl_engine/`) with clear separation of concerns:

| Module | Responsibility |
|--------|----------------|
| `config.py` | Load `config.yaml` (paths, logging, results) |
| `models.py` | Typed events, position keys, report dataclasses |
| `price_store.py` | Mark-price series with as-of lookup |
| `position.py` | Weighted-average cost basis and realized PnL |
| `loader.py` | CSV parsing, window filtering, deduplication |
| `engine.py` | Incremental `process(event)` and report aggregation |
| `main.py` | CLI entry point |

All monetary calculations use Python `Decimal` to avoid floating-point drift. Intermediate values are never rounded; only displayed output is rounded to two decimal places.

## Cost-Basis Methodology

**Weighted Average Cost (WAC)** is used at the account-partition level.

### Why WAC?

- Matches how many futures platforms report average entry price.
- Opening positions already supply a single `avg_entry_price`, which maps naturally to one WAC lot.
- Simpler and less storage-heavy than FIFO for high trade volume, while still producing correct realized PnL on reductions.
- Deterministic and easy to explain in an interview.

### Rules

1. **Same-direction trade (add):**  
   `new_avg = (old_qty × old_avg + signed_qty × price) / new_qty`

2. **Opposite-direction trade (reduce):**  
   Realized PnL += `(price - avg) × closed_qty` for long reductions  
   Realized PnL += `(avg - price) × closed_qty` for short reductions  
   Average entry is unchanged on partial reduction.

3. **Position flip (cross zero):**  
   Close the entire old side at the current average, then open the remainder at the trade price.

4. **Unrealized PnL:**  
   `(mark_price - avg_entry) × quantity` — works for both long and short because quantity is signed.

## Key Assumptions

- **Position grain:** `(trader, venue, venue_account, symbol)` — never netted before PnL calculation.
- **Report grain:** `(trader, symbol)` — sums quantity and PnL components across accounts.
- **Calculation window:** Trades and funding use half-open `[2026-08-01 00:00 UTC, 2026-08-02 00:00 UTC)`. Prices are reference data outside this filter; the tick at exactly period end is valid as the closing mark.
- **Fee conversion:** Non-USDT fees convert via latest `FEE_ASSETUSDT` price at or before trade time (e.g. BNB → BNBUSDT).
- **Funding:** Summed per `(trader, symbol)`; all funding in the dataset is USDT.
- **Identity keys:** `(venue, trade_id)` for trades, `(venue, event_id)` for funding, `(symbol, timestamp)` for prices.

## Edge Cases Handled

| Case | Policy |
|------|--------|
| Duplicate event (identical content) | No-op, returns `False` from `process()` |
| Conflicting correction (same ID, different content) | `ConflictingEventError` |
| Late trade (timestamp before partition's last event) | `LateEventError` |
| Missing mark price at valuation time | Unrealized and Total PnL reported as `unavailable` |
| Out-of-order CSV rows | Sorted by timestamp on initial load |
| Long, short, and flip-through-zero | WAC logic handles all three |
| Multi-asset fees (BNB) | Converted using as-of BNBUSDT price |

## Incremental Processing

```python
from pnl_engine import PnLEngine, load_config

config = load_config()
engine = PnLEngine()
engine.load_from_config(config)

changed = engine.process(new_trade)      # in-order live event
report = engine.report(trader=config.results.trader)
```

- Initial load deduplicates and processes events in timestamp order (trades before funding at the same timestamp).
- Exact duplicate re-delivery is idempotent.
- Late trades are rejected explicitly rather than misapplied in arrival order.

## Production & Scaling Notes

**Keep simple (for now):** WAC cost basis, in-memory state, synchronous `process()` API, global price store per symbol.

**Change first at scale:**

1. **Partitioned state** — shard by `(trader, venue, venue_account, symbol)` with per-partition event logs and checkpoints.
2. **Durable event store** — append-only log (Kafka/Pulsar) as source of truth; rebuild state from snapshots + replay.
3. **Price service** — dedicated time-series store (Redis sorted sets or TSDB) for as-of lookups at high tick rates.
4. **Bounded replay** — for late events, replay only the affected partition from last checkpoint (stretch goal documented, not implemented here).
5. **Reconciliation** — periodic diff of engine positions vs exchange REST/WebSocket snapshots; flag drift above tolerance.

**State recovery:** Snapshot positions + last processed event offset per partition every N minutes; on failure, restore snapshot and replay from offset.

## Trade-offs

| Choice | Benefit | Cost |
|--------|---------|------|
| WAC vs FIFO | Simpler state, matches opening snapshot | May differ from tax-lot FIFO reporting |
| Reject late events vs replay | Correctness without full recompute | Requires upstream ordering or replay pipeline |
| In-memory vs database | Fast to build and test | Not durable without added infrastructure |
| Decimal vs float | Financial precision | Slightly slower than float |

## Tests

14 tests cover price lookup, long/short PnL, BNB fee conversion, duplicate/late/conflicting events, and full-dataset integration including reconciliation of `Total = Realized + Unrealized + Funding − Fees`.

## Further Improvements (with more time)

- Bounded partition replay for late trades
- As-of report API at arbitrary timestamps after any event
- Property-based tests for WAC invariants
- Async event ingestion and back-pressure
- Reference output comparison harness if golden files are provided

See **SUMMARY.md** for a detailed walkthrough of requirements analysis, design decisions, and implementation reasoning.
