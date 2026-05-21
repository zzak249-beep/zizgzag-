"""
QF×JP Crypto Bot — BingX + Telegram
Railway deployment ready
"""
import asyncio
import logging
import os
import time
import hmac
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# ── Carga .env (local dev; en Railway las vars vienen del entorno directamente)
load_dotenv()

# ── Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
log = logging.getLogger("qfjp_bot")

# ── Config desde entorno
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_API_SECRET = os.environ["BINGX_API_SECRET"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "qfjp_secret_2025")
TRADE_SIZE_USDT  = float(os.environ.get("TRADE_SIZE_USDT", "10"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES", "2"))
SL_PCT           = float(os.environ.get("SL_PCT", "1.5"))
TP_PCT           = float(os.environ.get("TP_PCT", "3.0"))
ALLOWED_SIGNALS  = os.environ.get(
    "ALLOWED_SIGNALS",
    "LONG_SUP_V3,SHORT_SUP_V3,LONG_SUP,SHORT_SUP,LONG_FUEL,SHORT_FUEL,HUNT_LONG,HUNT_SHORT"
).split(",")
MIN_SIGNAL_LEVEL = os.environ.get("MIN_SIGNAL_LEVEL", "LONG_FUEL")

BINGX_BASE = "https://open-api.bingx.com"

# Jerarquía de señales (mayor = más convicción)
SIGNAL_RANK = {
    "HUNT_LONG": 1,  "HUNT_SHORT": 1,
    "LONG_FUEL": 2,  "SHORT_FUEL": 2,
    "LONG_SUP":  3,  "SHORT_SUP":  3,
    "LONG_SUP_V3": 4, "SHORT_SUP_V3": 4,
}
MIN_RANK = SIGNAL_RANK.get(MIN_SIGNAL_LEVEL, 2)

# ── Estado en memoria
open_trades: dict[str, dict] = {}

# ══════════════════════════════════════════════════
#  BINGX CLIENT
# ══════════════════════════════════════════════════
def _sign(params: dict, secret: str) -> str:
    """Firma HMAC-SHA256 para BingX."""
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def _bingx_headers() -> dict:
    return {
        "X-BX-APIKEY": BINGX_API_KEY,
        "Content-Type": "application/json",
    }

async def bingx_get(path: str, params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params, BINGX_API_SECRET)
    url = BINGX_BASE + path
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params, headers=_bingx_headers()) as r:
            data = await r.json()
            if data.get("code", 0) != 0:
                raise Exception(f"BingX GET error: {data}")
            return data

async def bingx_post(path: str, body: dict) -> dict:
    body["timestamp"] = int(time.time() * 1000)
    body["signature"] = _sign(body, BINGX_API_SECRET)
    url = BINGX_BASE + path
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=_bingx_headers()) as r:
            data = await r.json()
            if data.get("code", 0) != 0:
                raise Exception(f"BingX POST error: {data}")
            return data

async def get_price(symbol: str) -> float:
    """Precio actual de mercado."""
    data = await bingx_get("/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(data["data"]["price"])

async def get_balance() -> float:
    """Balance disponible en USDT."""
    data = await bingx_get("/openApi/swap/v2/user/balance", {})
    for item in data["data"]["balance"]:
        if item["asset"] == "USDT":
            return float(item["availableMargin"])
    return 0.0

async def get_open_position(symbol: str) -> Optional[dict]:
    """
    Consulta si hay posición abierta en BingX para ese símbolo.
    Devuelve el dict de posición o None.
    """
    try:
        data = await bingx_get("/openApi/swap/v2/user/positions", {"symbol": symbol})
        positions = data.get("data", [])
        for p in positions:
            if float(p.get("positionAmt", 0)) != 0:
                return p
        return None
    except Exception as e:
        log.error(f"Error consultando posición {symbol}: {e}")
        return None

async def place_order(symbol: str, side: str, quantity: float,
                      sl_price: float, tp_price: float) -> dict:
    """Abre orden de mercado con SL y TP en BingX perpetual swap."""
    order = await bingx_post("/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type": "MARKET",
        "quantity": str(quantity),
        "stopLossPrice": str(round(sl_price, 4)),
        "takeProfitPrice": str(round(tp_price, 4)),
    })
    log.info(f"Orden abierta {side} {symbol} qty={quantity} SL={sl_price} TP={tp_price}")
    return order

