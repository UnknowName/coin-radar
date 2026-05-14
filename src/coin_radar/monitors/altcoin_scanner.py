from __future__ import annotations

import logging
import time

from coin_radar.config.models import MonitorConfig
from coin_radar.db.database import DatabaseManager
from coin_radar.db.models import MarketDataRow
from coin_radar.notifiers.formatter import Signal

_MODULE = "Altcoin Movement"
logger = logging.getLogger(__name__)


def _linear_score(value: float, full_point: float) -> float:
    if value <= 0:
        return 0.0
    if value >= full_point:
        return 100.0
    return value / full_point * 100.0


def _calc_rsi(closes: list[float], period: int = 14) -> float | None:
    # RSI(14): 使用 Wilder 平滑法计算相对强弱指标
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i + 1] for i in range(len(closes) - 1)]
    gains = [d if d > 0 else 0.0 for d in deltas[:period]]
    losses = [-d if d < 0 else 0.0 for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for delta in deltas[period:]:
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _calc_resistance_support(highs: list[float], lows: list[float]) -> tuple[float | None, float | None]:
    # 阻力位/支撑位: 使用近期高/低点枢轴点计算
    if not highs or not lows:
        return None, None
    resistance = max(highs)
    support = min(lows)
    return resistance, support


class AltcoinScanner:
    def __init__(self, db: DatabaseManager, config: MonitorConfig):
        self._db = db
        self._config = config

    async def scan(self, symbols: list[str], exchange: str = "binance") -> list[Signal]:
        signals = []
        for symbol in symbols:
            if not await self._db.cooldowns.is_cooled_down(symbol, _MODULE):
                logger.debug("[%s] Skip: in cooldown period", symbol)
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
            logger.debug("[%s] Skip: no market data in last 1h", symbol)
            return None
        latest = recent[0]

        stats_24h = await self._db.market_data.get_24h_stats(symbol, exchange)
        cvd_mean, cvd_std = await self._get_7day_cvd_stats(symbol, exchange)
        old_oi, old_price = await self._get_4h_ago_data(symbol, exchange)
        avg_vp_ratio = await self._get_7day_avg_vp_ratio(symbol, exchange)
        change_24h = await self._get_24h_change(symbol, exchange, latest.close)

        extended_stats = {**stats_24h, "avg_vp_ratio_7d": avg_vp_ratio}

        s1, r1 = self._score_silent_accumulation(recent, extended_stats)
        s2, r2 = self._score_cvd_anomaly(latest.cvd, cvd_mean, cvd_std)
        s3, r3 = self._score_oi_price_stable(
            latest.open_interest, latest.close, old_oi, old_price
        )
        s4, r4 = self._score_whale_divergence(
            latest.top_trader_long_short_ratio, latest.long_short_ratio
        )
        s5, r5 = self._score_funding_divergence(change_24h, latest.funding_rate)
        s6, r6 = self._score_liquidity_thinning(
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

        logger.info(
            "[%s] Scoring: total=%.1f(threshold=%.0f) | silent_accum=%.1f(%s) | cvd_anom=%.1f(%s) | oi_price_stable=%.1f(%s) | whale_div=%.1f(%s) | funding_div=%.1f(%s) | liq_thin=%.1f(%s)",
            symbol, total, self._config.score_threshold,
            s1, r1, s2, r2, s3, r3, s4, r4, s5, r5, s6, r6,
        )

        if total < self._config.score_threshold:
            return None

        priority = "high" if total >= self._config.high_priority_threshold else "normal"

        z_score = None
        if latest.cvd is not None and cvd_mean is not None and cvd_std and cvd_std > 0:
            z_score = (latest.cvd - cvd_mean) / cvd_std

        direction = None
        if latest.cvd is not None:
            direction = "Bullish" if latest.cvd > 0 else "Bearish"

        # 计算样本时长：从数据库中最早记录到最新记录的时间跨度
        sample_duration_hours = None
        oldest_ts = await self._db.market_data.get_oldest_timestamp(symbol, exchange)
        if oldest_ts is not None:
            sample_duration_hours = (latest.timestamp - oldest_ts) / 3600

        # 计算技术指标
        closes = [r.close for r in recent]
        highs = [r.high for r in recent]
        lows = [r.low for r in recent]
        rsi_14 = _calc_rsi(closes)
        resistance, support = _calc_resistance_support(highs, lows)

        # 1h量倍数: 当前1h成交量 / 24h平均成交量
        volume_1h_multiple = None
        avg_volume = stats_24h.get("avg_volume")
        if avg_volume and avg_volume > 0 and latest.volume:
            volume_1h_multiple = latest.volume / avg_volume

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
            rsi_14=rsi_14,
            resistance=resistance,
            support=support,
            volume_1h_multiple=volume_1h_multiple,
            sample_duration_hours=sample_duration_hours,
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
    ) -> tuple[float, str]:
        if not data:
            return 0.0, "No data"

        # 需求3.2.1.1: 24小时价格波动小于5%
        high_24h = stats_24h.get("high_24h")
        low_24h = stats_24h.get("low_24h")
        if high_24h is None or low_24h is None:
            return 0.0, "24h high/low data missing"
        if low_24h <= 0:
            return 0.0, "24h low price <= 0"

        volatility_24h = (high_24h - low_24h) / low_24h
        if volatility_24h >= 0.05:
            return 0.0, f"24h Volatility {volatility_24h:.1%} > 5%"

        # 5分钟"成交量/价格变动"突然飙升
        latest = data[0]
        price_change = abs(latest.close - latest.open)
        if price_change == 0:
            if latest.volume > 0:
                return 100.0, "Price unchanged but volume present"
            return 0.0, "No price or volume change"

        current_vp_ratio = latest.volume / price_change

        avg_vp_ratio = stats_24h.get("avg_vp_ratio_7d")
        if not avg_vp_ratio or avg_vp_ratio <= 0:
            return 0.0, "No 7-day avg VP ratio"

        excess = current_vp_ratio / avg_vp_ratio - 1
        score = _linear_score(excess, 2.0)
        return score, f"Deviation {excess:.1%}"

    def _score_cvd_anomaly(
        self, current_cvd: float | None, mean: float | None, std: float | None
    ) -> tuple[float, str]:
        if current_cvd is None or mean is None or std is None or std <= 0:
            return 0.0, "CVD data incomplete"
        z = (current_cvd - mean) / std
        if z <= 0:
            return 0.0, f"z={z:.2f}<=0 (not bullish)"
        score = _linear_score(z, 2.0)
        return score, f"z={z:.2f}"

    def _score_oi_price_stable(
        self,
        current_oi: float | None,
        current_price: float,
        old_oi: float | None,
        old_price: float | None,
    ) -> tuple[float, str]:
        if current_oi is None or old_oi is None or old_oi <= 0:
            return 0.0, "OI data missing"
        if old_price is None or old_price <= 0:
            return 0.0, "4h ago price missing"

        price_change = abs(current_price - old_price) / old_price
        if price_change >= 0.03:
            return 0.0, f"Price change {price_change:.1%} >= 3%"

        oi_growth = (current_oi - old_oi) / old_oi
        score = _linear_score(oi_growth, 0.10)
        return score, f"Price change {price_change:.1%}, OI growth {oi_growth:.1%}"

    def _score_whale_divergence(
        self, top_ratio: float | None, all_ratio: float | None
    ) -> tuple[float, str]:
        if top_ratio is None or all_ratio is None:
            return 0.0, "Whale/retail long-short ratio data missing"
        divergence = abs(top_ratio - all_ratio)
        score = _linear_score(divergence, 0.30)
        return score, f"Divergence={divergence:.3f}"

    def _score_funding_divergence(
        self, change_24h: float | None, funding_rate: float | None
    ) -> tuple[float, str]:
        if change_24h is None or funding_rate is None:
            return 0.0, "Price change or funding rate data missing"
        if change_24h > 0 and funding_rate < 0:
            return 100.0, f"Up {change_24h:.1f}% + rate {funding_rate:.4f} (bullish divergence)"
        if change_24h < 0 and funding_rate > 0:
            return 100.0, f"Down {change_24h:.1f}% + rate {funding_rate:.4f} (bearish divergence)"
        return 0.0, f"Change {change_24h:.1f}% + rate {funding_rate:.4f} (no divergence)"

    def _score_liquidity_thinning(
        self,
        bid_depth: float | None,
        ask_depth: float | None,
        avg_bid: float | None,
        avg_ask: float | None,
    ) -> tuple[float, str]:
        if bid_depth is None or ask_depth is None or avg_bid is None or avg_ask is None:
            return 0.0, "Depth data missing"
        current_depth = bid_depth + ask_depth
        avg_depth = avg_bid + avg_ask
        if avg_depth <= 0:
            return 0.0, "Avg depth <= 0"

        drop_ratio = (avg_depth - current_depth) / avg_depth
        if drop_ratio <= 0:
            return 0.0, f"Depth not thinned (drop ratio={drop_ratio:.1%})"
        score = _linear_score(drop_ratio, 0.30)
        return score, f"Depth dropped {drop_ratio:.1%}"

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
