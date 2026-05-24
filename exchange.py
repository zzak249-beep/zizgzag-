"""
BingX Perpetual Futures — REST connector v3.2
FIX: Retry con backoff exponencial para error 100410 (rate limit disabled period)
FIX: Rate limiter interno para no superar ~20 req/s en klines
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import deque
from typing import Optional
from urllib.parse import urlencode

import aiohttp
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)
BASE_URL = "https://open-api.bingx.com"

# ── Rate limiter global ───────────────────────────────────────
# BingX permite ~20 req/s en klines; usamos 8 req/s por seguridad
_KLINES_WINDOW  = 1.0   # segundos
_KLINES_MAX_REQ = 8     # máximo de peticiones por ventana
_klines_times: deque = deque()
_klines_lock = asyncio.Lock()


async def _klines_throttle():
    """Limita las llamadas a klines a _KLINES_MAX_REQ por _KLINES_WINDOW segundos."""
    async with _klines_lock:
        now = time.monotonic()
        # Limpiar timestamps fuera de la ventana
        while _klines_times and now - _klines_times[0] > _KLINES_WINDOW:
            _klines_times.popleft()
        if len(_klines_times) >= _KLINES_MAX_REQ:
            wait = _KLINES_WINDOW - (now - _klines_times[0]) + 0.05
            if wait > 0:
                logger.debug(f"Throttle klines: esperando {wait:.2f}s")
                await asyncio.sleep(wait)
        _klines_times.append(time.monotonic())


class BingXClient:
    def __init__(self, api_key: str, secret: str, paper: bool = True):
        self.api_key = api_key
        self.secret  = secret
        self.paper   = paper
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-BX-APIKEY": self.api_key},
                connector=aiohttp.TCPConnector(limit=50),
            )
        return self._session

    def _sign(self, params: dict) -> str:
        qs = urlencode(sorted(params.items()))
        return hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    async def _get(self, path: str, params: dict = None,
                   retries: int = 4, is_klines: bool = False) -> dict:
        """
        GET con reintentos y backoff.
        Código 100410 = endpoint en "disabled period" → espera larga y reintenta.
        """
        params = params or {}
        if is_klines:
            await _klines_throttle()

        for attempt in range(retries):
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
            sess = await self._get_session()
            try:
                async with sess.get(BASE_URL + path, params=params,
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"BingX network error {path}: {e}")

            code = data.get("code", 0)
            if code == 0:
                return data

            # 100410 = rate limit "disabled period" — esperar más tiempo
            if code == 100410:
                # El timestamp en msg indica cuándo se desbloquea, pero usamos
                # backoff progresivo: 15s, 30s, 60s, 120s
                wait_s = [15, 30, 60, 120][min(attempt, 3)]
                logger.warning(
                    f"[100410] Rate limit en {path} — esperando {wait_s}s "
                    f"(intento {attempt+1}/{retries})"
                )
                await asyncio.sleep(wait_s)
                continue

            # 100001 = timestamp out of sync — reintento rápido
            if code == 100001:
                await asyncio.sleep(1)
                continue

            raise RuntimeError(f"BingX GET error {path}: {data}")

        raise RuntimeError(f"BingX: demasiados reintentos en {path}")

    async def _post(self, path: str, params: dict = None,
                    retries: int = 3) -> dict:
        params = params or {}
        for attempt in range(retries):
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
            sess = await self._get_session()
            try:
                async with sess.post(BASE_URL + path, params=params,
                                     timeout=aiohttp.ClientTimeout(total=15)) as r:
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"BingX POST network error {path}: {e}")

            code = data.get("code", 0)
            if code == 0:
                return data
            if code == 100410:
                wait_s = [15, 30, 60][min(attempt, 2)]
                logger.warning(f"[100410] Rate limit POST {path} — esperando {wait_s}s")
                await asyncio.sleep(wait_s)
                continue
            raise RuntimeError(f"BingX POST error {path}: {data}")

        raise RuntimeError(f"BingX POST: demasiados reintentos en {path}")

    # ── Market data ──────────────────────────────────────────
    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        data = await self._get(
            "/openApi/swap/v2/quote/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
            is_klines=True,
        )
        rows = data.get("data", [])
        if not rows:
            raise RuntimeError(f"No klines data for {symbol}")

        first = rows[0]
        if isinstance(first, dict):
            df = pd.DataFrame(rows)
            df.columns = [c.lower() for c in df.columns]
            rename_map = {
                'opentime': 'open_time', 'o': 'open', 'h': 'high',
                'l': 'low', 'c': 'close', 'v': 'volume',
                'time': 'open_time', 't': 'open_time',
            }
            df = df.rename(columns=rename_map)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col not in df.columns:
                    raise RuntimeError(f"Columna '{col}' no encontrada. Cols: {list(df.columns)}")
        elif isinstance(first, (list, tuple)):
            df = pd.DataFrame(rows, columns=['open_time', 'open', 'high', 'low', 'close', 'volume'])
        else:
            raise RuntimeError(f"Formato klines desconocido: {type(first)}")

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        if 'open_time' in df.columns:
            df['open_time'] = pd.to_numeric(df['open_time'], errors='coerce')
            df.set_index('open_time', inplace=True)
        else:
            df.index = range(len(df))

        df = df[['open', 'high', 'low', 'close', 'volume']].copy()
        df = df[df['close'] > 0].reset_index(drop=True)
        return df

    async def get_ticker(self, symbol: str) -> dict:
        data = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        return data["data"]

    async def get_all_symbols(self) -> list:
        data = await self._get("/openApi/swap/v2/quote/contracts")
        contracts = data.get("data", [])
        return [
            c.get("symbol") or c.get("contractId", "")
            for c in contracts
            if (c.get("symbol") or c.get("contractId", "")) and
               "USDT" in (c.get("symbol") or c.get("contractId", ""))
        ]

    async def get_balance(self) -> float:
        data = await self._get("/openApi/swap/v2/user/balance")
        for asset in data["data"]["balance"]:
            if asset["asset"] == "USDT":
                return float(asset["availableMargin"])
        return 0.0

    async def get_positions(self, symbol: str) -> list:
        data = await self._get("/openApi/swap/v2/user/positions", {"symbol": symbol})
        return data.get("data", [])

    # ── Orders ───────────────────────────────────────────────
    async def place_order(self, symbol, side, position_side, qty,
                          order_type="MARKET", price=None, reduce_only=False) -> dict:
        if self.paper:
            logger.info(f"[PAPER] {side} {position_side} {qty:.6f} {symbol}")
            return {"orderId": f"paper_{int(time.time())}", "paper": True}

        params = {
            "symbol": symbol, "side": side,
            "positionSide": position_side, "type": order_type, "quantity": qty,
        }
        if price and order_type == "LIMIT":
            params["price"] = price
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._post("/openApi/swap/v2/trade/order", params)

    async def set_sl_tp(self, symbol, position_side, sl_price, tp_price, qty) -> dict:
        if self.paper:
            logger.info(f"[PAPER] SL={sl_price:.6f} TP={tp_price:.6f}")
            return {"paper": True}
        sl_side = "SELL" if position_side == "LONG" else "BUY"
        await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": sl_side, "positionSide": position_side,
            "type": "STOP_MARKET", "stopPrice": sl_price,
            "quantity": qty, "reduceOnly": "true",
        })
        await self._post("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": sl_side, "positionSide": position_side,
            "type": "TAKE_PROFIT_MARKET", "stopPrice": tp_price,
            "quantity": qty, "reduceOnly": "true",
        })
        return {"ok": True}

    async def close_position(self, symbol, position_side, qty) -> dict:
        side = "SELL" if position_side == "LONG" else "BUY"
        return await self.place_order(symbol, side, position_side, qty, reduce_only=True)

    async def set_leverage(self, symbol, leverage) -> dict:
        if self.paper:
            return {"paper": True}
        return await self._post("/openApi/swap/v2/trade/leverage",
                                {"symbol": symbol, "side": "LONG", "leverage": leverage})

    async def close(self):
        if self._session:
            await self._session.close()
