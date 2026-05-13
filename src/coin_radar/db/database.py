from __future__ import annotations

from pathlib import Path
from types import TracebackType

import aiosqlite

from coin_radar.db.repository import (
    ContractRepo,
    CooldownRepo,
    MarketDataRepo,
    SignalRepo,
)

_DB_DIR = Path.home() / ".coin_radar"

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS market_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    quote_volume REAL,
    funding_rate REAL,
    open_interest REAL,
    cvd REAL,
    long_short_ratio REAL,
    top_trader_long_short_ratio REAL,
    bid_depth REAL,
    ask_depth REAL,
    perp_volume REAL,
    spot_volume REAL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    module TEXT NOT NULL,
    score REAL,
    priority TEXT,
    details TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cooldowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    module TEXT NOT NULL,
    cooldown_until INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS known_contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    detected_at INTEGER NOT NULL
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_market_data_se_ts
    ON market_data (symbol, exchange, timestamp);

CREATE INDEX IF NOT EXISTS idx_signals_sym_mod_ca
    ON signals (symbol, module, created_at);

CREATE INDEX IF NOT EXISTS idx_cooldowns_sym_mod
    ON cooldowns (symbol, module);
"""


class DatabaseManager:
    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            _DB_DIR.mkdir(parents=True, exist_ok=True)
            db_path = _DB_DIR / "coin_radar.db"
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_CREATE_TABLES)
        await self._conn.executescript(_CREATE_INDEXES)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> DatabaseManager:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("数据库未连接，请先调用 connect()")
        return self._conn

    @property
    def market_data(self) -> MarketDataRepo:
        return MarketDataRepo(self.conn)

    @property
    def signals(self) -> SignalRepo:
        return SignalRepo(self.conn)

    @property
    def cooldowns(self) -> CooldownRepo:
        return CooldownRepo(self.conn)

    @property
    def contracts(self) -> ContractRepo:
        return ContractRepo(self.conn)
