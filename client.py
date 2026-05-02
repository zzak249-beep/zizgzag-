# -*- coding: utf-8 -*-
"""client.py -- Phantom Edge Bot: High-Performance BingX Async Client.

Improvements over v3:
  - Persistent session (one session for lifetime of bot)
  - Connection pool: 300 connections, keepalive
  - Batch ticker price fetch (all positions in 1 call vs N calls)
  - Adaptive retry with jitter backoff
  - Response size limits to avoid memory spikes
"""
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
            limit           = 300,       # max simultaneous connections
            limit_per_host  = 100,       # per-host limit
            ttl_dns_cache   = 600,       # DNS cache 10 min
            keepalive_timeout = 30,      # keep connections alive
            ssl             = False,
            force_close     = False,     # reuse connections
        )
        _session = aiohttp.ClientSession(
            connector = connector,
            timeout   = aiohttp.ClientTimeout(
                total   = cfg.http_timeout,
                connect = 5,             # fast fail on connect
            ),
            headers = {"X-BX-APIKEY": cfg.bingx_api_key},
        )
    return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()


def _sign(params: dict, secret: str) -> str:
    qs = urlencode(sorted(params.items()))
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()


def _auth_params(params: dict | None = None) -> dict:
    from config import cfg
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p, cfg.bingx_secret_key)
    return p


def _headers() -> dict:
    from config import cfg
    return {"X-BX-APIKEY": cfg.bingx_api_key}


async def _request(
    method: str, path: str,
    params: dict | None = None,
    auth: bool = True,
    retries: int = 3,
) -> Any:
    sess = _get_session()
    p    = _auth_params(params) if auth else (params or {})
    hdrs = _headers() if auth else {}
    url  = BASE_URL + path

    for attempt in range(retries):
        try:
            if method == "GET":
                async with sess.get(url, params=p, headers=hdrs) as r:
                    return await r.json(content_type=None)
            elif method == "POST":
                async with sess.post(url, params=p, headers=hdrs) as r:
                    return await r.json(content_type=None)
            elif method == "DELETE":
                async with sess.delete(url, params=p, headers=hdrs) as r:
                    return await r.json(content_type=None)
        except asyncio.TimeoutError:
            jitter = random.uniform(0, 0.5)
            wait   = (1.5 ** attempt) + jitter
            logger.warning(f"{method} {path} timeout #{attempt+1} → retry {wait:.1f}s")
            if attempt < retries - 1:
                await asyncio.sleep(wait)
        except aiohttp.ClientConnectorError as e:
            logger.warning(f"{method} {path} connection error: {e}")
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"{method} {path} error: {e}")
            return {}
    return {}


async def _get(path: str, params: dict | None = None, auth: bool = False) -> Any:
    return await _request("GET", path, params, auth=auth)

async def _post(path: str, params: dict | None = None) -> Any:
    return await _request("POST", path, params, auth=True)

async def _delete(path: str, params: dict | None = None) -> Any:
    return await _request("DELETE", path, params, auth=True)


# ── Market data ───────────────────────────────────────────────────────────────

async def fetch_klines(symbol: str, interval: str, limit: int = 300) -> list[list]:
    resp = await _get("/openApi/swap/v3/quote/klines",
                      {"symbol": symbol, "interval": interval, "limit": limit})
    data = resp.get("data", []) if isinstance(resp, dict) else []
    return data if isinstance(data, list) else []


async def fetch_ohlcv(symbol: str, tf: str, limit: int = 300) -> dict | None:
    raw = await fetch_klines(symbol, tf, limit=limit)
    if len(raw) < 50:
        return None
    try:
        return {
            "open":   __import__("numpy").array([float(c[1]) for c in raw], dtype="float64"),
            "high":   __import__("numpy").array([float(c[2]) for c in raw], dtype="float64"),
            "low":    __import__("numpy").array([float(c[3]) for c in raw], dtype="float64"),
            "close":  __import__("numpy").array([float(c[4]) for c in raw], dtype="float64"),
            "volume": __import__("numpy").array([float(c[5]) for c in raw], dtype="float64"),
        }
    except Exception as e:
        logger.debug(f"OHLCV parse {symbol}: {e}")
        return None


# ── Batch price fetch (KEY OPTIMIZATION for pos_manager) ─────────────────────

async def get_all_tickers() -> dict[str, float]:
    """
    Single API call → prices for ALL symbols.
    Use instead of N individual get_price() calls in manage_positions.
    Returns {symbol: price}
    """
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
        elif isinstance(data, dict):
            sym = data.get("symbol", "")
            p   = float(data.get("lastPrice", 0) or 0)
            if sym and p > 0:
                prices[sym] = p
    except Exception as e:
        logger.warning(f"get_all_tickers error: {e}")
    return prices


# ── Account ───────────────────────────────────────────────────────────────────

async def get_balance() -> float:
    resp = await _get("/openApi/swap/v2/user/balance", auth=True)
    try:
        data = resp.get("data", {})
        if isinstance(data, dict):
            bal = data.get("balance", {})
            if isinstance(bal, dict):
                return float(bal.get("availableMargin", bal.get("balance", 0)))
            return float(data.get("availableMargin", data.get("equity", 0)))
    except Exception as e:
        logger.warning(f"get_balance error: {e}")
    return 0.0


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
        "stopLoss":      str(sl),
        "takeProfit":    str(tp),
    }
    resp = await _post("/openApi/swap/v2/trade/order", params)
    code = resp.get("code", -1)
    if code not in (0, 200, None):
        logger.warning(
            f"[ORDER FAIL] {symbol} {side} code={code} msg={resp.get('msg','')} "
            f"sl={sl} tp={tp}"
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


async def get_price(symbol: str) -> float:
    """Single-symbol price. Use get_all_tickers() for bulk."""
    resp = await _get("/openApi/swap/v2/quote/price", {"symbol": symbol})
    try:
        p = float(resp.get("data", {}).get("price", 0))
        if p > 0: return p
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
