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

_SUPPORTED_EXCHANGES = ("binance", "okx", "bybit")
_MAX_CONCURRENCY = 10


@dataclass
class FetchResult:
    symbol: str
    exchange: str
    ohlcv: list[list] | None = None
    ticker: dict | None = None
    funding_rate: dict | None = None
    open_interest: dict | None = None
    order_book: dict | None = None


def _build_market_data_row(result: FetchResult) -> MarketDataRow:
    """将 FetchResult 转换为 MarketDataRow"""
    # 从 OHLCV 最后一根K线提取价格数据
    open_price = high = low = close = volume = 0.0
    quote_volume = 0.0
    if result.ohlcv:
        last_candle = result.ohlcv[-1]
        # ccxt OHLCV 格式: [timestamp, open, high, low, close, volume]
        open_price = last_candle[1]
        high = last_candle[2]
        low = last_candle[3]
        close = last_candle[4]
        volume = last_candle[5]

    # 从 ticker 提取成交额
    if result.ticker:
        quote_volume = result.ticker.get("quoteVolume", 0.0) or 0.0

    # 从 funding_rate 提取资金费率
    funding_rate_val = None
    if result.funding_rate:
        funding_rate_val = result.funding_rate.get("fundingRate")

    # 从 open_interest 提取未平仓量
    open_interest_val = None
    if result.open_interest:
        open_interest_val = result.open_interest.get("openInterestAmount") or result.open_interest.get("openInterestValue")

    # 从 order_book 提取买卖深度
    bid_depth = ask_depth = None
    if result.order_book:
        bids = result.order_book.get("bids", [])
        asks = result.order_book.get("asks", [])
        bid_depth = sum(b[1] for b in bids) if bids else None
        ask_depth = sum(a[1] for a in asks) if asks else None

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
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )


class DataFetcher:
    """异步并发数据获取调度器：管理多交易所适配器，并发获取并持久化"""

    def __init__(self, db: DatabaseManager, config: AppConfig) -> None:
        self._db = db
        self._config = config
        self._adapters: dict[str, ExchangeAdapter] = {}
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        self._init_adapters()

    def _init_adapters(self) -> None:
        for name in _SUPPORTED_EXCHANGES:
            exchange_config = ExchangeConfig(
                name=name,
                proxy=self._config.exchange.proxy,
                timeout=self._config.exchange.timeout,
            )
            self._adapters[name] = ExchangeAdapter(name, exchange_config)

    def _get_adapter(self, exchange: str) -> ExchangeAdapter:
        if exchange not in self._adapters:
            raise ValueError(f"不支持的交易所: {exchange}，可选: {list(_SUPPORTED_EXCHANGES)}")
        return self._adapters[exchange]

    async def fetch_single(self, symbol: str, exchange: str = "binance") -> FetchResult:
        adapter = self._get_adapter(exchange)
        async with self._semaphore:
            ohlcv, ticker, funding_rate, open_interest, order_book = await asyncio.gather(
                adapter.fetch_ohlcv(symbol),
                adapter.fetch_ticker(symbol),
                adapter.fetch_funding_rate(symbol),
                adapter.fetch_open_interest(symbol),
                adapter.fetch_order_book(symbol),
            )
        return FetchResult(
            symbol=symbol,
            exchange=exchange,
            ohlcv=ohlcv,
            ticker=ticker,
            funding_rate=funding_rate,
            open_interest=open_interest,
            order_book=order_book,
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
            logger.exception("数据持久化失败: %s %s", exchange, symbol)
        return result

    async def close(self) -> None:
        for adapter in self._adapters.values():
            await adapter.close()
