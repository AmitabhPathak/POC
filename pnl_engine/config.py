"""Load and expose application configuration from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass(frozen=True)
class DataFilesConfig:
    opening_positions: str
    trades: str
    funding: str
    prices: str


@dataclass(frozen=True)
class DataConfig:
    directory: Path
    files: DataFilesConfig

    def path_for(self, key: str) -> Path:
        filename = getattr(self.files, key)
        return self.directory / filename


@dataclass(frozen=True)
class LoggingConfig:
    name: str
    file: Path
    level: str
    console: bool


@dataclass(frozen=True)
class ResultsConfig:
    directory: Path
    trader: str | None
    output_file: str

    @property
    def output_path(self) -> Path:
        return self.directory / self.output_file


@dataclass(frozen=True)
class AppConfig:
    data: DataConfig
    logging: LoggingConfig
    results: ResultsConfig
    root: Path


def _as_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (root / path).resolve()


def _require_mapping(raw: Any, section: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Config section '{section}' must be a mapping")
    return raw


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from YAML. Paths are resolved relative to the config file."""
    config_path = (path or DEFAULT_CONFIG_PATH).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError("Top-level config must be a mapping")

    root = config_path.parent
    data_raw = _require_mapping(raw.get("data"), "data")
    files_raw = _require_mapping(data_raw.get("files"), "data.files")
    logging_raw = _require_mapping(raw.get("logging"), "logging")
    results_raw = _require_mapping(raw.get("results"), "results")

    required_files = ("opening_positions", "trades", "funding", "prices")
    for key in required_files:
        if key not in files_raw:
            raise ValueError(f"Missing data.files.{key} in config")

    trader = results_raw.get("trader")
    if trader is not None:
        trader = str(trader).strip() or None

    return AppConfig(
        root=root,
        data=DataConfig(
            directory=_as_path(root, data_raw.get("directory", "data")),
            files=DataFilesConfig(
                opening_positions=str(files_raw["opening_positions"]),
                trades=str(files_raw["trades"]),
                funding=str(files_raw["funding"]),
                prices=str(files_raw["prices"]),
            ),
        ),
        logging=LoggingConfig(
            name=str(logging_raw.get("name", "pnl_engine")),
            file=_as_path(root, logging_raw.get("file", "logs/pnl_engine.log")),
            level=str(logging_raw.get("level", "INFO")),
            console=bool(logging_raw.get("console", False)),
        ),
        results=ResultsConfig(
            directory=_as_path(root, results_raw.get("directory", "result")),
            trader=trader,
            output_file=str(results_raw.get("output_file", "pnl_report.txt")),
        ),
    )
