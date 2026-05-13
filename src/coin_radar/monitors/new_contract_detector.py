from __future__ import annotations

import logging

from coin_radar.db.database import DatabaseManager
from coin_radar.fetcher.exchange_adapter import ExchangeAdapter
from coin_radar.notifiers.formatter import Signal

logger = logging.getLogger(__name__)


class NewContractDetector:
    """Detect newly launched perpetual contracts on exchanges"""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def detect(self, adapter: ExchangeAdapter) -> list[Signal]:
        """
        Detect new contracts:
        1. Fetch current market list from exchange
        2. Filter for perpetual contracts
        3. Get known contracts from database
        4. Compare to identify new contracts
        5. Store new contracts in database
        6. Generate Signal for each new contract
        """
        markets = await adapter.fetch_markets()
        # Filter for perpetual contracts
        current_perp_symbols = [m["symbol"] for m in markets if self._is_perp_contract(m)]

        # Get known contracts and compare
        known_symbols = await self._db.contracts.get_known_symbols(adapter._exchange_id)
        known_set = set(known_symbols)
        new_symbols = [s for s in current_perp_symbols if s not in known_set]

        signals: list[Signal] = []
        for symbol in new_symbols:
            # Store new contract in database
            await self._db.contracts.add_symbol(symbol, adapter._exchange_id)
            signal = Signal(
                module="New Contract",
                symbol=symbol,
                score=70,
                priority="high",
                details={"exchange": adapter._exchange_id, "contract_type": "perp"},
            )
            signals.append(signal)
            logger.info("Detected new contract: %s", symbol)

        return signals

    @staticmethod
    def _is_perp_contract(market: dict) -> bool:
        """Check if it's a perpetual contract: linear=True or swap type in ccxt"""
        return market.get("linear", False) or market.get("type") == "swap"
