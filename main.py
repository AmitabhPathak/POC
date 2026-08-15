#!/usr/bin/env python3
"""CLI for the Real-Time PnL Engine."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pnl_engine.engine import PnLEngine
from pnl_engine.logging_config import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Real-time PnL engine for futures trading data"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("."),
        help="Directory containing CSV data files (default: current directory)",
    )
    parser.add_argument(
        "--trader",
        type=str,
        default=None,
        help="Filter report to a single trader (e.g. TRADER_A)",
    )
    args = parser.parse_args(argv)

    data_dir = args.data_dir.resolve()

    required = [
        "opening_positions.csv",
        "trades.csv",
        "funding.csv",
        "prices.csv",
    ]

    for name in required:
        if not (data_dir / name).exists():
            print(f"Missing required file: {data_dir / name}", file=sys.stderr)
            return 1

    # Configure logging before using the engine.
    setup_logging()

    engine = PnLEngine()
    engine.load_from_directory(data_dir)

    print(engine.format_report(trader=args.trader))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())