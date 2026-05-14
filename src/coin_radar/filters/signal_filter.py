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
        baseline = self._monitor_config.baseline_hours
        filtered: list[Signal] = []
        for s in signals:
            if s.sample_duration_hours is None:
                logger.info(
                    "[SignalFilter] Hard threshold: %s(%s) sample duration unknown, rejected",
                    s.symbol, s.module,
                )
                continue
            if s.sample_duration_hours < baseline:
                logger.info(
                    "[SignalFilter] Hard threshold: %s(%s) sample duration %.1fh < %dh",
                    s.symbol, s.module, s.sample_duration_hours, baseline,
                )
                continue
            filtered.append(s)
        if len(filtered) < len(signals):
            logger.info(
                "[SignalFilter] Hard threshold: %d/%d passed", len(filtered), len(signals),
            )
        return filtered

    def _quality_sort(self, signals: list[Signal]) -> list[Signal]:
        for s in signals:
            if s.ratio_current is not None:
                if s.ratio_current > 10:
                    s.speculation_label = "High speculation"
                elif s.ratio_current < 0:
                    s.speculation_label = "Low speculation"
                # 变异系数加分: CV = std/mean, CV低表示币种稳定，异动更有意义
                if s.ratio_mean is not None and s.ratio_std is not None and s.ratio_mean > 0:
                    cv = s.ratio_std / s.ratio_mean
                    if cv < 0.2:
                        s.score = min(100.0, s.score + 5)
        sorted_signals = sorted(signals, key=lambda s: s.score, reverse=True)
        if len(sorted_signals) > self._filter_config.top_n:
            logger.info(
                "[SignalFilter] Quality sort truncation: keep top %d (total %d), truncated: %s",
                self._filter_config.top_n, len(sorted_signals),
                ", ".join(f"{s.symbol}({s.score:.0f})" for s in sorted_signals[self._filter_config.top_n:]),
            )
        return sorted_signals[: self._filter_config.top_n]

    async def _cooldown_filter(self, signals: list[Signal]) -> list[Signal]:
        cooldown_minutes = self._filter_config.cooldown_minutes
        filtered: list[Signal] = []
        for s in signals:
            if await self._db.cooldowns.is_cooled_down(s.symbol, s.module):
                filtered.append(s)
                await self._db.cooldowns.set_cooldown(
                    s.symbol, s.module, cooldown_minutes,
                )
            else:
                logger.info("[SignalFilter] Cooldown dedup: %s(%s) in cooldown period", s.symbol, s.module)
        return filtered

    def _cross_module_dedup(self, signals: list[Signal]) -> list[Signal]:
        seen: list[str] = []
        deduped: dict[str, Signal] = {}
        for s in signals:
            if s.symbol not in deduped or s.score > deduped[s.symbol].score:
                if s.symbol in deduped:
                    seen.append(f"{s.symbol}({deduped[s.symbol].module}@{deduped[s.symbol].score:.0f}→{s.module}@{s.score:.0f})")
                deduped[s.symbol] = s
        if seen:
            logger.info("[SignalFilter] Cross-module dedup: %s", ", ".join(seen))
        return list(deduped.values())
