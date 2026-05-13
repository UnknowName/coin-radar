from __future__ import annotations

import logging

import ccxt.async_support as ccxt_async
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from coin_radar.config.models import ExchangeConfig
from coin_radar.fetcher.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger(__name__)

# ccxt 网络错误基类，用于重试判断
_NetworkError = (
    ccxt_async.NetworkError,
    ccxt_async.ExchangeNotAvailable,
    ccxt_async.RequestTimeout,
)

# 通用重试策略：3次重试，指数退避
_retry_decorator = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    retry=retry_if_exception_type(_NetworkError),
    reraise=True,
)


class ExchangeAdapter:
    """交易所适配器：封装 ccxt 异步调用，内置限流和重试"""

    def __init__(self, exchange_id: str, config: ExchangeConfig) -> None:
        self._exchange_id = exchange_id
        self._limiter = TokenBucketRateLimiter(rate=10.0, capacity=20)

        exchange_class = getattr(ccxt_async, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"不支持的交易所: {exchange_id}")

        options: dict = {"timeout": config.timeout * 1000}
        if config.proxy:
            options["proxies"] = {
                "http": config.proxy,
                "https": config.proxy,
            }
            options["aiohttp_proxy"] = config.proxy

        self._exchange: ccxt_async.Exchange = exchange_class(options)

    @_retry_decorator
    async def _call(self, fn, *args, **kwargs):
        await self._limiter.acquire()
        return await fn(*args, **kwargs)

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "5m", limit: int = 100
    ) -> list[list] | None:
        try:
            return await self._call(self._exchange.fetch_ohlcv, symbol, timeframe, limit=limit)
        except Exception:
            logger.warning("fetch_ohlcv 失败: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_ticker(self, symbol: str) -> dict | None:
        try:
            return await self._call(self._exchange.fetch_ticker, symbol)
        except Exception:
            logger.warning("fetch_ticker 失败: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict | None:
        try:
            return await self._call(self._exchange.fetch_tickers, symbols)
        except Exception:
            logger.warning("fetch_tickers 失败: %s", self._exchange_id)
            return None

    async def fetch_funding_rate(self, symbol: str) -> dict | None:
        # 仅合约市场支持资金费率
        if not self._exchange.has.get("fetchFundingRate"):
            return None
        try:
            return await self._call(self._exchange.fetch_funding_rate, symbol)
        except Exception:
            logger.debug("fetch_funding_rate 失败: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_open_interest(self, symbol: str) -> dict | None:
        if not self._exchange.has.get("fetchOpenInterest"):
            return None
        try:
            return await self._call(self._exchange.fetch_open_interest, symbol)
        except Exception:
            logger.debug("fetch_open_interest 失败: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict | None:
        try:
            return await self._call(self._exchange.fetch_order_book, symbol, limit)
        except Exception:
            logger.warning("fetch_order_book 失败: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_markets(self) -> list[dict]:
        try:
            return await self._call(self._exchange.fetch_markets)
        except Exception:
            logger.warning("fetch_markets 失败: %s", self._exchange_id)
            return []

    async def close(self) -> None:
        await self._exchange.close()
