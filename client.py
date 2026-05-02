# -*- coding: utf-8 -*-
"""client.py -- Phantom Edge Bot: BingX Perpetual Futures Client (FIXED SIGNATURE)."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import random
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp
from loguru import logger

BASE_URL = "https://open-api.bingx.com"
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        from config import cfg
        connector = aiohttp.TCPConnector(
            limit=200, limit_per_host=80,
            ttl_dns_cache=300, ssl=False,
        )
        _session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=cfg.http_timeout, connect=5),
        )
    return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()


def _sign(params: dict, secret: str) -> str:
    """
    BingX signature: HMAC-SHA256 of sorted query string.
    CRITICAL: sort keys alphabetically, urlencode, then sign.
    """
    sorted_params = sorted(params.items())
    query_string  = urlencode(sorted_params)
    signature     = hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return signature


def _build_params(extra: dict | None = None) -> dict:
    from config import cfg
    p = dict(extra or {})
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p, cfg.bingx_secret_key)
    return p


def _headers() -> dict:
    from config import cfg
    return {
        "X-BX-APIKEY": cfg.bingx_api_key,
        "Content-Type": "application/json",
    }


async def _request(method: str, path: str,
                   params: dict | None = None,
                   auth: bool = True,
                   retries: int = 3) -> Any:
    sess    = _get_session()
    p       = _build_params(params) if auth else (params or {})
    headers = _headers()
    url     = BASE_URL + path

    for attempt in range(retries):
        try:
            if method == "GET":
                async with sess.get(url, params=p, headers=headers) as r:
                    data = await r.json(content_type=None)
                    if isinstance(data, dict) and data.get("code") not in (0, 200, None):
                        msg = data.get("msg", "")
                        if "Signature" in msg or "signature" in msg:
                            logger.error(f"[FIRMA] {path}: {msg}")
                            logger.error("Verifica BINGX_API_KEY y BINGX_SECRET_KEY en Railway Variables")
                        elif attempt == 0:
                            logger.debug(f"API {path} code={data.get('code')} {msg[:80]}")
                    return data
            elif method == "POST":
                async with sess.post(url, params=p, headers=headers) as r:
                    return await r.json(content_type=None)
            elif method == "DELETE":
                async with sess.delete(url, params=p, headers=headers) as r:
                    return await r.json(content_type=None)
        except asyncio.TimeoutError:
            wait = (1.5 ** attempt) + random.uniform(0, 0.5)
            logger.warning(f"{method} {path} timeout #{attempt+1} retry {wait:.1f}s")
            if attempt < retries - 1:
                await asyncio.sleep(wait)
        except Exception as e:
            logger.warning(f"{method} {path} error: {e}")
            return {}
    return {}


async def _get(path, params=None, auth=False):
    return await _request("GET", path, params, auth=auth)

async def _post(path, params=None):
    return await _request("POST", path, params, auth=True)

async def _delete(path, params=None):
    return await _request("DELETE", path, params, auth=True)


# ── Market data ───────────────────────────────────────────────────────────────

async def fetch_klines(symbol: str, interval: str, limit: int = 300) -> list[list]:
    resp = await _get("/openApi/swap/v3/quote/klines",
                      {"symbol": symbol, "interval": interval, "limit": limit})
    data = resp.get("data", []) if isinstance(resp, dict) else []
    return data if isinstance(data, list) else []


async def fetch_ohlcv(symbol: str, tf: str, limit: int = 300) -> dict | None:
    import numpy as np
    raw = await fetch_klines(symbol, tf, limit=limit)
    if len(raw) < 50:
        return None
    try:
        return {
            "open":   np.array([float(c[1]) for c in raw], dtype=np.float64),
            "high":   np.array([float(c[2]) for c in raw], dtype=np.float64),
            "low":    np.array([float(c[3]) for c in raw], dtype=np.float64),
            "close":  np.array([float(c[4]) for c in raw], dtype=np.float64),
            "volume": np.array([float(c[5]) for c in raw], dtype=np.float64),
        }
    except Exception as e:
        logger.debug(f"OHLCV parse {symbol}: {e}")
        return None


# ── Account ───────────────────────────────────────────────────────────────────

async def get_balance() -> float:
    """Fetch available USDT balance from BingX perpetual futures account."""
    resp = await _get("/openApi/swap/v2/user/balance", auth=True)
    try:
        code = resp.get("code", -1)
        if code not in (0, 200, None):
            logger.warning(f"get_balance API error code={code}: {resp.get('msg','')}")
            return 0.0
        data = resp.get("data", {})
        # BingX returns nested: data.balance.availableMargin
        if isinstance(data, dict):
            bal = data.get("balance", {})
            if isinstance(bal, dict):
                v = bal.get("availableMargin") or bal.get("available") or bal.get("balance") or 0
                return float(v)
            # Flat structure fallback
            for key in ("availableMargin", "available", "equity", "balance"):
                if key in data:
                    return float(data[key])
    except Exception as e:
        logger.warning(f"get_balance parse error: {e} | resp: {str(resp)[:200]}")
    return 0.0


async def get_all_tickers() -> dict[str, float]:
    """Single call → prices for ALL symbols."""
    resp = await _get("/openApi/swap/v2/quote/ticker")
    prices: dict[str, float] = {}
    try:
        data = resp.get("data", [])
        if isinstance(data, list):
            for item in data:
                sym = item.get("symbol", "")
                p   = float(item.get("lastPrice", 0) or 0)
                if sym and p > 0:
                    prices[sym] = p
    except Exception as e:
        logger.warning(f"get_all_tickers error: {e}")
    return prices


async def get_all_positions() -> dict[str, dict]:
    resp = await _get("/openApi/swap/v2/user/positions", auth=True)
    try:
        data = resp.get("data", [])
        if isinstance(data, list):
            return {
                p["symbol"]: p for p in data
                if abs(float(p.get("positionAmt", 0))) > 1e-9
            }
    except Exception as e:
        logger.warning(f"get_positions error: {e}")
    return {}


async def get_price(symbol: str) -> float:
    resp = await _get("/openApi/swap/v2/quote/price", {"symbol": symbol})
    try:
        p = float(resp.get("data", {}).get("price", 0))
        if p > 0:
            return p
    except Exception:
        pass
    resp2 = await _get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    try:
        d = resp2.get("data", {})
        if isinstance(d, list) and d:
            return float(d[0].get("lastPrice", 0))
        if isinstance(d, dict):
            return float(d.get("lastPrice", 0))
    except Exception:
        pass
    return 0.0


# ── Trading ───────────────────────────────────────────────────────────────────

async def set_leverage(symbol: str, leverage: int) -> Any:
    await _post("/openApi/swap/v2/trade/leverage",
                {"symbol": symbol, "side": "LONG",  "leverage": leverage})
    return await _post("/openApi/swap/v2/trade/leverage",
                       {"symbol": symbol, "side": "SHORT", "leverage": leverage})


async def place_market_order(symbol: str, side: str, size_usdt: float,
                             sl: float, tp: float) -> dict:
    params: dict[str, Any] = {
        "symbol":        symbol,
        "side":          side,
        "type":          "MARKET",
        "quoteOrderQty": size_usdt,
        "stopLoss":      str(round(sl, 8)),
        "takeProfit":    str(round(tp, 8)),
    }
    resp = await _post("/openApi/swap/v2/trade/order", params)
    code = resp.get("code", -1)
    if code not in (0, 200, None):
        logger.warning(
            f"[ORDER FAIL] {symbol} {side} code={code} "
            f"msg={resp.get('msg','')} sl={sl} tp={tp}"
        )
    return resp if isinstance(resp, dict) else {"raw": resp}


async def place_reduce_order(symbol: str, side: str, quantity: float) -> dict:
    resp = await _post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": side, "type": "MARKET",
        "quantity": quantity, "reduceOnly": "true",
    })
    return resp if isinstance(resp, dict) else {}


async def close_position(symbol: str, position: dict) -> Any:
    resp = await _post("/openApi/swap/v2/trade/closePosition", {"symbol": symbol})
    if resp.get("code", -1) in (0, 200):
        return resp
    amt = float(position.get("positionAmt", 0))
    if abs(amt) < 1e-9:
        return {}
    return await _post("/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side":   "SELL" if amt > 0 else "BUY",
        "type":   "MARKET",
        "quantity": abs(amt),
    })


async def cancel_all_orders(symbol: str) -> Any:
    return await _delete("/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol})
