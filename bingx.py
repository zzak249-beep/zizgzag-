"""
Cliente BingX Perpetual Futures — v2
Endpoints: https://bingx-api.github.io/docs/#/en-us/swapV2/
"""
import hashlib
import hmac
import time
import urllib.parse
import asyncio
import aiohttp
import logging

logger = logging.getLogger("bingx")
BASE = "https://open-api.bingx.com"

# Cache global de contratos (se llena una sola vez)
_contracts_cache: dict[str, dict] = {}


class BingXClient:
    def __init__(self, api_key: str, secret: str):
        self.api_key = api_key
        self.secret  = secret
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-BX-APIKEY": self.api_key},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, params: dict) -> str:
        params["timestamp"] = int(time.time() * 1000)
        qs  = urllib.parse.urlencode(sorted(params.items()))
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return qs + "&signature=" + sig

    async def _get(self, path: str, params: dict | None = None, signed: bool = False) -> dict | list:
        p = dict(params or {})
        qs  = self._sign(p) if signed else (urllib.parse.urlencode(p) if p else "")
        url = f"{BASE}{path}?{qs}" if qs else f"{BASE}{path}"
        s   = await self._get_session()
        for attempt in range(3):
            try:
                async with s.get(url) as r:
                    data = await r.json(content_type=None)
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2)
        code = data.get("code", 0)
        if code != 0:
            raise RuntimeError(f"GET {path} [{code}]: {data.get('msg', data)}")
        return data["data"]

    async def _post(self, path: str, params: dict) -> dict:
        qs = self._sign(dict(params))
        s  = await self._get_session()
        for attempt in range(3):
            try:
                async with s.post(
                    f"{BASE}{path}", data=qs,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as r:
                    data = await r.json(content_type=None)
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2)
        code = data.get("code", 0)
        if code != 0:
            raise RuntimeError(f"POST {path} [{code}]: {data.get('msg', data)}")
        return data["data"]

    # ─── Mercado ──────────────────────────────────────────────────────────────

    async def all_usdt_symbols(self) -> list[str]:
        """TODOS los pares -USDT en futuros perpetuos, ordenados por volumen 24h."""
        data = await self._get("/openApi/swap/v2/quote/ticker")
        usdt = [t for t in data if t["symbol"].endswith("-USDT")]
        usdt.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
        symbols = [t["symbol"] for t in usdt]
        logger.info(f"Pares USDT disponibles: {len(symbols)}")
        return symbols

    async def last_price(self, symbol: str) -> float:
        data   = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        ticker = data[0] if isinstance(data, list) else data
        return float(ticker["lastPrice"])

    async def klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        """Velas OHLCV ordenadas antiguo→reciente. Keys: o h l c v t"""
        raw = await self._get("/openApi/swap/v3/quote/klines", {
            "symbol": symbol, "interval": interval, "limit": limit,
        })
        candles = [
            {
                "o": float(c["open"]),  "h": float(c["high"]),
                "l": float(c["low"]),   "c": float(c["close"]),
                "v": float(c["volume"]), "t": int(c["time"]),
            }
            for c in raw
        ]
        candles.sort(key=lambda x: x["t"])
        return candles

    async def load_contracts_cache(self):
        """Precarga metadatos de contratos (step size, min qty…)."""
        global _contracts_cache
        data = await self._get("/openApi/swap/v2/quote/contracts")
        for s in data:
            _contracts_cache[s["symbol"]] = s
        logger.info(f"Contratos en cache: {len(_contracts_cache)}")

    def get_step_size(self, symbol: str) -> float:
        return float(_contracts_cache.get(symbol, {}).get("tradeMinQuantity", 0.001))

    # ─── Cuenta ───────────────────────────────────────────────────────────────

    async def balance_usdt(self) -> float:
        data = await self._get("/openApi/swap/v2/user/balance", signed=True)
        for asset in data.get("balance", []):
            if asset["asset"] == "USDT":
                return float(asset["availableMargin"])
        return 0.0

    async def get_open_positions(self) -> list[dict]:
        """Posiciones abiertas reales en la exchange."""
        try:
            data = await self._get("/openApi/swap/v2/user/positions", signed=True)
            return [p for p in (data or []) if abs(float(p.get("positionAmt", 0))) > 0]
        except Exception as e:
            logger.warning(f"get_open_positions: {e}")
            return []

    async def get_realized_pnl(self, symbol: str) -> float:
        """PnL realizado de las últimas órdenes cerradas de un símbolo."""
        try:
            data = await self._get("/openApi/swap/v2/trade/allOrders", {
                "symbol": symbol, "limit": 5,
            }, signed=True)
            orders = data if isinstance(data, list) else data.get("orders", [])
            total = sum(float(o.get("realizedPnl", 0)) for o in orders
                        if o.get("status") in ("FILLED", "PARTIALLY_FILLED"))
            return total
        except Exception as e:
            logger.warning(f"get_realized_pnl {symbol}: {e}")
            return 0.0

    async def set_leverage(self, symbol: str, leverage: int = 10):
        for side in ("LONG", "SHORT"):
            try:
                await self._post("/openApi/swap/v2/trade/leverage", {
                    "symbol": symbol, "side": side, "leverage": leverage,
                })
            except RuntimeError as e:
                if "same leverage" not in str(e).lower():
                    logger.warning(f"set_leverage {symbol}/{side}: {e}")

    # ─── Trading ─────────────────────────────────────────────────────────────

    def _round_qty(self, qty: float, step: float) -> float:
        if step <= 0:
            return round(qty, 6)
        return int(qty / step) * step

    async def open_order(
        self,
        symbol: str,
        side: str,
        usdt_amount: float,
        tp_price: float,
        sl_price: float,
        leverage: int = 10,
    ) -> tuple[str, float, float]:
        """
        Configura leverage, abre MARKET, coloca TP y SL.
        Retorna (order_id, qty, entry_price).
        """
        await self.set_leverage(symbol, leverage)

        entry = await self.last_price(symbol)
        step  = self.get_step_size(symbol)
        qty   = self._round_qty((usdt_amount * leverage) / entry, step)

        if qty <= 0:
            raise ValueError(f"Qty inválida {symbol}: usdt={usdt_amount} price={entry} step={step}")

        action = "BUY"  if side == "LONG" else "SELL"
        close  = "SELL" if side == "LONG" else "BUY"

        # Orden principal
        resp     = await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": action, "positionSide": side,
            "type": "MARKET", "quantity": qty,
        })
        order_id = resp["order"]["orderId"]

        # Take Profit
        try:
            await self._post("/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": close, "positionSide": side,
                "type": "TAKE_PROFIT_MARKET", "quantity": qty,
                "stopPrice": f"{tp_price:.8f}", "workingType": "MARK_PRICE",
                "reduceOnly": "true",
            })
        except Exception as e:
            logger.warning(f"TP {symbol}: {e}")

        # Stop Loss
        try:
            await self._post("/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": close, "positionSide": side,
                "type": "STOP_MARKET", "quantity": qty,
                "stopPrice": f"{sl_price:.8f}", "workingType": "MARK_PRICE",
                "reduceOnly": "true",
            })
        except Exception as e:
            logger.warning(f"SL {symbol}: {e}")

        logger.info(f"✅ Abierto {symbol} {side} qty={qty:.4f} @ {entry:.6f} TP={tp_price:.6f} SL={sl_price:.6f}")
        return order_id, qty, entry

    async def close_position(self, symbol: str, side: str, qty: float) -> float:
        """Cierra con MARKET reduceOnly. Retorna precio de salida."""
        close = "SELL" if side == "LONG" else "BUY"
        await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": close, "positionSide": side,
            "type": "MARKET", "quantity": qty, "reduceOnly": "true",
        })
        return await self.last_price(symbol)

    async def cancel_all_orders(self, symbol: str):
        try:
            await self._post("/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol})
        except Exception as e:
            logger.warning(f"cancel_all_orders {symbol}: {e}")
