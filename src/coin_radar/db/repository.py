from __future__ import annotations

import json
import time
from dataclasses import asdict

import aiosqlite

from coin_radar.db.models import (
    KnownContractRow,
    MarketDataRow,
    SignalRow,
)

_MARKET_DATA_FIELDS = (
    "symbol", "exchange", "timestamp", "open", "high", "low", "close",
    "volume", "quote_volume", "funding_rate", "open_interest", "cvd",
    "long_short_ratio", "top_trader_long_short_ratio", "bid_depth",
    "ask_depth", "perp_volume", "spot_volume",
)


class MarketDataRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, data: MarketDataRow) -> int:
        placeholders = ", ".join(f":{f}" for f in _MARKET_DATA_FIELDS)
        cols = ", ".join(_MARKET_DATA_FIELDS)
        sql = f"INSERT INTO market_data ({cols}) VALUES ({placeholders})"
        params = {f: getattr(data, f) for f in _MARKET_DATA_FIELDS}
        cursor = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cursor.lastrowid

    async def get_recent(
        self, symbol: str, exchange: str, hours: int
    ) -> list[MarketDataRow]:
        cutoff = int(time.time()) - hours * 3600
        cursor = await self._conn.execute(
            "SELECT * FROM market_data "
            "WHERE symbol = ? AND exchange = ? AND timestamp >= ? "
            "ORDER BY timestamp DESC",
            (symbol, exchange, cutoff),
        )
        rows = await cursor.fetchall()
        return [MarketDataRow(**dict(r)) for r in rows]

    async def get_7day_avg_volume(self, symbol: str, exchange: str) -> float | None:
        cutoff = int(time.time()) - 7 * 24 * 3600
        cursor = await self._conn.execute(
            "SELECT AVG(volume) AS avg_vol FROM market_data "
            "WHERE symbol = ? AND exchange = ? AND timestamp >= ?",
            (symbol, exchange, cutoff),
        )
        row = await cursor.fetchone()
        return row["avg_vol"] if row and row["avg_vol"] is not None else None

    async def get_24h_stats(
        self, symbol: str, exchange: str
    ) -> dict[str, float | None]:
        cutoff = int(time.time()) - 24 * 3600
        cursor = await self._conn.execute(
            "SELECT "
            "  AVG(volume) AS avg_volume, "
            "  AVG(quote_volume) AS avg_quote_volume, "
            "  AVG(funding_rate) AS avg_funding_rate, "
            "  AVG(open_interest) AS avg_open_interest, "
            "  AVG(cvd) AS avg_cvd, "
            "  AVG(long_short_ratio) AS avg_ls_ratio, "
            "  AVG(bid_depth) AS avg_bid_depth, "
            "  AVG(ask_depth) AS avg_ask_depth, "
            "  MAX(high) AS high_24h, "
            "  MIN(low) AS low_24h "
            "FROM market_data "
            "WHERE symbol = ? AND exchange = ? AND timestamp >= ?",
            (symbol, exchange, cutoff),
        )
        row = await cursor.fetchone()
        if row is None:
            return {}
        return {k: row[k] for k in row.keys()}

    async def get_oldest_timestamp(
        self, symbol: str, exchange: str
    ) -> int | None:
        cursor = await self._conn.execute(
            "SELECT MIN(timestamp) AS ts FROM market_data "
            "WHERE symbol = ? AND exchange = ?",
            (symbol, exchange),
        )
        row = await cursor.fetchone()
        return row["ts"] if row and row["ts"] is not None else None


class SignalRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, signal: SignalRow) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO signals (symbol, module, score, priority, details, created_at) "
            "VALUES (:symbol, :module, :score, :priority, :details, :created_at)",
            asdict(signal),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_recent_by_symbol(
        self, symbol: str, hours: int
    ) -> list[SignalRow]:
        cutoff = int(time.time()) - hours * 3600
        cursor = await self._conn.execute(
            "SELECT * FROM signals "
            "WHERE symbol = ? AND created_at >= ? "
            "ORDER BY created_at DESC",
            (symbol, cutoff),
        )
        rows = await cursor.fetchall()
        return [SignalRow(**dict(r)) for r in rows]


class CooldownRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def set_cooldown(
        self, symbol: str, module: str, minutes: int
    ) -> None:
        cooldown_until = int(time.time()) + minutes * 60
        # Check if cooldown record already exists for this symbol+module
        cursor = await self._conn.execute(
            "SELECT id FROM cooldowns WHERE symbol = ? AND module = ?",
            (symbol, module),
        )
        existing = await cursor.fetchone()
        if existing:
            await self._conn.execute(
                "UPDATE cooldowns SET cooldown_until = ? WHERE id = ?",
                (cooldown_until, existing["id"]),
            )
        else:
            await self._conn.execute(
                "INSERT INTO cooldowns (symbol, module, cooldown_until) "
                "VALUES (?, ?, ?)",
                (symbol, module, cooldown_until),
            )
        await self._conn.commit()

    async def is_cooled_down(self, symbol: str, module: str) -> bool:
        now = int(time.time())
        cursor = await self._conn.execute(
            "SELECT cooldown_until FROM cooldowns "
            "WHERE symbol = ? AND module = ?",
            (symbol, module),
        )
        row = await cursor.fetchone()
        # No record or expired means cooled down
        if row is None:
            return True
        return now >= row["cooldown_until"]

    async def cleanup_expired(self) -> int:
        now = int(time.time())
        cursor = await self._conn.execute(
            "DELETE FROM cooldowns WHERE cooldown_until < ?", (now,)
        )
        await self._conn.commit()
        return cursor.rowcount


class ContractRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def get_known_symbols(self, exchange: str) -> list[str]:
        cursor = await self._conn.execute(
            "SELECT DISTINCT symbol FROM known_contracts WHERE exchange = ?",
            (exchange,),
        )
        rows = await cursor.fetchall()
        return [r["symbol"] for r in rows]

    async def add_symbol(self, symbol: str, exchange: str) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO known_contracts (symbol, exchange, detected_at) "
            "VALUES (?, ?, ?)",
            (symbol, exchange, int(time.time())),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def is_new_symbol(self, symbol: str, exchange: str) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM known_contracts "
            "WHERE symbol = ? AND exchange = ? LIMIT 1",
            (symbol, exchange),
        )
        return await cursor.fetchone() is None
