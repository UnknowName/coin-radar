from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MarketDataRow:
    symbol: str
    exchange: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    funding_rate: float | None = None
    open_interest: float | None = None
    cvd: float | None = None
    long_short_ratio: float | None = None
    top_trader_long_short_ratio: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None
    perp_volume: float | None = None
    spot_volume: float | None = None
    id: int | None = None


@dataclass
class SignalRow:
    symbol: str
    module: str
    score: float
    priority: str
    details: str
    created_at: int
    id: int | None = None


@dataclass
class CooldownRow:
    symbol: str
    module: str
    cooldown_until: int
    id: int | None = None


@dataclass
class KnownContractRow:
    symbol: str
    exchange: str
    detected_at: int
    id: int | None = None
