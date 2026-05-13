from __future__ import annotations

import math

from coin_radar.config.models import MonitorConfig
from coin_radar.db.database import DatabaseManager
from coin_radar.notifiers.formatter import Signal

_LOOKBACK_HOURS = 7 * 24
_MIN_SAMPLES = 2


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
            return None

        # Calculate perp_volume / spot_volume ratio for each row, skip invalid data points
        ratios: list[float] = []
        for row in rows:
            if not row.spot_volume or row.spot_volume <= 0:
                continue
            if row.perp_volume is None:
                continue
            ratios.append(row.perp_volume / row.spot_volume)

        # Insufficient valid data points to calculate statistics
        if len(ratios) < _MIN_SAMPLES:
            return None

        # Calculate mean and standard deviation
        mean = sum(ratios) / len(ratios)
        variance = sum((r - mean) ** 2 for r in ratios) / len(ratios)
        std = math.sqrt(variance)

        # Standard deviation is 0, cannot calculate z-score as all ratios are identical
        if std == 0:
            return None

        # Current ratio uses the latest valid data point
        current_ratio = ratios[0]
        z_score = (current_ratio - mean) / std

        # |z-score| does not exceed threshold, no alert triggered
        if abs(z_score) <= self._config.z_score_threshold:
            return None

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
            ratio_current=current_ratio,
            ratio_mean=mean,
            ratio_std=std,
            sample_count=len(ratios),
            sample_duration_hours=sample_duration_hours,
        )
