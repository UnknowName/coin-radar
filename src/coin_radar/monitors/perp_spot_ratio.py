from __future__ import annotations

import logging
import math

from coin_radar.config.models import MonitorConfig
from coin_radar.db.database import DatabaseManager
from coin_radar.monitors.altcoin_scanner import _calc_rsi, _calc_resistance_support
from coin_radar.notifiers.formatter import Signal

_LOOKBACK_HOURS = 7 * 24
_MIN_SAMPLES = 2

logger = logging.getLogger(__name__)


class PerpSpotRatioMonitor:
    def __init__(self, db: DatabaseManager, config: MonitorConfig) -> None:
        self._db = db
        self._config = config

    async def scan(self, symbols: list[str], exchange: str = "binance") -> list[Signal]:
        signals: list[Signal] = []
        for symbol in symbols:
            signal = await self._check_symbol(symbol, exchange)
            if signal is not None:
                signals.append(signal)
        return signals

    async def _check_symbol(self, symbol: str, exchange: str) -> Signal | None:
        rows = await self._db.market_data.get_recent(symbol, exchange, _LOOKBACK_HOURS)
        if len(rows) < _MIN_SAMPLES:
            logger.debug("[%s] Skip: insufficient data points (%d<%d)", symbol, len(rows), _MIN_SAMPLES)
            return None

        ratios: list[float] = []
        for row in rows:
            if not row.spot_volume or row.spot_volume <= 0:
                continue
            if row.perp_volume is None:
                continue
            ratios.append(row.perp_volume / row.spot_volume)

        if len(ratios) < _MIN_SAMPLES:
            logger.debug("[%s] Skip: insufficient valid ratios (%d<%d)", symbol, len(ratios), _MIN_SAMPLES)
            return None

        mean = sum(ratios) / len(ratios)
        variance = sum((r - mean) ** 2 for r in ratios) / len(ratios)
        std = math.sqrt(variance)

        if std == 0:
            logger.debug("[%s] Skip: std dev is 0 (all ratios identical=%.2f)", symbol, mean)
            return None

        current_ratio = ratios[0]
        z_score = (current_ratio - mean) / std

        if abs(z_score) <= self._config.z_score_threshold:
            logger.info(
                "[%s] Perp/spot ratio: z=%.2f(threshold=%.1f) | ratio=%.2f mean=%.2f std=%.4f | below threshold",
                symbol, z_score, self._config.z_score_threshold,
                current_ratio, mean, std,
            )
            return None

        logger.info(
            "[%s] Perp/spot ratio: z=%.2f(threshold=%.1f) | ratio=%.2f mean=%.2f std=%.4f | triggered!",
            symbol, z_score, self._config.z_score_threshold,
            current_ratio, mean, std,
        )

        # Score: z=3→60, z=5→100, capped at 100
        score = min(100.0, abs(z_score) * 20)

        # Direction judgment
        direction = "Bullish" if z_score > 0 else "Bearish"

        # Speculation label
        speculation_label = None
        if current_ratio > 10:
            speculation_label = "High speculation"
        elif current_ratio < 0:
            speculation_label = "Low speculation"

        # Additional information
        latest = rows[0]
        oldest = rows[-1]
        sample_duration_hours = (latest.timestamp - oldest.timestamp) / 3600

        change_24h = None
        if len(rows) >= 2 and rows[-1].close > 0:
            change_24h = (latest.close - rows[-1].close) / rows[-1].close * 100

        # 计算技术指标
        closes = [r.close for r in rows]
        highs = [r.high for r in rows]
        lows = [r.low for r in rows]
        rsi_14 = _calc_rsi(closes)
        resistance, support = _calc_resistance_support(highs, lows)

        return Signal(
            module="Perp/Swap Ratio Anomaly",
            symbol=symbol,
            score=score,
            priority="high" if score >= 80 else "normal",
            z_score=z_score,
            direction=direction,
            speculation_label=speculation_label,
            price=latest.close,
            change_24h=change_24h,
            volume=latest.volume,
            rsi_14=rsi_14,
            resistance=resistance,
            support=support,
            ratio_current=current_ratio,
            ratio_mean=mean,
            ratio_std=std,
            sample_count=len(ratios),
            sample_duration_hours=sample_duration_hours,
        )
