"""
bingx_client.py — Cliente BingX Perpetual Swap (Futuros).
Soporta: obtener todos los símbolos, klines, órdenes, posiciones.
"""
from __future__ import annotations
import hashlib, hmac, json, logging, time
from typing import Dict, List, Optional
from urllib.parse import urlencode
import requests
import config

logger = logging.getLogger(__name__)
BASE   = config.BINGX_BASE_URL


# ── Firma ──────────────────────────────────────────────────────────────────

def _ts() -> str:
    return str(int(time.time() * 1000))

def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

def _req(method: str, path: str, params: dict = None, body: dict = None) -> Dict:
    params = dict(params or {})
    params["timestamp"] = _ts()
    qs = urlencode(params)
    bs = json.dumps(body, separators=(",",":")) if body else ""
    params["signature"] = _sign(config.BINGX_SECRET_KEY, qs + bs)
    headers = {"X-BX-APIKEY": config.BINGX_API_KEY, "Content-Type": "application/json"}
    url = f"{BASE}{path}?{urlencode(params)}"
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            r = requests.post(url, headers=headers, data=bs, timeout=10)
        elif method == "DELETE":
            r = requests.delete(url, headers=headers, timeout=10)
        else:
            raise ValueError(method)
        r.raise_for_status()
        data = r.json()
        if data.get("code") not in (0, "0", None):
            raise RuntimeError(f"BingX {data.get('code')}: {data.get('msg')}")
        return data
    except requests.RequestException as e:
        logger.error("HTTP error BingX %s %s: %s", method, path, e)
        raise


# ── Mercado público ────────────────────────────────────────────────────────

def get_all_symbols() -> List[Dict]:
    """
    Retorna todos los contratos perpetuos de BingX (USDT).
    Cada item: {symbol, lastPrice, volume24h, ...}
    """
    url  = f"{BASE}/openApi/swap/v2/quote/contracts"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    contracts = data.get("data", [])
    # Filtrar solo pares USDT activos
    return [c for c in contracts if str(c.get("currency","")).upper() == "USDT"
            or str(c.get("symbol","")).endswith("-USDT")]


def get_tickers_all() -> List[Dict]:
    """Ticker de todos los símbolos (precio, volumen 24h)."""
    url  = f"{BASE}/openApi/swap/v2/quote/ticker"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    tickers = data.get("data", [])
    if isinstance(tickers, dict):
        tickers = [tickers]
    return tickers


def get_top_symbols_by_volume(max_n: int = 30, min_vol: float = 5_000_000) -> List[str]:
    """
    Retorna los top N símbolos USDT ordenados por volumen 24h (mayor a menor).
    Filtra por volumen mínimo para evitar pares ilíquidos.
    """
    try:
        tickers = get_tickers_all()
    except Exception as e:
        logger.error("Error obteniendo tickers: %s", e)
        return []

    usdt = []
    for t in tickers:
        sym = t.get("symbol","")
        if not sym.endswith("-USDT"):
            continue
        try:
            vol = float(t.get("quoteVolume", t.get("volume", 0)))
            price = float(t.get("lastPrice", t.get("price", 0)))
            if vol >= min_vol and price > 0:
                usdt.append((sym, vol))
        except (ValueError, TypeError):
            continue

    usdt.sort(key=lambda x: x[1], reverse=True)
    result = [s for s, _ in usdt[:max_n]]
    logger.info("Símbolos seleccionados (%d): %s", len(result), result[:10])
    return result


