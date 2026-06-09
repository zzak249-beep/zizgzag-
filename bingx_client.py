"""
BingX Perpetual Futures client — market data + order execution.
API docs: https://bingx-api.github.io/docs/#/en-us/swapV2/
"""
import hashlib
import hmac
import time
import logging
from urllib.parse import urlencode
from typing import Optional
import httpx

log = logging.getLogger("qfjp.bingx")

# BingX timeframe map
TF_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1H", "2h": "2H", "4h": "4H",
    "1d": "1D", "1w": "1W",
}


class BingXClient:
    def __init__(self, api_key: str, secret: str, base_url: str = "https://open-api.bingx.com"):
        self.api_key  = api_key
        self.secret   = secret
        self.base_url = base_url
        self._http    = httpx.AsyncClient(
            base_url=base_url,
            timeout=20.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self.last_qty: float = 0.0

    # ── Auth helpers ───────────────────────────────────────────────────────
    def _ts(self) -> int:
        return int(time.time() * 1000)

    def _sign(self, params: dict) -> str:
        qs = urlencode(sorted(params.items()))
        return hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    def _auth_headers(self) -> dict:
        return {"X-BX-APIKEY": self.api_key, "Content-Type": "application/json"}

    # ── Market data ────────────────────────────────────────────────────────
    async def get_all_symbols(self, min_volume: float = 0) -> list[str]:
        """Returns perpetual symbols sorted by 24h volume, filtered by min_volume."""
        try:
            r = await self._http.get("/openApi/swap/v2/quote/ticker")
            r.raise_for_status()
            tickers = r.json().get("data", [])
            filtered = [
                t["symbol"]
                for t in tickers
                if float(t.get("quoteVolume", 0)) >= min_volume
                and t["symbol"].endswith("-USDT")
            ]
            # Sort by volume descending
            vol_map = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers}
            filtered.sort(key=lambda s: vol_map.get(s, 0), reverse=True)
            return filtered
        except Exception as exc:
            log.error(f"get_all_symbols failed: {exc}")
            return []

    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[dict]:
        """Returns OHLCV list newest-last: [{ts, open, high, low, close, volume}, ...]"""
        tf = TF_MAP.get(interval, interval)
        try:
            r = await self._http.get(
                "/openApi/swap/v3/quote/klines",
                params={"symbol": symbol, "interval": tf, "limit": limit},
            )
            r.raise_for_status()
            raw = r.json().get("data", [])
            candles = []
            for c in raw:
                candles.append({
                    "ts":     int(c[0]),
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                })
            candles.sort(key=lambda x: x["ts"])
            return candles
        except Exception as exc:
            log.debug(f"get_klines {symbol} {interval}: {exc}")
            return []

    async def get_price(self, symbol: str) -> float:
        try:
            r = await self._http.get(
                "/openApi/swap/v2/quote/price", params={"symbol": symbol}
            )
            r.raise_for_status()
            return float(r.json()["data"]["price"])
        except Exception as exc:
            log.error(f"get_price {symbol}: {exc}")
            return 0.0

    async def get_contract_info(self, symbol: str) -> Optional[dict]:
        try:
            r = await self._http.get("/openApi/swap/v2/quote/contracts")
            r.raise_for_status()
            for c in r.json().get("data", []):
                if c["symbol"] == symbol:
                    return c
        except Exception as exc:
            log.error(f"get_contract_info {symbol}: {exc}")
        return None

    async def get_balance(self) -> float:
        params = {"timestamp": self._ts()}
        params["signature"] = self._sign(params)
        try:
            r = await self._http.get(
                "/openApi/swap/v2/user/balance",
                params=params, headers=self._auth_headers(),
            )
            r.raise_for_status()
            return float(r.json()["data"]["balance"]["availableMargin"])
        except Exception as exc:
            log.error(f"get_balance: {exc}")
            return 0.0

    async def get_open_positions(self) -> list[dict]:
        params = {"timestamp": self._ts()}
        params["signature"] = self._sign(params)
        try:
            r = await self._http.get(
                "/openApi/swap/v2/user/positions",
                params=params, headers=self._auth_headers(),
            )
            r.raise_for_status()
            return [p for p in r.json().get("data", []) if float(p.get("positionAmt", 0)) != 0]
        except Exception as exc:
            log.error(f"get_open_positions: {exc}")
            return []

    # ── Set leverage ───────────────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int, side: str) -> bool:
        params = {"symbol": symbol, "side": side, "leverage": leverage, "timestamp": self._ts()}
        params["signature"] = self._sign(params)
        try:
            r = await self._http.post(
                "/openApi/swap/v2/trade/leverage",
                params=params, headers=self._auth_headers(),
            )
            r.raise_for_status()
            return True
        except Exception as exc:
            log.warning(f"set_leverage {symbol}: {exc}")
            return False

    # ── Place order ────────────────────────────────────────────────────────
    async def place_order(self, signal: dict, risk, price: float) -> Optional[dict]:
        symbol   = signal["symbol"]
        side     = signal["side"]   # LONG | SHORT
        leverage = risk.settings.LEVERAGE

        # 1. Leverage
        await self.set_leverage(symbol, leverage, side)

        # 2. Contract info for qty precision
        info    = await self.get_contract_info(symbol)
        ct_size = float(info["contractSize"]) if info and "contractSize" in info else 1.0
        qty     = risk.compute_qty(price, ct_size)
        qty     = max(round(qty, 2), 0.01)
        self.last_qty = qty

        # 3. ATR-based SL/TP
        atr = signal.get("atr", price * 0.003)
        if side == "LONG":
            sl_price  = round(price - atr * risk.settings.SL_MULT,  6)
            tp1_price = round(price + atr * risk.settings.TP1_MULT, 6)
            tp2_price = round(price + atr * risk.settings.TP2_MULT, 6)
            bx_side   = "BUY"
        else:
            sl_price  = round(price + atr * risk.settings.SL_MULT,  6)
            tp1_price = round(price - atr * risk.settings.TP1_MULT, 6)
            tp2_price = round(price - atr * risk.settings.TP2_MULT, 6)
            bx_side   = "SELL"

        rr1 = round(abs(tp1_price - price) / max(abs(sl_price - price), 0.000001), 2)

        # 4. Market entry
        ep = {"symbol": symbol, "side": bx_side, "positionSide": side,
              "type": "MARKET", "quantity": qty, "timestamp": self._ts()}
        ep["signature"] = self._sign(ep)
        try:
            r = await self._http.post(
                "/openApi/swap/v2/trade/order", params=ep, headers=self._auth_headers()
            )
            r.raise_for_status()
            order = r.json()["data"]["order"]
            order_id = order["orderId"]
            log.info(f"✅ ENTRY {side} {qty} {symbol} @ ~{price} | ID:{order_id}")
        except Exception as exc:
            log.error(f"Entry order failed {symbol}: {exc}")
            return None

        close_side = "SELL" if side == "LONG" else "BUY"

        # 5. Stop-loss
        sl_p = {"symbol": symbol, "side": close_side, "positionSide": side,
                "type": "STOP_MARKET", "stopPrice": sl_price,
                "closePosition": "true", "timestamp": self._ts()}
        sl_p["signature"] = self._sign(sl_p)
        try:
            await self._http.post("/openApi/swap/v2/trade/order", params=sl_p, headers=self._auth_headers())
            log.info(f"   SL placed @ {sl_price}")
        except Exception as exc:
            log.warning(f"   SL failed: {exc} — SET MANUALLY!")

        # 6. Take-profit TP1
        tp_p = {"symbol": symbol, "side": close_side, "positionSide": side,
                "type": "TAKE_PROFIT_MARKET", "stopPrice": tp1_price,
                "closePosition": "true", "timestamp": self._ts()}
        tp_p["signature"] = self._sign(tp_p)
        try:
            await self._http.post("/openApi/swap/v2/trade/order", params=tp_p, headers=self._auth_headers())
            log.info(f"   TP1 placed @ {tp1_price}")
        except Exception as exc:
            log.warning(f"   TP1 failed: {exc}")

        order["price"]     = price
        order["sl_price"]  = sl_price
        order["tp1_price"] = tp1_price
        order["tp2_price"] = tp2_price
        order["origQty"]   = qty
        order["rr1"]       = rr1
        return order

    async def cancel_all_orders(self, symbol: str) -> None:
        params = {"symbol": symbol, "timestamp": self._ts()}
        params["signature"] = self._sign(params)
        try:
            await self._http.delete(
                "/openApi/swap/v2/trade/allOpenOrders",
                params=params, headers=self._auth_headers(),
            )
        except Exception as exc:
            log.warning(f"cancel_all_orders {symbol}: {exc}")
