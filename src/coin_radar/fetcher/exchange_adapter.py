from __future__ import annotations

import logging

import ccxt.async_support as ccxt_async
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from coin_radar.config.models import ExchangeConfig
from coin_radar.fetcher.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger(__name__)

# ccxt network error base class for retry judgment
_NetworkError = (
    ccxt_async.NetworkError,
    ccxt_async.ExchangeNotAvailable,
    ccxt_async.RequestTimeout,
)

# DNS/connection errors that should be retried
_DNSError = (
    ccxt_async.NetworkError,
    ccxt_async.ExchangeNotAvailable,
)

# Generic retry strategy: 3 retries, exponential backoff
_retry_decorator = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    retry=retry_if_exception_type(_NetworkError) | retry_if_exception_type(_DNSError),
    reraise=True,
)


class ExchangeAdapter:
    """Exchange adapter: wraps ccxt async calls with built-in rate limiting and retry"""

    def __init__(self, exchange_id: str, config: ExchangeConfig) -> None:
        self._exchange_id = exchange_id
        self._limiter = TokenBucketRateLimiter(rate=10.0, capacity=20)

        exchange_class = getattr(ccxt_async, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unsupported exchange: {exchange_id}, available: {[attr for attr in dir(ccxt_async) if not attr.startswith('_')]}")

        options: dict = {"timeout": config.timeout * 1000}
        if config.proxy:
            options["proxies"] = {
                "http": config.proxy,
                "https": config.proxy,
            }
            options["aiohttp_proxy"] = config.proxy

        self._exchange: ccxt_async.Exchange = exchange_class(options)
        logger.info("Initialized exchange adapter: %s", exchange_id)

    @_retry_decorator
    async def _call(self, fn, *args, **kwargs):
        try:
            await self._limiter.acquire()
            return await fn(*args, **kwargs)
        except _NetworkError as e:
            # Check if it's a DNS/connection error
            error_msg = str(e).lower()
            if 'dns' in error_msg or 'could not contact' in error_msg or 'cannot connect' in error_msg:
                logger.error(
                    "DNS/Network error for %s. "
                    "Possible causes: 1) Network connection issue, "
                    "2) DNS server problem, or "
                    "3) Exchange API blocked (may need proxy). "
                    "Please check network settings or configure a proxy.",
                    self._exchange_id
                )
            else:
                logger.error("Network error for %s: %s", self._exchange_id, e)
            raise
        except Exception as e:
            logger.error("Unexpected error for %s: %s", self._exchange_id, e)
            raise

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "5m", limit: int = 100
    ) -> list[list] | None:
        try:
            return await self._call(self._exchange.fetch_ohlcv, symbol, timeframe, limit=limit)
        except Exception:
            logger.warning("fetch_ohlcv failed: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_ticker(self, symbol: str) -> dict | None:
        try:
            return await self._call(self._exchange.fetch_ticker, symbol)
        except Exception:
            logger.warning("fetch_ticker failed: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict | None:
        try:
            return await self._call(self._exchange.fetch_tickers, symbols)
        except Exception:
            logger.warning("fetch_tickers failed: %s", self._exchange_id)
            return None

    async def fetch_funding_rate(self, symbol: str) -> dict | None:
        # Only futures market supports funding rate
        if not self._exchange.has.get("fetchFundingRate"):
            return None
        try:
            return await self._call(self._exchange.fetch_funding_rate, symbol)
        except Exception:
            logger.debug("fetch_funding_rate failed: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_open_interest(self, symbol: str) -> dict | None:
        if not self._exchange.has.get("fetchOpenInterest"):
            return None
        try:
            return await self._call(self._exchange.fetch_open_interest, symbol)
        except Exception:
            logger.debug("fetch_open_interest failed: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict | None:
        try:
            return await self._call(self._exchange.fetch_order_book, symbol, limit)
        except Exception:
            logger.warning("fetch_order_book failed: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_markets(self) -> list[dict]:
        try:
            return await self._call(self._exchange.fetch_markets)
        except Exception:
            logger.error("fetch_markets failed: %s", self._exchange_id)
            return []

    async def fetch_perp_ticker(self, symbol: str) -> dict | None:
        # 获取永续合约行情（用于 CVD 和永续成交量）
        if ':' in symbol:
            perp_symbol = symbol
        else:
            perp_symbol = symbol + ':USDT'
        try:
            return await self._call(self._exchange.fetch_ticker, perp_symbol)
        except Exception:
            logger.debug("fetch_perp_ticker failed: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_long_short_ratio(self, symbol: str) -> list | None:
        # 获取全局多空比（Binance 专用 API）
        method_name = 'fapiPublicGetGlobalLongShortAccountRatio'
        if not hasattr(self._exchange, method_name):
            return None
        try:
            pair = symbol.replace('/', '').replace(':', '')
            return await self._call(
                getattr(self._exchange, method_name),
                {'symbol': pair, 'period': '5m', 'limit': 1},
            )
        except Exception:
            logger.debug("fetch_long_short_ratio failed: %s %s", self._exchange_id, symbol)
            return None

    async def fetch_top_trader_long_short_ratio(self, symbol: str) -> list | None:
        # 获取大户多空比（Binance 专用 API）
        method_name = 'fapiPublicGetTopLongShortAccountRatio'
        if not hasattr(self._exchange, method_name):
            return None
        try:
            pair = symbol.replace('/', '').replace(':', '')
            return await self._call(
                getattr(self._exchange, method_name),
                {'symbol': pair, 'period': '5m', 'limit': 1},
            )
        except Exception:
            logger.debug("fetch_top_trader_long_short_ratio failed: %s %s", self._exchange_id, symbol)
            return None

    async def close(self) -> None:
        await self._exchange.close()
