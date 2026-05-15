"""
单次扫描脚本：运行一次完整的监控周期后退出
用于调试和 GitHub Actions 环境
"""
from __future__ import annotations

import asyncio
import logging
import time

from coin_radar.async_utils import cleanup_event_loop
from coin_radar.config.loader import load_config
from coin_radar.config.models import AppConfig
from coin_radar.db.database import DatabaseManager
from coin_radar.fetcher.data_fetcher import DataFetcher
from coin_radar.fetcher.exchange_adapter import ExchangeAdapter
from coin_radar.filters.signal_filter import SignalFilter
from coin_radar.monitors.altcoin_scanner import AltcoinScanner
from coin_radar.monitors.major_coin_alert import MajorCoinAlert, MAJOR_SYMBOLS
from coin_radar.monitors.new_contract_detector import NewContractDetector
from coin_radar.monitors.perp_spot_ratio import PerpSpotRatioMonitor
from coin_radar.notifiers.dingtalk import DingTalkNotifier
from coin_radar.notifiers.formatter import Signal
from coin_radar.logger import setup_logger

logger = logging.getLogger(__name__)


class SingleScanScheduler:
    """单次扫描调度器：运行一次完整周期后退出"""

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or load_config()
        self._db = DatabaseManager()
        self._fetcher = DataFetcher(self._db, self._config)
        self._altcoin_scanner = AltcoinScanner(self._db, self._config.monitor)
        self._major_alert = MajorCoinAlert(self._db, self._config.monitor)
        self._new_contract = NewContractDetector(self._db)
        self._perp_spot = PerpSpotRatioMonitor(self._db, self._config.monitor)
        self._signal_filter = SignalFilter(self._db, self._config.filter, self._config.monitor)
        self._notifier = DingTalkNotifier(self._config.dingtalk)
        self._symbols_by_exchange: dict[str, list[str]] = {}

    async def run(self) -> None:
        """运行单次完整扫描周期"""
        try:
            await self._db.connect()
            # 1. 初始化交易对列表
            await self._init_symbols()
            
            # 2. 数据获取
            await self._fetch_data()
            
            # 3. 监控模块扫描
            signals = await self._run_monitors()
            
            # 4. 信号过滤
            filtered = await self._signal_filter.filter(signals)
            
            # 5. 推送通知
            await self._push_signals(filtered)
            
            # 6. 输出统计
            logger.info("=== Single scan completed ===")
            logger.info("Total signals: %d, Filtered signals: %d", len(signals), len(filtered))
            if filtered:
                logger.info("Filtered signals details:")
                for s in filtered:
                    logger.info(
                        "  %s | %s | score=%.0f | priority=%s | price=%s",
                        s.module, s.symbol, s.score, s.priority,
                        f"{s.price:.2f}" if s.price else "N/A",
                    )
        finally:
            await self._fetcher.close()
            await self._db.close()

    async def _init_symbols(self) -> None:
        """从所有配置的交易所获取交易对列表"""
        for name in self._config.exchange_names:
            try:
                adapter = self._fetcher.get_adapter(name)
                markets = await adapter.fetch_markets()
                symbols = [
                    m["symbol"] for m in markets
                    if m["symbol"].endswith("/USDT") and m.get("active", True)
                ]
                if not symbols:
                    logger.warning("No symbols fetched from %s (empty list)", name)
                    self._symbols_by_exchange[name] = []
                else:
                    self._symbols_by_exchange[name] = symbols
                    logger.info("Fetched %d USDT symbols from %s", len(symbols), name)
            except Exception as e:
                logger.exception("Failed to fetch symbols from %s", name)
                self._symbols_by_exchange[name] = []

    async def _fetch_data(self) -> None:
        """并发从所有交易所获取市场数据"""
        tasks = []
        exchange_names = []
        for exchange, symbols in self._symbols_by_exchange.items():
            if symbols:  # 只获取有交易对的交易所
                tasks.append(self._fetcher.fetch_all(symbols, exchange))
                exchange_names.append(exchange)
        
        if not tasks:
            logger.warning("No symbols to fetch, skipping data fetch")
            return
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(exchange_names, results):
            if isinstance(result, Exception):
                logger.exception("Data fetch error for %s", name)

    async def _run_monitors(self) -> list[Signal]:
        """在所有交易所上运行监控模块并聚合信号"""
        monitor_tasks = []
        exchange_keys = []
        for exchange, symbols in self._symbols_by_exchange.items():
            if not symbols:
                continue
            alt_symbols = [s for s in symbols if s not in MAJOR_SYMBOLS]
            monitor_tasks.append(
                self._run_exchange_monitors(exchange, alt_symbols, symbols)
            )
            exchange_keys.append(exchange)

        results = await asyncio.gather(*monitor_tasks, return_exceptions=True)
        all_signals: list[Signal] = []
        for name, result in zip(exchange_keys, results):
            if isinstance(result, Exception):
                logger.exception("Monitor error for %s", name)
            else:
                all_signals.extend(result)
        return all_signals

    async def _run_exchange_monitors(
        self, exchange: str, alt_symbols: list[str], all_symbols: list[str]
    ) -> list[Signal]:
        """在单个交易所上运行三个监控模块"""
        logger.info(
            "[%s] Symbol split: total=%d | altcoins=%d",
            exchange, len(all_symbols), len(alt_symbols),
        )
        alt_signals, major_signals, ratio_signals = await asyncio.gather(
            self._altcoin_scanner.scan(alt_symbols, exchange),
            self._major_alert.scan(MAJOR_SYMBOLS, exchange),
            self._perp_spot.scan(all_symbols, exchange),
        )
        logger.info(
            "[%s] Monitor results: altcoins %d, majors %d, ratio %d",
            exchange, len(alt_signals), len(major_signals), len(ratio_signals),
        )
        return alt_signals + major_signals + ratio_signals

    async def _push_signals(self, signals: list[Signal]) -> None:
        """推送信号到钉钉，失败时使用备用通知"""
        if not signals:
            logger.info("No signals to push")
            return

        try:
            result = await self._notifier.send_signals_batch(signals)
            if result.get("errcode") == 0:
                sent_count = result.get("sent_count", len(signals))
                logger.info("Batch push success: %d signals sent", sent_count)
            else:
                logger.warning("Batch push failed: %s", result)
        except Exception as e:
            logger.exception("Batch push error")


def main() -> None:
    """单次扫描入口"""
    config = load_config()
    setup_logger(log_file="~/.coin_radar/coin_radar.log")

    scheduler = SingleScanScheduler(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(scheduler.run())
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        cleanup_event_loop(loop)


if __name__ == "__main__":
    main()
