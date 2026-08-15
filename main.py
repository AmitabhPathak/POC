#!/usr/bin/env python3
"""CLI for the Real-Time PnL Engine."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pnl_engine.config import load_config
from pnl_engine.engine import PnLEngine
from pnl_engine.logging_config import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Real-time PnL engine for futures trading data"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (default: config.yaml next to this script)",
    )
    parser.add_argument(
        "--trader",
        type=str,
        default=None,
        help="Override results.trader from config (e.g. TRADER_A)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    data_dir = config.data.directory
    required = [
        config.data.files.opening_positions,
        config.data.files.trades,
        config.data.files.funding,
        config.data.files.prices,
    ]
    for name in required:
        if not (data_dir / name).exists():
            print(f"Missing required file: {data_dir / name}", file=sys.stderr)
            return 1

    setup_logging(
        log_file=config.logging.file,
        level=config.logging.level,
        console=config.logging.console,
        logger_name=config.logging.name,
    )

    trader = args.trader if args.trader is not None else config.results.trader

    engine = PnLEngine()
    engine.load_from_config(config)

    report = engine.format_report(trader=trader)
    print(report)

    output_path = config.results.output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report + "\n", encoding="utf-8")
    print(f"\nReport written to {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
