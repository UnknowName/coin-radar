from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from coin_radar.config.models import AppConfig, ExchangeConfig
from coin_radar.db.database import DatabaseManager
from coin_radar.db.models import MarketDataRow
from coin_radar.fetcher.exchange_adapter import ExchangeAdapter

logger = logging.getLogger(__name__)

_MAX_CONCURRENCY = 20  # Maximum concurrent symbols across all exchanges (reduced from 50 to avoid connection floods)
_PER_SYMBOL_CONCURRENCY = 1  # Maximum concurrent API calls per symbol (reduced from 2 to serialize requests)


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


@dataclass
class FetchResult:
    symbol: str
    exchange: str
    ohlcv: list[list] | None = None
    ticker: dict | None = None
    funding_rate: dict | None = None
    open_interest: dict | None = None
    order_book: dict | None = None
    perp_ticker: dict | None = None
    long_short_ratio_data: list | None = None
    top_trader_ratio_data: list | None = None


def _build_market_data_row(result: FetchResult) -> MarketDataRow:
    """Convert FetchResult to MarketDataRow"""
    # Extract price data from last OHLCV candle
    open_price = high = low = close = volume = 0.0
    quote_volume = 0.0
    if result.ohlcv:
        last_candle = result.ohlcv[-1]
        # ccxt OHLCV format: [timestamp, open, high, low, close, volume]
        open_price = last_candle[1]
        high = last_candle[2]
        low = last_candle[3]
        close = last_candle[4]
        volume = last_candle[5]

    # Extract quote volume from ticker
    if result.ticker:
        quote_volume = result.ticker.get("quoteVolume", 0.0) or 0.0

    # Extract funding rate from funding_rate
    funding_rate_val = None
    if result.funding_rate:
        funding_rate_val = result.funding_rate.get("fundingRate")

    # Extract open interest from open_interest
    open_interest_val = None
    if result.open_interest:
        open_interest_val = result.open_interest.get("openInterestAmount") or result.open_interest.get("openInterestValue")

    # Extract bid/ask depth from order_book
    bid_depth = ask_depth = None
    if result.order_book:
        bids = result.order_book.get("bids", [])
        asks = result.order_book.get("asks", [])
        bid_depth = sum(b[1] for b in bids) if bids else None
        ask_depth = sum(a[1] for a in asks) if asks else None

    # 从永续合约行情中提取 CVD 和永续成交量
    cvd = None
    perp_volume = None
    if result.perp_ticker:
        perp_volume = result.perp_ticker.get("baseVolume")
        info = result.perp_ticker.get("info", {})
        buy_vol = _safe_float(info.get("takerBuyBaseAssetVolume"))
        total_vol = _safe_float(info.get("volume"))
        if buy_vol is not None and total_vol is not None and total_vol > 0:
            cvd = 2 * buy_vol - total_vol

    # 从现货行情中提取现货成交量
    spot_volume = None
    if result.ticker:
        spot_volume = result.ticker.get("baseVolume")

    # 从 Binance 专用 API 提取多空比
    long_short_ratio = None
    if result.long_short_ratio_data:
        try:
            long_short_ratio = float(result.long_short_ratio_data[0].get("longShortRatio", 0))
        except (IndexError, ValueError, TypeError):
            pass

    top_trader_long_short_ratio = None
    if result.top_trader_ratio_data:
        try:
            top_trader_long_short_ratio = float(result.top_trader_ratio_data[0].get("longShortRatio", 0))
        except (IndexError, ValueError, TypeError):
            pass

    return MarketDataRow(
        symbol=result.symbol,
        exchange=result.exchange,
        timestamp=int(time.time()),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        quote_volume=quote_volume,
        funding_rate=funding_rate_val,
        open_interest=open_interest_val,
        cvd=cvd,
        long_short_ratio=long_short_ratio,
        top_trader_long_short_ratio=top_trader_long_short_ratio,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        perp_volume=perp_volume,
        spot_volume=spot_volume,
    )


class DataFetcher:
    """Async concurrent data fetcher: manages multi-exchange adapters, fetches and persists concurrently"""

    def __init__(self, db: DatabaseManager, config: AppConfig) -> None:
        self._db = db
        self._config = config
        self._adapters: dict[str, ExchangeAdapter] = {}
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        self._symbol_semaphore = asyncio.Semaphore(_PER_SYMBOL_CONCURRENCY)
        self._closed = False
        self._init_adapters()

    def _init_adapters(self) -> None:
        for exchange_config in self._config.exchanges:
            self._adapters[exchange_config.name] = ExchangeAdapter(
                exchange_config.name, exchange_config
            )

    def _get_adapter(self, exchange: str) -> ExchangeAdapter:
        if exchange not in self._adapters:
            raise ValueError(f"Unsupported exchange: {exchange}, available: {list(self._adapters.keys())}")
        return self._adapters[exchange]

    def get_adapter(self, exchange: str) -> ExchangeAdapter:
        return self._get_adapter(exchange)

    async def fetch_single(self, symbol: str, exchange: str = "binance") -> FetchResult:
        adapter = self._get_adapter(exchange)
        async with self._semaphore:
            async with self._symbol_semaphore:
                ohlcv = await adapter.fetch_ohlcv(symbol, timeframe="5m", limit=100)
            async with self._symbol_semaphore:
                ticker = await adapter.fetch_ticker(symbol)
            async with self._symbol_semaphore:
                funding_rate = await adapter.fetch_funding_rate(symbol)
            async with self._symbol_semaphore:
                open_interest = await adapter.fetch_open_interest(symbol)
            async with self._symbol_semaphore:
                order_book = await adapter.fetch_order_book(symbol)
            async with self._symbol_semaphore:
                perp_ticker = await adapter.fetch_perp_ticker(symbol)
            async with self._symbol_semaphore:
                ls_data = await adapter.fetch_long_short_ratio(symbol)
            async with self._symbol_semaphore:
                top_data = await adapter.fetch_top_trader_long_short_ratio(symbol)
        return FetchResult(
            symbol=symbol,
            exchange=exchange,
            ohlcv=ohlcv,
            ticker=ticker,
            funding_rate=funding_rate,
            open_interest=open_interest,
            order_book=order_book,
            perp_ticker=perp_ticker,
            long_short_ratio_data=ls_data,
            top_trader_ratio_data=top_data,
        )

    async def fetch_all(
        self, symbols: list[str], exchange: str = "binance"
    ) -> list[FetchResult]:
        tasks = [self._fetch_and_save(symbol, exchange) for symbol in symbols]
        return await asyncio.gather(*tasks)

    async def _fetch_and_save(self, symbol: str, exchange: str) -> FetchResult:
        result = await self.fetch_single(symbol, exchange)
        try:
            row = _build_market_data_row(result)
            await self._db.market_data.insert(row)
        except Exception:
            logger.exception("Failed to persist data: %s %s", exchange, symbol)
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name, adapter in self._adapters.items():
            try:
                await adapter.close()
            except Exception:
                logger.exception("Failed to close adapter: %s", name)
