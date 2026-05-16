"""
Exchange Layer — BingX via CCXT
Handles: OHLCV fetch, order placement, position tracking, balance.
"""
import logging
import asyncio
from typing import Optional, Dict, List
import ccxt.async_support as ccxt
import pandas as pd
from config.settings import Settings

logger = logging.getLogger(__name__)


class ExchangeClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._ex: Optional[ccxt.bingx] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self):
        self._ex = ccxt.bingx({
            "apiKey":  self.s.BINGX_API_KEY,
            "secret":  self.s.BINGX_API_SECRET,
            "options": {"defaultType": "swap"},   # perpetual futures
            "enableRateLimit": True,
        })
        await self._ex.load_markets()
        logger.info(f"✅ Connected to BingX | {len(self._ex.markets)} markets loaded")

    async def close(self):
        if self._ex:
            await self._ex.close()

    # ── Market data ───────────────────────────────────────────────────────────

    async def fetch_ohlcv(self,
                          symbol: str,
                          timeframe: str,
                          limit: int = 300) -> Optional[pd.DataFrame]:
        """Returns OHLCV DataFrame or None on error."""
        try:
            raw = await self._ex.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not raw:
                return None
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df.astype(float)
        except Exception as exc:
            logger.warning(f"OHLCV fetch error [{symbol} {timeframe}]: {exc}")
            return None

    async def fetch_ticker(self, symbol: str) -> Optional[Dict]:
        try:
            return await self._ex.fetch_ticker(symbol)
        except Exception as exc:
            logger.warning(f"Ticker fetch error [{symbol}]: {exc}")
            return None

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Returns USDT free balance."""
        try:
            bal = await self._ex.fetch_balance()
            return float(bal.get("USDT", {}).get("free", 0) or 0)
        except Exception as exc:
            logger.error(f"Balance fetch error: {exc}")
            return 0.0

    async def get_positions(self) -> List[Dict]:
        """All open perpetual positions."""
        try:
            positions = await self._ex.fetch_positions()
            return [p for p in positions if float(p.get("contracts", 0) or 0) != 0]
        except Exception as exc:
            logger.error(f"Positions fetch error: {exc}")
            return []

    async def get_open_orders(self, symbol: str) -> List[Dict]:
        try:
            return await self._ex.fetch_open_orders(symbol)
        except Exception as exc:
            logger.warning(f"Open orders fetch error [{symbol}]: {exc}")
            return []

    # ── Order execution ───────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int):
        try:
            await self._ex.set_leverage(leverage, symbol)
            logger.info(f"Leverage set: {symbol} x{leverage}")
        except Exception as exc:
            logger.warning(f"Leverage set error [{symbol}]: {exc}")

    async def open_position(self,
                             symbol: str,
                             direction: str,
                             size_pct: float,
                             entry_price: float,
                             tp_price: float,
                             sl_price: float,
                             capital: float) -> Optional[Dict]:
        """
        Places a market entry + TP/SL OCO.
        size_pct: percentage of capital to allocate.
        Returns order dict or None.
        """
        try:
            side = "buy" if direction == "LONG" else "sell"
            pos_value = capital * (size_pct / 100) * self.s.LEVERAGE
            amount = pos_value / entry_price

            # Round to exchange precision
            market = self._ex.market(symbol)
            amount = self._ex.amount_to_precision(symbol, amount)

            await self.set_leverage(symbol, self.s.LEVERAGE)

            # Market entry
            order = await self._ex.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=float(amount),
                params={"positionSide": "LONG" if direction == "LONG" else "SHORT"},
            )
            logger.info(f"✅ Entry order placed: {symbol} {direction} qty={amount}")

            # TP order
            tp_side = "sell" if direction == "LONG" else "buy"
            await self._ex.create_order(
                symbol=symbol,
                type="limit",
                side=tp_side,
                amount=float(amount),
                price=tp_price,
                params={
                    "positionSide": "LONG" if direction == "LONG" else "SHORT",
                    "reduceOnly": True,
                },
            )

            # SL order (stop-market)
            await self._ex.create_order(
                symbol=symbol,
                type="stop_market",
                side=tp_side,
                amount=float(amount),
                price=sl_price,
                params={
                    "stopPrice": sl_price,
                    "positionSide": "LONG" if direction == "LONG" else "SHORT",
                    "reduceOnly": True,
                },
            )

            logger.info(f"✅ TP={tp_price} SL={sl_price} set for {symbol}")
            return order

        except Exception as exc:
            logger.error(f"Order placement failed [{symbol}]: {exc}")
            return None

    async def close_position(self, symbol: str, direction: str, amount: float) -> bool:
        """Market close entire position."""
        try:
            side = "sell" if direction == "LONG" else "buy"
            await self._ex.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=amount,
                params={
                    "positionSide": "LONG" if direction == "LONG" else "SHORT",
                    "reduceOnly": True,
                },
            )
            logger.info(f"✅ Position closed: {symbol}")
            return True
        except Exception as exc:
            logger.error(f"Close position failed [{symbol}]: {exc}")
            return False

    async def cancel_all_orders(self, symbol: str):
        """Cancel all open TP/SL orders for a symbol."""
        try:
            await self._ex.cancel_all_orders(symbol)
        except Exception as exc:
            logger.warning(f"Cancel orders error [{symbol}]: {exc}")

    async def move_sl_to_breakeven(self, symbol: str, direction: str,
                                    entry: float, amount: float):
        """Cancel current SL and place new one at entry (breakeven)."""
        try:
            await self.cancel_all_orders(symbol)
            be_side = "sell" if direction == "LONG" else "buy"
            await self._ex.create_order(
                symbol=symbol,
                type="stop_market",
                side=be_side,
                amount=amount,
                price=entry,
                params={
                    "stopPrice": entry,
                    "positionSide": "LONG" if direction == "LONG" else "SHORT",
                    "reduceOnly": True,
                },
            )
            logger.info(f"🔒 SL moved to breakeven {entry} for {symbol}")
        except Exception as exc:
            logger.warning(f"Breakeven SL error [{symbol}]: {exc}")