def get_klines(symbol: str, interval: str, limit: int = 110) -> List[Dict]:
    """Velas históricas. interval: '15' para 15 minutos."""
    url    = f"{BASE}/openApi/swap/v2/quote/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp   = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data   = resp.json()
    if data.get("code") not in (0, "0", None):
        raise RuntimeError(f"klines error: {data}")
    candles = []
    for item in (data.get("data") or []):
        try:
            candles.append({
                "ts":    int(item[0]),
                "open":  float(item[1]),
                "high":  float(item[2]),
                "low":   float(item[3]),
                "close": float(item[4]),
                "vol":   float(item[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue
    candles.sort(key=lambda x: x["ts"])
    return candles


def get_ticker(symbol: str) -> Dict:
    url    = f"{BASE}/openApi/swap/v2/quote/ticker"
    resp   = requests.get(url, params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


# ── Cuenta ─────────────────────────────────────────────────────────────────

def get_balance() -> Dict:
    data = _req("GET", "/openApi/swap/v2/user/balance")
    balances = data.get("data", {})
    # Buscar balance USDT
    if isinstance(balances, list):
        for b in balances:
            if b.get("asset","").upper() == "USDT":
                return b
        return balances[0] if balances else {}
    return balances


def get_open_positions(symbol: str = None) -> List[Dict]:
    params = {}
    if symbol:
        params["symbol"] = symbol
    data = _req("GET", "/openApi/swap/v2/user/positions", params=params)
    positions = data.get("data", [])
    return [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]


def get_all_open_positions() -> List[Dict]:
    return get_open_positions()


# ── Trading ────────────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int) -> None:
    for side in ("LONG", "SHORT"):
        try:
            _req("POST", "/openApi/swap/v2/trade/leverage",
                 body={"symbol": symbol, "side": side, "leverage": leverage})
        except Exception as e:
            logger.debug("set_leverage %s %s: %s", symbol, side, e)


def place_market_order(
    symbol:   str,
    side:     str,       # "BUY" | "SELL"
    quantity: float,
    tp_price: float,
    sl_price: float,
) -> Dict:
    body = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": "BOTH",
        "type":         "MARKET",
        "quantity":     str(round(quantity, 6)),
        "takeProfit": json.dumps({
            "type":        "TAKE_PROFIT_MARKET",
            "stopPrice":   str(round(tp_price, 6)),
            "workingType": "MARK_PRICE",
        }),
        "stopLoss": json.dumps({
            "type":        "STOP_MARKET",
            "stopPrice":   str(round(sl_price, 6)),
            "workingType": "MARK_PRICE",
        }),
    }
    data = _req("POST", "/openApi/swap/v2/trade/order", body=body)
    return data.get("data", {})


def close_position_market(symbol: str) -> Dict:
    """Cierra toda la posición abierta en un símbolo a mercado."""
    positions = get_open_positions(symbol)
    if not positions:
        return {}
    pos   = positions[0]
    amt   = float(pos.get("positionAmt", 0))
    side  = "SELL" if amt > 0 else "BUY"
    body  = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": "BOTH",
        "type":         "MARKET",
        "quantity":     str(abs(round(amt, 6))),
    }
    data = _req("POST", "/openApi/swap/v2/trade/order", body=body)
    return data.get("data", {})


# ── Historial ──────────────────────────────────────────────────────────────

def get_closed_orders(symbol: str, limit: int = 10) -> List[Dict]:
    try:
        data = _req("GET", "/openApi/swap/v2/trade/allFillOrders",
                    params={"symbol": symbol, "limit": limit})
        return data.get("data", {}).get("fill_orders", [])
    except Exception:
        return []


# ── Utilidades ─────────────────────────────────────────────────────────────

def pip_size(symbol: str) -> float:
    """Valor de 1 pip según el símbolo."""
    s = symbol.upper()
    if "BTC"  in s: return 1.0
    if "ETH"  in s: return 0.1
    if "BNB"  in s: return 0.01
    if "SOL"  in s: return 0.01
    if "XRP"  in s: return 0.0001
    if "DOGE" in s: return 0.00001
    if "PEPE" in s: return 0.0000001
    return 0.0001  # default


def calc_quantity(capital_usdt: float, price: float, leverage: int) -> float:
    """Cantidad de contratos = (capital × leverage) / precio."""
    if price <= 0:
        return 0
    return (capital_usdt * leverage) / price
