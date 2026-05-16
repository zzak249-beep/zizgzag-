"""
bot/binance_client.py
Cliente asíncrono de Binance USDT-M Futures.

Maneja:
  - Configuración de margen ISOLATED y apalancamiento
  - Precisión de precio y cantidad por símbolo
  - Apertura de posición con TP (TAKE_PROFIT_MARKET) y SL (STOP_MARKET)
  - Cierre de posición
  - Consulta de saldo y posición abierta
  - Reconexión automática
"""
import asyncio
import logging
import math
from typing import Optional

from binance import AsyncClient, BinanceAPIException

logger = logging.getLogger(__name__)


class BinanceClient:

    def __init__(self, api_key: str, secret_key: str, testnet: bool = False):
        self.api_key    = api_key
        self.secret_key = secret_key
        self.testnet    = testnet
        self._client: Optional[AsyncClient] = None
        self._exchange_info: dict = {}

    # ─────────────────────────────────────────
    # CONEXIÓN
    # ─────────────────────────────────────────

    async def connect(self) -> None:
        self._client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.secret_key,
            testnet=self.testnet
        )
        logger.info(f"Binance Futures conectado (testnet={self.testnet})")
        await self._load_exchange_info()

    async def disconnect(self) -> None:
        if self._client:
            await self._client.close_connection()

    async def _load_exchange_info(self) -> None:
        info = await self._client.futures_exchange_info()
        for sym in info["symbols"]:
            filters = {f["filterType"]: f for f in sym["filters"]}
            self._exchange_info[sym["symbol"]] = {
                "price_precision": sym["pricePrecision"],
                "qty_precision":   sym["quantityPrecision"],
                "tick_size":       float(filters.get("PRICE_FILTER", {}).get("tickSize", "0.01")),
                "step_size":       float(filters.get("LOT_SIZE",    {}).get("stepSize", "0.001")),
                "min_notional":    float(filters.get("MIN_NOTIONAL", {}).get("notional", "5")),
            }

    def _round_price(self, symbol: str, price: float) -> float:
        info = self._exchange_info.get(symbol, {})
        tick = info.get("tick_size", 0.01)
        return round(math.floor(price / tick) * tick, info.get("price_precision", 2))

    def _round_qty(self, symbol: str, qty: float) -> float:
        info = self._exchange_info.get(symbol, {})
        step = info.get("step_size", 0.001)
        return round(math.floor(qty / step) * step, info.get("qty_precision", 3))

    # ─────────────────────────────────────────
    # DATOS DE MERCADO
    # ─────────────────────────────────────────

    async def get_klines(self, symbol: str, interval: str,
                         limit: int = 300) -> Optional["pd.DataFrame"]:
        import pandas as pd
        try:
            raw = await self._client.futures_klines(
                symbol=symbol, interval=interval, limit=limit
            )
            df = pd.DataFrame(raw, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_vol", "trades", "taker_buy_base",
                "taker_buy_quote", "ignore"
            ])
            df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df.set_index("open_time", inplace=True)
            return df
        except BinanceAPIException as e:
            logger.error(f"get_klines {symbol}: {e}")
            return None

    async def get_balance(self) -> float:
        """Saldo disponible en USDT (wallet balance)."""
        try:
            account = await self._client.futures_account()
            for asset in account["assets"]:
                if asset["asset"] == "USDT":
                    return float(asset["walletBalance"])
            return 0.0
        except BinanceAPIException as e:
            logger.error(f"get_balance: {e}")
            return 0.0

    async def get_position(self, symbol: str) -> Optional[dict]:
        try:
            positions = await self._client.futures_position_information(symbol=symbol)
            for p in positions:
                if p["symbol"] == symbol:
                    size = float(p["positionAmt"])
                    return {
                        "symbol":      symbol,
                        "size":        size,
                        "side":        "LONG" if size > 0 else ("SHORT" if size < 0 else "FLAT"),
                        "entry_price": float(p["entryPrice"]),
                        "unrealized":  float(p["unrealizedProfit"]),
                        "leverage":    int(p["leverage"]),
                    }
            return None
        except BinanceAPIException as e:
            logger.error(f"get_position {symbol}: {e}")
            return None

    # ─────────────────────────────────────────
    # CONFIGURACIÓN DE SÍMBOLO
    # ─────────────────────────────────────────

    async def setup_symbol(self, symbol: str, leverage: int) -> None:
        try:
            await self._client.futures_change_margin_type(
                symbol=symbol, marginType="ISOLATED"
            )
        except BinanceAPIException as e:
            if "No need to change margin type" not in str(e):
                logger.warning(f"Margin type {symbol}: {e}")

        try:
            await self._client.futures_change_leverage(
                symbol=symbol, leverage=leverage
            )
            logger.info(f"{symbol}: leverage={leverage}x ISOLATED configurado")
        except BinanceAPIException as e:
            logger.error(f"setup_symbol {symbol}: {e}")

    # ─────────────────────────────────────────
    # ÓRDENES
    # ─────────────────────────────────────────

    async def open_long(self, symbol: str, qty: float,
                        tp_price: float, sl_price: float) -> Optional[dict]:
        qty = self._round_qty(symbol, qty)
        tp  = self._round_price(symbol, tp_price)
        sl  = self._round_price(symbol, sl_price)

        if qty <= 0:
            logger.warning(f"{symbol}: cantidad inválida {qty}")
            return None

        try:
            # Orden de mercado principal
            entry = await self._client.futures_create_order(
                symbol=symbol, side="BUY",
                type="MARKET", quantity=qty
            )
            logger.info(f"{symbol} LONG abierto qty={qty}")

            # TP
            await self._client.futures_create_order(
                symbol=symbol, side="SELL",
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp,
                closePosition=True,
                timeInForce="GTE_GTC"
            )
            # SL
            await self._client.futures_create_order(
                symbol=symbol, side="SELL",
                type="STOP_MARKET",
                stopPrice=sl,
                closePosition=True,
                timeInForce="GTE_GTC"
            )
            return {"order": entry, "tp": tp, "sl": sl, "qty": qty, "side": "LONG"}

        except BinanceAPIException as e:
            logger.error(f"open_long {symbol}: {e}")
            return None

    async def open_short(self, symbol: str, qty: float,
                         tp_price: float, sl_price: float) -> Optional[dict]:
        qty = self._round_qty(symbol, qty)
        tp  = self._round_price(symbol, tp_price)
        sl  = self._round_price(symbol, sl_price)

        if qty <= 0:
            logger.warning(f"{symbol}: cantidad inválida {qty}")
            return None

        try:
            entry = await self._client.futures_create_order(
                symbol=symbol, side="SELL",
                type="MARKET", quantity=qty
            )
            logger.info(f"{symbol} SHORT abierto qty={qty}")

            await self._client.futures_create_order(
                symbol=symbol, side="BUY",
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp,
                closePosition=True,
                timeInForce="GTE_GTC"
            )
            await self._client.futures_create_order(
                symbol=symbol, side="BUY",
                type="STOP_MARKET",
                stopPrice=sl,
                closePosition=True,
                timeInForce="GTE_GTC"
            )
            return {"order": entry, "tp": tp, "sl": sl, "qty": qty, "side": "SHORT"}

        except BinanceAPIException as e:
            logger.error(f"open_short {symbol}: {e}")
            return None

    async def close_position(self, symbol: str, position: dict) -> Optional[dict]:
        """Cierre de mercado (usado para barrera de tiempo)."""
        size = abs(position["size"])
        if size == 0:
            return None
        qty  = self._round_qty(symbol, size)
        side = "SELL" if position["side"] == "LONG" else "BUY"
        try:
            # Cancelar órdenes pendientes primero
            await self._client.futures_cancel_all_open_orders(symbol=symbol)
            order = await self._client.futures_create_order(
                symbol=symbol, side=side,
                type="MARKET", quantity=qty,
                reduceOnly=True
            )
            logger.info(f"{symbol} posición cerrada por tiempo")
            return order
        except BinanceAPIException as e:
            logger.error(f"close_position {symbol}: {e}")
            return None

    async def get_last_trade_pnl(self, symbol: str) -> float:
        """PnL de la última operación cerrada."""
        try:
            trades = await self._client.futures_account_trades(
                symbol=symbol, limit=5
            )
            if trades:
                return float(trades[-1].get("realizedPnl", 0))
            return 0.0
        except BinanceAPIException as e:
            logger.error(f"get_last_trade_pnl {symbol}: {e}")
            return 0.0