async def close_position(symbol: str, side: str, quantity: float) -> dict:
    """Cierra posición abierta (reduceOnly)."""
    close_side = "SELL" if side == "BUY" else "BUY"
    pos_side   = "LONG" if side == "BUY" else "SHORT"
    order = await bingx_post("/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": close_side,
        "positionSide": pos_side,
        "type": "MARKET",
        "quantity": str(quantity),
        "reduceOnly": "true",
    })
    log.info(f"Posición cerrada {symbol}")
    return order

# ══════════════════════════════════════════════════
#  TELEGRAM CLIENT
# ══════════════════════════════════════════════════
async def tg_send(msg: str, parse_mode: str = "HTML") -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": parse_mode}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload) as r:
                if r.status != 200:
                    log.error(f"Telegram error {r.status}: {await r.text()}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")

def fmt_signal_msg(signal: str, symbol: str, price: float,
                   side: str, sl: float, tp: float, qty: float) -> str:
    emoji = "🟢" if "LONG" in signal else "🔴"
    rank_stars = "★" * SIGNAL_RANK.get(signal, 1)
    return (
        f"{emoji} <b>{signal}</b> {rank_stars}\n"
        f"📊 <b>{symbol}</b> @ <code>{price}</code>\n"
        f"📐 Qty: <code>{qty}</code> USDT\n"
        f"🛑 SL: <code>{sl}</code>\n"
        f"🎯 TP: <code>{tp}</code>\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )

def fmt_close_msg(symbol: str, pnl_pct: float, reason: str) -> str:
    emoji = "✅" if pnl_pct > 0 else "❌"
    return (
        f"{emoji} <b>CERRADO</b> {symbol}\n"
        f"PnL: <code>{pnl_pct:+.2f}%</code>\n"
        f"Razón: {reason}"
    )

# ══════════════════════════════════════════════════
#  LÓGICA DE TRADING
# ══════════════════════════════════════════════════
def normalize_symbol(raw: str) -> str:
    """Convierte BTCUSDT → BTC-USDT para BingX perpetuals."""
    raw = raw.upper().strip()
    raw = raw.replace("-PERP", "").replace("USDT.P", "").replace(".P", "")
    if "-USDT" in raw:
        return raw
    if raw.endswith("USDT"):
        base = raw[:-4]
    else:
        base = raw
    return f"{base}-USDT"

def calc_quantity(price: float, usdt: float) -> float:
    """Calcula cantidad de contratos dado el precio y el capital."""
    qty = round(usdt / price, 4)
    return max(qty, 0.001)

async def handle_signal(data: dict) -> None:
    signal     = data.get("signal", "").strip()
    symbol_raw = data.get("symbol", "").strip()
    symbol     = normalize_symbol(symbol_raw)

    # ── Filtros
    if signal not in ALLOWED_SIGNALS:
        log.info(f"Señal ignorada (no permitida): {signal}")
        return
    if SIGNAL_RANK.get(signal, 0) < MIN_RANK:
        log.info(f"Señal nivel insuficiente: {signal} rank={SIGNAL_RANK.get(signal,0)} < {MIN_RANK}")
        return
    if signal in ("BLACKOUT", "SPOOF", "LIQ_LONG", "LIQ_SHORT"):
        await tg_send(f"⚠️ <b>AVISO</b> {signal} en {symbol}")
        return

    is_long  = "LONG"  in signal
    is_short = "SHORT" in signal
    if not (is_long or is_short):
        return

    if len(open_trades) >= MAX_OPEN_TRADES:
        log.info(f"Límite de trades alcanzado ({MAX_OPEN_TRADES})")
        return

    if symbol in open_trades:
        existing = open_trades[symbol]
        if (is_long and existing["side"] == "BUY") or (is_short and existing["side"] == "SELL"):
            log.info(f"Posición ya abierta en {symbol}")
            return

    try:
        price   = await get_price(symbol)
        balance = await get_balance()

        usdt_to_use = min(TRADE_SIZE_USDT, balance * 0.20)
        if usdt_to_use < 5:
            await tg_send(f"⚠️ Balance insuficiente: <code>{balance:.2f} USDT</code>")
            return

        qty  = calc_quantity(price, usdt_to_use)
        side = "BUY" if is_long else "SELL"

        sl = round(price * (1 - SL_PCT / 100) if is_long else price * (1 + SL_PCT / 100), 4)
        tp = round(price * (1 + TP_PCT / 100) if is_long else price * (1 - TP_PCT / 100), 4)

        order    = await place_order(symbol, side, qty, sl, tp)
        order_id = order.get("data", {}).get("orderId", "—")

        open_trades[symbol] = {
            "side": side, "entry": price, "qty": qty,
            "sl": sl, "tp": tp, "signal": signal,
            "order_id": order_id, "time": time.time(),
        }

        await tg_send(fmt_signal_msg(signal, symbol, price, side, sl, tp, qty))
        log.info(f"Trade ejecutado: {side} {symbol} @ {price}")

    except Exception as e:
        log.error(f"Error ejecutando trade: {e}")
        await tg_send(f"❌ <b>ERROR</b> al ejecutar {signal} {symbol}\n<code>{e}</code>")

# ══════════════════════════════════════════════════
#  MONITOR DE POSICIONES ABIERTAS
# ══════════════════════════════════════════════════
async def position_monitor() -> None:
    """Revisa posiciones abiertas cada 30 s y cierra si SL/TP/timeout."""
    while True:
        await asyncio.sleep(30)
        for symbol, trade in list(open_trades.items()):
            try:
                # Primero comprueba si BingX ya cerró la posición (SL/TP servidor)
                live_pos = await get_open_position(symbol)
                if live_pos is None:
                    # BingX ya cerró — eliminar del estado local
                    log.info(f"Posición {symbol} ya cerrada en BingX (SL/TP servidor)")
                    price    = await get_price(symbol)
                    entry    = trade["entry"]
                    side     = trade["side"]
                    pnl_pct  = ((price - entry) / entry * 100) if side == "BUY" else ((entry - price) / entry * 100)
                    await tg_send(fmt_close_msg(symbol, pnl_pct, "SL/TP ejecutado en servidor"))
                    open_trades.pop(symbol, None)
                    continue

                price   = await get_price(symbol)
                entry   = trade["entry"]
                side    = trade["side"]
                pnl_pct = ((price - entry) / entry * 100) if side == "BUY" else ((entry - price) / entry * 100)

                reason: Optional[str] = None

                if side == "BUY":
                    if price <= trade["sl"]:
                        reason = "SL alcanzado"
                    elif price >= trade["tp"]:
                        reason = "TP alcanzado"
                else:
                    if price >= trade["sl"]:
                        reason = "SL alcanzado"
                    elif price <= trade["tp"]:
                        reason = "TP alcanzado"

                if time.time() - trade["time"] > 10_800:
                    reason = "Timeout 3h"

                if reason:
                    await close_position(symbol, side, trade["qty"])
                    await tg_send(fmt_close_msg(symbol, pnl_pct, reason))
                    open_trades.pop(symbol, None)

            except Exception as e:
                log.error(f"Monitor error {symbol}: {e}")

# ══════════════════════════════════════════════════
#  FASTAPI — lifespan + rutas
# ══════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown con lifespan (FastAPI >= 0.93, reemplaza on_event)."""
    # ── Startup
    asyncio.create_task(position_monitor())
    await tg_send("🤖 <b>QF×JP Bot iniciado</b>\nEsperando señales de TradingView...")
    log.info("Bot iniciado correctamente")
    yield
    # ── Shutdown (limpieza opcional)
    log.info("Bot detenido")

app = FastAPI(title="QF×JP Bot", lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    secret = request.headers.get("X-Webhook-Secret", "")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    log.info(f"Webhook recibido: {data}")
    asyncio.create_task(handle_signal(data))
    return JSONResponse({"status": "ok"})


@app.get("/health")
async def health():
    return {
        "status": "running",
        "open_trades": len(open_trades),
        "trades": list(open_trades.keys()),
    }


@app.get("/trades")
async def trades():
    return {"open_trades": open_trades}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
