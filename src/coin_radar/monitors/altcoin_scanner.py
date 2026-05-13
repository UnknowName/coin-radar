from __future__ import annotations

import time

from coin_radar.config.models import MonitorConfig
from coin_radar.db.database import DatabaseManager
from coin_radar.db.models import MarketDataRow
from coin_radar.notifiers.formatter import Signal

_MODULE = "山寨币异动"


def _linear_score(value: float, full_point: float) -> float:
    if value <= 0:
        return 0.0
    if value >= full_point:
        return 100.0
    return value / full_point * 100.0


class AltcoinScanner:
    def __init__(self, db: DatabaseManager, config: MonitorConfig):
        self._db = db
        self._config = config

    async def scan(self, symbols: list[str], exchange: str = "binance") -> list[Signal]:
        signals = []
        for symbol in symbols:
            if not await self._db.cooldowns.is_cooled_down(symbol, _MODULE):
                continue
            signal = await self._score_symbol(symbol, exchange)
            if signal is not None:
                signals.append(signal)
                await self._db.cooldowns.set_cooldown(
                    symbol, _MODULE, self._config.dedup_minutes
                )
        return signals

    async def _score_symbol(self, symbol: str, exchange: str) -> Signal | None:
        recent = await self._db.market_data.get_recent(symbol, exchange, hours=1)
        if not recent:
            return None
        latest = recent[0]

        stats_24h = await self._db.market_data.get_24h_stats(symbol, exchange)
        cvd_mean, cvd_std = await self._get_7day_cvd_stats(symbol, exchange)
        old_oi, old_price = await self._get_4h_ago_data(symbol, exchange)
        avg_vp_ratio = await self._get_7day_avg_vp_ratio(symbol, exchange)
        change_24h = await self._get_24h_change(symbol, exchange, latest.close)

        extended_stats = {**stats_24h, "avg_vp_ratio_7d": avg_vp_ratio}

        s1 = self._score_silent_accumulation(recent, extended_stats)
        s2 = self._score_cvd_anomaly(latest.cvd, cvd_mean, cvd_std)
        s3 = self._score_oi_price_stable(
            latest.open_interest, latest.close, old_oi, old_price
        )
        s4 = self._score_whale_divergence(
            latest.top_trader_long_short_ratio, latest.long_short_ratio
        )
        s5 = self._score_funding_divergence(change_24h, latest.funding_rate)
        s6 = self._score_liquidity_thinning(
            latest.bid_depth, latest.ask_depth,
            stats_24h.get("avg_bid_depth"), stats_24h.get("avg_ask_depth"),
        )

        w = self._config.weights
        total = (
            s1 * w.silent_accumulation
            + s2 * w.cvd_anomaly
            + s3 * w.oi_price_stable
            + s4 * w.whale_divergence
            + s5 * w.funding_divergence
            + s6 * w.liquidity_thinning
        )

        if total < self._config.score_threshold:
            return None

        priority = "high" if total >= self._config.high_priority_threshold else "normal"

        z_score = None
        if latest.cvd is not None and cvd_mean is not None and cvd_std and cvd_std > 0:
            z_score = (latest.cvd - cvd_mean) / cvd_std

        direction = None
        if latest.cvd is not None:
            direction = "多头主导" if latest.cvd > 0 else "空头主导"

        return Signal(
            module=_MODULE,
            symbol=symbol,
            score=total,
            priority=priority,
            z_score=z_score,
            direction=direction,
            price=latest.close,
            change_24h=change_24h,
            volume=latest.volume,
            open_interest=latest.open_interest,
            details={
                "silent_accumulation": round(s1, 2),
                "cvd_anomaly": round(s2, 2),
                "oi_price_stable": round(s3, 2),
                "whale_divergence": round(s4, 2),
                "funding_divergence": round(s5, 2),
                "liquidity_thinning": round(s6, 2),
            },
        )

    def _score_silent_accumulation(
        self, data: list[MarketDataRow], stats_24h: dict
    ) -> float:
        if not data:
            return 0.0
        latest = data[0]
        if latest.low <= 0:
            return 0.0

        # 24h价格波动率
        volatility = (latest.high - latest.low) / latest.low
        if volatility >= 0.05:
            return 0.0

        # 5min成交量/价格变动比值
        price_change = abs(latest.close - latest.open)
        if price_change == 0:
            # 价格无变动但有成交量，强烈吸筹信号
            return 100.0 if latest.volume > 0 else 0.0

        current_vp_ratio = latest.volume / price_change

        # 7日vp_ratio均值
        avg_vp_ratio = stats_24h.get("avg_vp_ratio_7d")
        if not avg_vp_ratio or avg_vp_ratio <= 0:
            return 0.0

        # 偏离倍数 = 当前/均值 - 1；偏离2倍(当前=3倍均值)得100，1倍(当前=2倍均值)得50
        excess = current_vp_ratio / avg_vp_ratio - 1
        return _linear_score(excess, 2.0)

    def _score_cvd_anomaly(
        self, current_cvd: float | None, mean: float | None, std: float | None
    ) -> float:
        if current_cvd is None or mean is None or std is None or std <= 0:
            return 0.0
        z = (current_cvd - mean) / std
        if z <= 0:
            return 0.0
        # z_score >= 2得100，>= 1得50，线性插值
        return _linear_score(z, 2.0)

    def _score_oi_price_stable(
        self,
        current_oi: float | None,
        current_price: float,
        old_oi: float | None,
        old_price: float | None,
    ) -> float:
        if current_oi is None or old_oi is None or old_oi <= 0:
            return 0.0
        if old_price is None or old_price <= 0:
            return 0.0

        # 价格变动率超过3%则不符合"价格平稳"
        price_change = abs(current_price - old_price) / old_price
        if price_change >= 0.03:
            return 0.0

        # OI增长率：>= 10%得100，>= 5%得50
        oi_growth = (current_oi - old_oi) / old_oi
        return _linear_score(oi_growth, 0.10)

    def _score_whale_divergence(
        self, top_ratio: float | None, all_ratio: float | None
    ) -> float:
        if top_ratio is None or all_ratio is None:
            return 0.0
        # 背离度 = 大户多空比与全体多空比之差
        divergence = abs(top_ratio - all_ratio)
        # 背离度 >= 0.3得100，>= 0.15得50
        return _linear_score(divergence, 0.30)

    def _score_funding_divergence(
        self, change_24h: float | None, funding_rate: float | None
    ) -> float:
        if change_24h is None or funding_rate is None:
            return 0.0
        # 价格上涨但资金费率为负 → 空头付费维持，看涨信号
        if change_24h > 0 and funding_rate < 0:
            return 100.0
        # 价格下跌但资金费率为正 → 多头付费维持，看跌信号
        if change_24h < 0 and funding_rate > 0:
            return 100.0
        return 0.0

    def _score_liquidity_thinning(
        self,
        bid_depth: float | None,
        ask_depth: float | None,
        avg_bid: float | None,
        avg_ask: float | None,
    ) -> float:
        if bid_depth is None or ask_depth is None or avg_bid is None or avg_ask is None:
            return 0.0
        current_depth = bid_depth + ask_depth
        avg_depth = avg_bid + avg_ask
        if avg_depth <= 0:
            return 0.0

        # 下降比 = (均值 - 当前) / 均值
        drop_ratio = (avg_depth - current_depth) / avg_depth
        if drop_ratio <= 0:
            return 0.0
        # 下降比 >= 30%得100，>= 15%得50
        return _linear_score(drop_ratio, 0.30)

    async def _get_7day_cvd_stats(
        self, symbol: str, exchange: str
    ) -> tuple[float | None, float | None]:
        cutoff = int(time.time()) - 7 * 24 * 3600
        cursor = await self._db.conn.execute(
            "SELECT AVG(cvd) AS mean, "
            "SQRT(MAX(0, AVG(cvd * cvd) - AVG(cvd) * AVG(cvd))) AS std "
            "FROM market_data "
            "WHERE symbol = ? AND exchange = ? AND timestamp >= ? AND cvd IS NOT NULL",
            (symbol, exchange, cutoff),
        )
        row = await cursor.fetchone()
        if row is None or row["mean"] is None:
            return None, None
        return row["mean"], row["std"]

    async def _get_4h_ago_data(
        self, symbol: str, exchange: str
    ) -> tuple[float | None, float | None]:
        cutoff = int(time.time()) - 4 * 3600
        cursor = await self._db.conn.execute(
            "SELECT open_interest, close FROM market_data "
            "WHERE symbol = ? AND exchange = ? AND timestamp <= ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (symbol, exchange, cutoff),
        )
        row = await cursor.fetchone()
        if row is None:
            return None, None
        return row["open_interest"], row["close"]

    async def _get_7day_avg_vp_ratio(
        self, symbol: str, exchange: str
    ) -> float | None:
        cutoff = int(time.time()) - 7 * 24 * 3600
        cursor = await self._db.conn.execute(
            "SELECT AVG(CASE WHEN ABS(close - open) > 0 "
            "THEN volume / ABS(close - open) ELSE NULL END) AS avg_ratio "
            "FROM market_data "
            "WHERE symbol = ? AND exchange = ? AND timestamp >= ?",
            (symbol, exchange, cutoff),
        )
        row = await cursor.fetchone()
        return row["avg_ratio"] if row and row["avg_ratio"] is not None else None

    async def _get_24h_change(
        self, symbol: str, exchange: str, current_close: float
    ) -> float | None:
        cutoff = int(time.time()) - 24 * 3600
        cursor = await self._db.conn.execute(
            "SELECT close FROM market_data "
            "WHERE symbol = ? AND exchange = ? AND timestamp <= ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (symbol, exchange, cutoff),
        )
        row = await cursor.fetchone()
        if row is None or row["close"] == 0:
            return None
        return (current_close - row["close"]) / row["close"] * 100
