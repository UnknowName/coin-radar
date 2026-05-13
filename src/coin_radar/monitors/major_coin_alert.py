from __future__ import annotations

from coin_radar.config.models import MonitorConfig
from coin_radar.db.database import DatabaseManager
from coin_radar.db.models import MarketDataRow
from coin_radar.notifiers.formatter import Signal

MAJOR_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

PRICE_CHANGE_1H_THRESHOLD = 2.0
PRICE_CHANGE_4H_THRESHOLD = 5.0
PRICE_CHANGE_24H_THRESHOLD = 10.0
VOLUME_SPIKE_THRESHOLD = 3.0

_SCORE_MAP: dict[str, float] = {"1h": 60, "4h": 75, "24h": 90}


class MajorCoinAlert:
    def __init__(self, db: DatabaseManager, config: MonitorConfig) -> None:
        self._db = db
        self._config = config

    async def scan(
        self, symbols: list[str] | None = None, exchange: str = "binance"
    ) -> list[Signal]:
        symbols = symbols or MAJOR_SYMBOLS
        signals: list[Signal] = []
        for symbol in symbols:
            signal = await self._check_symbol(symbol, exchange)
            if signal is not None:
                signals.append(signal)
        return signals

    async def _check_symbol(
        self, symbol: str, exchange: str
    ) -> Signal | None:
        # Get candlestick data for the last 24h
        rows = await self._db.market_data.get_recent(symbol, exchange, hours=24)
        if not rows:
            return None

        now_ts = rows[0].timestamp
        current_price = rows[0].close
        current_volume = rows[0].volume

        # Calculate price change for 1h/4h/24h
        change_1h = _calc_price_change(rows, now_ts, 1)
        change_4h = _calc_price_change(rows, now_ts, 4)
        change_24h = _calc_price_change(rows, now_ts, 24)

        # Calculate current volume relative to 24h average
        stats = await self._db.market_data.get_24h_stats(symbol, exchange)
        avg_volume = stats.get("avg_volume") if stats else None
        volume_multiple = (
            current_volume / avg_volume
            if avg_volume and avg_volume > 0
            else None
        )

        # Check if any threshold is triggered
        triggers: list[str] = []
        if change_1h is not None and abs(change_1h) >= PRICE_CHANGE_1H_THRESHOLD:
            triggers.append("1h")
        if change_4h is not None and abs(change_4h) >= PRICE_CHANGE_4H_THRESHOLD:
            triggers.append("4h")
        if change_24h is not None and abs(change_24h) >= PRICE_CHANGE_24H_THRESHOLD:
            triggers.append("24h")

        volume_spike = (
            volume_multiple is not None and volume_multiple >= VOLUME_SPIKE_THRESHOLD
        )

        # No signal if no condition is triggered
        if not triggers and not volume_spike:
            return None

        score = _calc_score(triggers, volume_spike)

        # Direction: use longest timeframe price change, up for bullish, down for bearish
        ref_change = next(
            (c for c in (change_24h, change_4h, change_1h) if c is not None),
            None,
        )
        direction = "Bullish" if ref_change is not None and ref_change > 0 else "Bearish"

        # Open interest: use latest value, fallback to 24h average if missing
        open_interest = rows[0].open_interest
        if open_interest is None and stats:
            open_interest = stats.get("avg_open_interest")

        return Signal(
            module="Major Coin Alert",
            symbol=symbol,
            score=score,
            priority="high" if score >= 80 else "normal",
            direction=direction,
            price=current_price,
            change_24h=change_24h,
            volume=current_volume,
            volume_24h_multiple=volume_multiple,
            open_interest=open_interest,
            details={
                "change_1h": change_1h,
                "change_4h": change_4h,
                "change_24h": change_24h,
                "volume_spike": volume_spike,
                "triggers": triggers,
            },
        )


def _calc_price_change(
    rows: list[MarketDataRow], now_ts: int, hours: int
) -> float | None:
    """Find the record closest to and not later than cutoff in rows, calculate price change percentage"""
    cutoff = now_ts - hours * 3600
    closest: MarketDataRow | None = None
    min_diff = float("inf")
    for row in rows:
        if row.timestamp <= cutoff:
            diff = abs(row.timestamp - cutoff)
            if diff < min_diff:
                min_diff = diff
                closest = row
    if closest is None or closest.close == 0:
        return None
    return (rows[0].close - closest.close) / closest.close * 100


def _calc_score(triggers: list[str], volume_spike: bool) -> float:
    """Scoring rule: take highest trigger score + 10 for volume spike + 10 for multiple conditions, capped at 100"""
    base_score = max((_SCORE_MAP[t] for t in triggers), default=0)
    if volume_spike:
        base_score += 10
    total_conditions = len(triggers) + (1 if volume_spike else 0)
    if total_conditions >= 2:
        base_score += 10
    return min(base_score, 100)
