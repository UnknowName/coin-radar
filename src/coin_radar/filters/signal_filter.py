from __future__ import annotations

import logging

from coin_radar.config.models import FilterConfig, MonitorConfig
from coin_radar.db.database import DatabaseManager
from coin_radar.notifiers.formatter import Signal

logger = logging.getLogger(__name__)


class SignalFilter:
    """Signal filter: hard threshold → quality score sorting → cooldown deduplication → cross-module deduplication"""

    def __init__(
        self,
        db: DatabaseManager,
        filter_config: FilterConfig,
        monitor_config: MonitorConfig,
    ) -> None:
        self._db = db
        self._filter_config = filter_config
        self._monitor_config = monitor_config

    async def filter(self, signals: list[Signal]) -> list[Signal]:
        """
        Four-layer filtering pipeline:
        1. Hard threshold - filter out signals with insufficient baseline sample duration
        2. Quality score sorting - speculation label + score descending + top N
        3. Cooldown mechanism - no duplicate pushes within cooldown period for same symbol and module
        4. Cross-module deduplication - keep only highest score signal for same symbol
        """
        result = self._hard_threshold_filter(signals)
        result = self._quality_sort(result)
        result = await self._cooldown_filter(result)
        result = self._cross_module_dedup(result)
        return result

    def _hard_threshold_filter(self, signals: list[Signal]) -> list[Signal]:
        """Filter out signals with baseline sample duration less than baseline_hours; None values are kept"""
        baseline = self._monitor_config.baseline_hours
        filtered: list[Signal] = []
        for s in signals:
            if s.sample_duration_hours is not None and s.sample_duration_hours < baseline:
                logger.debug(
                    "Hard threshold filter: %s sample duration %.1fh < %dh",
                    s.symbol, s.sample_duration_hours, baseline,
                )
                continue
            filtered.append(s)
        return filtered

    def _quality_sort(self, signals: list[Signal]) -> list[Signal]:
        """Speculation label + score descending + keep top N"""
        for s in signals:
            if s.ratio_current is not None:
                if s.ratio_current > 10:
                    s.speculation_label = "High speculation"
                elif s.ratio_current < 0:
                    s.speculation_label = "Low speculation"
        sorted_signals = sorted(signals, key=lambda s: s.score, reverse=True)
        return sorted_signals[: self._filter_config.top_n]

    async def _cooldown_filter(self, signals: list[Signal]) -> list[Signal]:
        """Cooldown mechanism: no duplicate pushes within cooldown period for same symbol and module"""
        cooldown_minutes = self._filter_config.cooldown_minutes
        filtered: list[Signal] = []
        for s in signals:
            if await self._db.cooldowns.is_cooled_down(s.symbol, s.module):
                filtered.append(s)
                await self._db.cooldowns.set_cooldown(
                    s.symbol, s.module, cooldown_minutes,
                )
            else:
                logger.debug("In cooldown: %s (%s)", s.symbol, s.module)
        return filtered

    def _cross_module_dedup(self, signals: list[Signal]) -> list[Signal]:
        """Keep only highest score signal for same symbol"""
        seen: dict[str, Signal] = {}
        for s in signals:
            if s.symbol not in seen or s.score > seen[s.symbol].score:
                seen[s.symbol] = s
        return list(seen.values())
