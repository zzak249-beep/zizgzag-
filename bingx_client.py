"""
bingx_client.py — Cliente para la API de BingX Perpetual Swap (Futuros).

Documentación oficial: https://bingx-api.github.io/docs/
"""

from __future__ import annotations
import hashlib
import hmac
import json
import logging
import time
from typing import Dict, Optional
from urllib.parse import urlencode

import requests

import config

logger = logging.getLogger(__name__)

BASE_URL = config.BINGX_BASE_URL


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de firma
# ─────────────────────────────────────────────────────────────────────────────

def _timestamp() -> str:
    return str(int(time.time() * 1000))


def _sign(secret: str, payload: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _signed_request(
    method: str,
    path:   str,
    params: Optional[Dict] = None,
    body:   Optional[Dict] = None,
) -> Dict:
    """
    Realiza una petición firmada a la API de BingX.
    """
    params = params or {}
    params["timestamp"] = _timestamp()

    # Construir la cadena a firmar
    query_string = urlencode(params)
    if body:
        body_string  = json.dumps(body, separators=(",", ":"))
        sign_payload = query_string + body_string
    else:
        body_string  = ""
        sign_payload = query_string

    params["signature"] = _sign(config.BINGX_SECRET_KEY, sign_payload)

    headers = {
        "X-BX-APIKEY": config.BINGX_API_KEY,
        "Content-Type": "application/json",
    }

    url = f"{BASE_URL}{path}?{urlencode(params)}"

    try:
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        elif method.upper() == "POST":
            resp = requests.post(url, headers=headers, data=body_string, timeout=10)
        elif method.upper() == "DELETE":
            resp = requests.delete(url, headers=headers, timeout=10)
        else:
            raise ValueError(f"Método HTTP no soportado: {method}")

        resp.raise_for_status()
        data = resp.json()

        if data.get("code") not in (0, "0", None):
            raise RuntimeError(f"BingX error {data.get('code')}: {data.get('msg')}")

        return data

    except requests.RequestException as e:
        logger.error("Error HTTP en BingX: %s", e)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Datos de mercado
# ─────────────────────────────────────────────────────────────────────────────

def get_klines(symbol: str, interval: str, limit: int = 100) -> list:
    """
    Obtiene velas históricas.
    interval: "1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"
    Retorna lista de dicts: {open, high, low, close, volume, timestamp}
    """
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }
    # Endpoint público (no requiere firma)
    url = f"{BASE_URL}/openApi/swap/v2/quote/klines"
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") not in (0, "0", None):
        raise RuntimeError(f"BingX klines error: {data}")

    candles = []
    for item in data.get("data", []):
        candles.append({
            "timestamp": item[0],
            "open":      float(item[1]),
            "high":      float(item[2]),
            "low":       float(item[3]),
            "close":     float(item[4]),
            "volume":    float(item[5]),
        })

    # Ordenar por timestamp ascendente (más antigua primero)
    candles.sort(key=lambda x: x["timestamp"])
    return candles


def get_ticker(symbol: str) -> Dict:
    """Precio actual del símbolo."""
    url    = f"{BASE_URL}/openApi/swap/v2/quote/ticker"
    params = {"symbol": symbol}
    resp   = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


def get_balance() -> Dict:
    """Balance de la cuenta de futuros."""
    data = _signed_request("GET", "/openApi/swap/v2/user/balance")
    return data.get("data", {})


# ─────────────────────────────────────────────────────────────────────────────
# Gestión de posiciones y órdenes
# ─────────────────────────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int) -> Dict:
    """Establece el apalancamiento para el símbolo."""
    data = _signed_request(
        "POST",
        "/openApi/swap/v2/trade/leverage",
        body={"symbol": symbol, "side": "LONG",  "leverage": leverage},
    )
    _signed_request(
        "POST",
        "/openApi/swap/v2/trade/leverage",
        body={"symbol": symbol, "side": "SHORT", "leverage": leverage},
    )
    return data


def get_open_positions(symbol: str) -> list:
    """Posiciones abiertas para el símbolo."""
    data = _signed_request(
        "GET",
        "/openApi/swap/v2/user/positions",
        params={"symbol": symbol},
    )
    positions = data.get("data", [])
    # Filtrar posiciones con cantidad > 0
    return [p for p in positions if float(p.get("positionAmt", 0)) != 0]


def get_open_orders(symbol: str) -> list:
    """Órdenes abiertas (pendientes)."""
    data = _signed_request(
        "GET",
        "/openApi/swap/v2/trade/openOrders",
        params={"symbol": symbol},
    )
    return data.get("data", {}).get("orders", [])


def place_market_order(
    symbol:   str,
    side:     str,      # "BUY" | "SELL"
    quantity: float,
    tp_price: float,
    sl_price: float,
    position_side: str = "BOTH",  # "BOTH" | "LONG" | "SHORT"
) -> Dict:
    """
    Abre una orden de mercado con TP y SL automáticos.
    """
    body = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": position_side,
        "type":         "MARKET",
        "quantity":     str(round(quantity, 6)),
        "takeProfit":   json.dumps({
            "type":       "TAKE_PROFIT_MARKET",
            "stopPrice":  str(tp_price),
            "workingType": "MARK_PRICE",
        }),
        "stopLoss": json.dumps({
            "type":       "STOP_MARKET",
            "stopPrice":  str(sl_price),
            "workingType": "MARK_PRICE",
        }),
    }
    logger.info("Colocando orden: %s", json.dumps(body))
    data = _signed_request("POST", "/openApi/swap/v2/trade/order", body=body)
    return data.get("data", {})


def close_position(symbol: str, position_side: str = "BOTH") -> Dict:
    """Cierra la posición abierta para el símbolo."""
    data = _signed_request(
        "POST",
        "/openApi/swap/v2/trade/closePosition",
        body={"symbol": symbol, "positionSide": position_side},
    )
    return data.get("data", {})


def cancel_all_orders(symbol: str) -> Dict:
    """Cancela todas las órdenes abiertas del símbolo."""
    data = _signed_request(
        "DELETE",
        "/openApi/swap/v2/trade/allOpenOrders",
        params={"symbol": symbol},
    )
    return data.get("data", {})


def get_trade_history(symbol: str, limit: int = 20) -> list:
    """Historial de operaciones cerradas."""
    data = _signed_request(
        "GET",
        "/openApi/swap/v2/trade/allFillOrders",
        params={"symbol": symbol, "limit": limit},
    )
    return data.get("data", {}).get("fill_orders", [])


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────────────────────

def calculate_quantity(capital_usdt: float, price: float, leverage: int) -> float:
    """
    Calcula la cantidad de contratos a abrir.
    cantidad = (capital × leverage) / precio
    """
    notional = capital_usdt * leverage
    return notional / price


def pip_value(symbol: str) -> float:
    """
    Retorna el valor de 1 pip para el símbolo.
    Para pares de crypto (BTC-USDT), 1 pip = 0.1 USDT (ajusta según el símbolo).
    Para pares de Forex podría ser 0.0001.
    """
    if "BTC" in symbol:
        return 1.0      # 1 pip = $1 para BTC
    elif "ETH" in symbol:
        return 0.1
    else:
        return 0.0001   # Forex
