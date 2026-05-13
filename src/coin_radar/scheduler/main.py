from __future__ import annotations

import asyncio
import logging
import signal
import time

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

SCAN_INTERVAL = 300
CONTRACT_CHECK_INTERVAL = 3600


class Scheduler:
    """Main scheduler: periodically triggers data fetching, monitoring scans, signal filtering, and DingTalk notifications"""

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
        self._running = False
        self._symbols: list[str] = []

    async def run(self) -> None:
        """Main execution loop"""
        self._running = True
        await self._db.connect()
        try:
            await self._init_symbols()
            await self._check_new_contracts()

            last_contract_check = time.time()

            while self._running:
                cycle_start = time.time()
                logger.info("=== Starting new scan cycle ===")

                # 1. Data fetching
                await self._fetch_data()

                # 2. Four monitor modules scan in parallel
                signals = await self._run_monitors()

                # 3. Signal filtering
                filtered = await self._signal_filter.filter(signals)

                # 4. DingTalk notification
                await self._push_signals(filtered)

                # 5. Periodic new contract detection
                if time.time() - last_contract_check >= CONTRACT_CHECK_INTERVAL:
                    await self._check_new_contracts()
                    last_contract_check = time.time()

                # 6. Clean up expired cooldown records
                await self._db.cooldowns.cleanup_expired()

                elapsed = time.time() - cycle_start
                logger.info("=== Scan completed in %.1fs, signals %d/%d ===", elapsed, len(filtered), len(signals))

                # Wait for next cycle
                wait_time = max(0, SCAN_INTERVAL - elapsed)
                if self._running and wait_time > 0:
                    await asyncio.sleep(wait_time)
        finally:
            await self._fetcher.close()
            await self._db.close()

    async def _init_symbols(self) -> None:
        """Fetch symbol list from exchange"""
        try:
            adapter = self._fetcher._get_adapter("binance")
            markets = await adapter.fetch_markets()
            # Filter for USDT trading pairs
            self._symbols = [
                m["symbol"] for m in markets
                if m["symbol"].endswith("/USDT") and m.get("active", True)
            ]
            logger.info("Fetched %d USDT symbols", len(self._symbols))
        except Exception:
            logger.exception("Failed to fetch symbol list, using default list")
            self._symbols = MAJOR_SYMBOLS.copy()

    async def _fetch_data(self) -> None:
        """Fetch market data for all symbols"""
        try:
            await self._fetcher.fetch_all(self._symbols, "binance")
        except Exception:
            logger.exception("Data fetch error")

    async def _run_monitors(self) -> list[Signal]:
        """Run four monitor modules in parallel"""
        # Altcoin and ratio scans use full list (excluding major coins)
        alt_symbols = [s for s in self._symbols if s not in MAJOR_SYMBOLS]

        alt_signals, major_signals, ratio_signals = await asyncio.gather(
            self._altcoin_scanner.scan(alt_symbols, "binance"),
            self._major_alert.scan(MAJOR_SYMBOLS, "binance"),
            self._perp_spot.scan(self._symbols, "binance"),
        )

        all_signals = alt_signals + major_signals + ratio_signals
        logger.info(
            "Monitor results: altcoins %d, majors %d, ratio %d",
            len(alt_signals), len(major_signals), len(ratio_signals),
        )
        return all_signals

    async def _check_new_contracts(self) -> None:
        """Detect new contracts"""
        try:
            adapter = self._fetcher._get_adapter("binance")
            new_signals = await self._new_contract.detect(adapter)
            if new_signals:
                filtered = await self._signal_filter.filter(new_signals)
                await self._push_signals(filtered)
        except Exception:
            logger.exception("New contract detection error")

    async def _push_signals(self, signals: list[Signal]) -> None:
        """Push signals to DingTalk"""
        for s in signals:
            try:
                result = await self._notifier.send_signal(s)
                if result.get("errcode") == 0:
                    logger.info("Push success: [%s] %s", s.module, s.symbol)
                else:
                    logger.warning("Push failed: [%s] %s - %s", s.module, s.symbol, result)
            except Exception:
                logger.exception("Push error: [%s] %s", s.module, s.symbol)

    def stop(self) -> None:
        """Stop the scheduler"""
        self._running = False
        logger.info("Stopping scheduler...")


def main() -> None:
    """System entry point"""
    config = load_config()
    setup_logger(log_file="~/.coin_radar/coin_radar.log")

    scheduler = Scheduler(config)

    # Graceful shutdown (Windows doesn't support add_signal_handler, use try-except for Ctrl+C)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # Unix/Linux: register signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, scheduler.stop)
            except (NotImplementedError, OSError):
                # Windows doesn't support add_signal_handler, rely on KeyboardInterrupt
                pass

        loop.run_until_complete(scheduler.run())
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, stopping scheduler...")
        scheduler.stop()
    finally:
        # Cleanup
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


if __name__ == "__main__":
    main()
