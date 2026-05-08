"""
Cliente BingX Futures API v3
- HMAC-SHA256 authentication
- Market orders con SL/TP integrado
- Soporte todas las monedas perpetuos USDT
"""
import os
import time
import hmac
import hashlib
import urllib.parse
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_API_SECRET = os.environ["BINGX_API_SECRET"]
BASE_URL         = "https://open-api.bingx.com"

TIMEOUT = httpx.Timeout(10.0)


def _sign(params: dict, secret: str) -> str:
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(
        secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _headers() -> dict:
    return {
        "X-BX-APIKEY": BINGX_API_KEY,
        "Content-Type": "application/json",
    }


async def _get(path: str, params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params, BINGX_API_SECRET)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(BASE_URL + path, params=params, headers=_headers())
    r.raise_for_status()
    data = r.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"BingX error: {data.get('msg')} (code {data.get('code')})")
    return data


async def _post(path: str, body: dict) -> dict:
    body["timestamp"] = int(time.time() * 1000)
    body["signature"] = _sign(body, BINGX_API_SECRET)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(BASE_URL + path, params=body, headers=_headers())
    r.raise_for_status()
    data = r.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"BingX error: {data.get('msg')} (code {data.get('code')})")
    return data


async def obtener_precio(simbolo: str) -> float:
    """Precio mark actual del perpetuo."""
    data = await _get(
        "/openApi/swap/v2/quote/price",
        {"symbol": simbolo}
    )
    return float(data["data"]["price"])


async def set_apalancamiento(simbolo: str, apalancamiento: int, lado: str = "LONG"):
    """Establece apalancamiento antes de operar."""
    try:
        await _post("/openApi/swap/v2/trade/leverage", {
            "symbol": simbolo,
            "side": lado,
            "leverage": apalancamiento,
        })
    except Exception as e:
        log.warning(f"No se pudo ajustar apalancamiento: {e}")


async def ejecutar_orden(simbolo: str, orden: dict) -> dict:
    """
    Ejecuta orden market con SL y TP adjuntos.
    BingX soporta stopLoss y takeProfit en la misma llamada.
    """
    lado = "BUY" if orden["accion"] == "BUY" else "SELL"
    pos_lado = "LONG" if lado == "BUY" else "SHORT"

    # Ajustar apalancamiento primero
    apalancamiento = int(os.environ.get("APALANCAMIENTO", "10"))
    await set_apalancamiento(simbolo, apalancamiento, pos_lado)

    params = {
        "symbol":           simbolo,
        "side":             lado,
        "positionSide":     pos_lado,
        "type":             "MARKET",
        "quoteOrderQty":    orden["cantidad_usdt"],  # USDT amount
        "stopLoss":         str(orden["stop_loss"]),
        "takeProfit":       str(orden["take_profit"]),
        "stopLossWorkingType":   "MARK_PRICE",
        "takeProfitWorkingType": "MARK_PRICE",
    }

    log.info(f"Enviando orden: {params}")
    resultado = await _post("/openApi/swap/v2/trade/order", params)
    log.info(f"Orden ejecutada: {resultado}")
    return resultado


async def cerrar_posicion(simbolo: str, lado: str) -> dict:
    """Cierre market de posición existente."""
    lado_cierre = "SELL" if lado == "BUY" else "BUY"
    pos_lado    = "LONG" if lado == "BUY" else "SHORT"

    # Obtener cantidad de la posición abierta
    try:
        pos_data = await _get("/openApi/swap/v2/user/positions", {"symbol": simbolo})
        posiciones = pos_data.get("data", [])
        cantidad = 0.0
        for p in posiciones:
            if p.get("positionSide") == pos_lado:
                cantidad = abs(float(p.get("positionAmt", 0)))
                break
    except Exception as e:
        log.error(f"No se pudo obtener posición: {e}")
        cantidad = 0.0

    params = {
        "symbol":       simbolo,
        "side":         lado_cierre,
        "positionSide": pos_lado,
        "type":         "MARKET",
        "reduceOnly":   "true",
    }
    if cantidad > 0:
        params["quantity"] = cantidad

    resultado = await _post("/openApi/swap/v2/trade/order", params)

    # Calcular PnL aproximado
    pnl = 0.0
    try:
        hist = await _get("/openApi/swap/v2/user/income", {
            "symbol": simbolo,
            "incomeType": "REALIZED_PNL",
            "limit": 1,
        })
        items = hist.get("data", {}).get("list", [])
        if items:
            pnl = float(items[0].get("income", 0))
    except Exception:
        pass

    return {**resultado, "pnl": pnl}


async def listar_simbolos() -> list[str]:
    """Devuelve todos los pares perpetuos USDT activos en BingX."""
    data = await _get("/openApi/swap/v2/quote/contracts", {})
    contratos = data.get("data", [])
    return [
        c["symbol"] for c in contratos
        if c.get("symbol", "").endswith("-USDT") and c.get("status") == 1
    ]
