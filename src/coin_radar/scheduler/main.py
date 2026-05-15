from __future__ import annotations

import asyncio
import logging
import signal
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

SCAN_INTERVAL = 300
CONTRACT_CHECK_INTERVAL = 3600


# Common USDT symbols fallback list for major exchanges
_FALLBACK_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "TRX/USDT", "LINK/USDT",
    "MATIC/USDT", "DOT/USDT", "UNI/USDT", "LTC/USDT", "ATOM/USDT",
    "ETC/USDT", "NEAR/USDT", "XLM/USDT", "BCH/USDT", "FIL/USDT",
]


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
        self._symbols_by_exchange: dict[str, list[str]] = {}

    async def run(self) -> None:
        """Main execution loop"""
        self._running = True
        try:
            await self._db.connect()
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
        """Fetch symbol list from all configured exchanges"""
        for name in self._config.exchange_names:
            try:
                adapter = self._fetcher.get_adapter(name)
                markets = await adapter.fetch_markets()
                symbols = [
                    m["symbol"] for m in markets
                    if m["symbol"].endswith("/USDT") and m.get("active", True)
                ]
                if not symbols:
                    # Empty symbol list indicates network failure
                    logger.warning("No symbols fetched from %s (empty list), using fallback", name)
                    self._symbols_by_exchange[name] = _FALLBACK_SYMBOLS.copy()
                else:
                    self._symbols_by_exchange[name] = symbols
                    logger.info("Fetched %d USDT symbols from %s", len(symbols), name)
            except Exception as e:
                logger.exception("Failed to fetch symbols from %s", name)
                # Use fallback symbols when network fails
                logger.warning("Using fallback symbol list for %s due to network error", name)
                self._symbols_by_exchange[name] = _FALLBACK_SYMBOLS.copy()

    async def _fetch_data(self) -> None:
        """Fetch market data from all exchanges concurrently"""
        tasks = []
        exchange_names = []
        for exchange, symbols in self._symbols_by_exchange.items():
            tasks.append(self._fetcher.fetch_all(symbols, exchange))
            exchange_names.append(exchange)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(exchange_names, results):
            if isinstance(result, Exception):
                logger.exception("Data fetch error for %s", name)

    async def _run_monitors(self) -> list[Signal]:
        """Run monitor modules on all exchanges concurrently and aggregate signals"""
        monitor_tasks = []
        exchange_keys = []
        for exchange, symbols in self._symbols_by_exchange.items():
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
        """Run three monitor modules on a single exchange"""
        logger.info(
            "[%s] Symbol split: total=%d | majors=%d(%s) | altcoins=%d",
            exchange, len(all_symbols),
            len(MAJOR_SYMBOLS), ",".join(MAJOR_SYMBOLS), len(alt_symbols),
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

    async def _check_new_contracts(self) -> None:
        """Detect new contracts (requirement 3.2.3.1 only for Binance)"""
        try:
            # Prefer binance, if not configured use first exchange
            contract_exchange = "binance" if "binance" in self._config.exchange_names else self._config.exchange_names[0]
            adapter = self._fetcher.get_adapter(contract_exchange)
            new_signals = await self._new_contract.detect(adapter)
            if new_signals:
                filtered = await self._signal_filter.filter(new_signals)
                await self._push_signals(filtered)
        except Exception:
            logger.exception("New contract detection error")

    async def _push_signals(self, signals: list[Signal]) -> None:
        """Push signals to DingTalk in batch, with fallback notification on failure"""
        if not signals:
            return

        try:
            result = await self._notifier.send_signals_batch(signals)
            if result.get("errcode") == 0:
                sent_count = result.get("sent_count", len(signals))
                logger.info("Batch push success: %d signals sent", sent_count)
            else:
                logger.warning("Batch push failed: %s", result)
                await self._fallback_notify(signals, result.get("errmsg", "unknown error"))
        except Exception as e:
            logger.exception("Batch push error")
            await self._fallback_notify(signals, str(e))

    async def _fallback_notify(self, signals: list[Signal], error_msg: str) -> None:
        # 备用通知机制：控制台高亮输出 + 写入本地通知日志文件
        logger.warning("=== Fallback notification (DingTalk failed: %s) ===", error_msg)
        for s in signals:
            logger.warning(
                "[FALLBACK] %s | %s | score=%.0f | priority=%s | price=%s",
                s.module, s.symbol, s.score, s.priority,
                f"{s.price:.2f}" if s.price else "N/A",
            )
        try:
            import os
            log_dir = os.path.expanduser("~/.coin_radar")
            os.makedirs(log_dir, exist_ok=True)
            fallback_path = os.path.join(log_dir, "fallback_notifications.log")
            with open(fallback_path, "a", encoding="utf-8") as f:
                import datetime
                ts = datetime.datetime.now().isoformat()
                f.write(f"\n[{ts}] DingTalk failed: {error_msg}\n")
                for s in signals:
                    f.write(
                        f"  {s.module} | {s.symbol} | score={s.score:.0f} | "
                        f"priority={s.priority} | price={s.price}\n"
                    )
        except Exception:
            logger.exception("Fallback notification file write failed")

    def stop(self) -> None:
        """Stop the scheduler"""
        self._running = False
        logger.info("Stopping scheduler...")


def main() -> None:
    """System entry point"""
    config = load_config()
    setup_logger(log_file="~/.coin_radar/coin_radar.log")

    scheduler = Scheduler(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, scheduler.stop)
            except (NotImplementedError, OSError):
                pass

        loop.run_until_complete(scheduler.run())
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, stopping scheduler...")
        scheduler.stop()
    finally:
        cleanup_event_loop(loop)


if __name__ == "__main__":
    main()
