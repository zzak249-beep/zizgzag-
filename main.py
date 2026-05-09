"""
Sniper Bot V26.1 — archivo único para Railway
"""
import os, hmac, json, logging, time, hashlib, urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
BINGX_API_KEY    = os.environ.get("BINGX_API_KEY", "")
BINGX_API_SECRET = os.environ.get("BINGX_API_SECRET", "")
TG_TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT          = os.environ.get("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "")
CAPITAL          = float(os.environ.get("CAPITAL_USDT", "8"))
LEVERAGE         = int(os.environ.get("APALANCAMIENTO", "10"))
RISK_PCT         = float(os.environ.get("RIESGO_PCT", "1.0"))
RATIO_RR         = float(os.environ.get("RATIO_RR", "3.0"))
BASE_URL         = "https://open-api.bingx.com"

# ── STATE ─────────────────────────────────────────────────────────────────────
@dataclass
class Estado:
    posicion_abierta: bool = False
    simbolo: Optional[str] = None
    lado: Optional[str] = None
    precio_entrada: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0

    def abrir(self, simbolo, lado, orden):
        self.posicion_abierta = True
        self.simbolo = simbolo
        self.lado = lado
        self.precio_entrada = orden["precio_entrada"]
        self.stop_loss = orden["stop_loss"]
        self.take_profit = orden["take_profit"]

    def limpiar(self):
        self.__init__()

estado = Estado()

# ── RISK ──────────────────────────────────────────────────────────────────────
def calcular_orden(accion, precio, atr):
    sl_dist  = atr * 1.5
    sl_pct   = sl_dist / precio * 100
    if accion == "BUY":
        sl = precio - sl_dist
        tp = precio + sl_dist * RATIO_RR
    else:
        sl = precio + sl_dist
        tp = precio - sl_dist * RATIO_RR
    return {
        "accion": accion,
        "precio_entrada": round(precio, 6),
        "stop_loss": round(sl, 6),
        "take_profit": round(tp, 6),
        "cantidad_usdt": round(CAPITAL * LEVERAGE, 2),
        "riesgo_usdt": round(CAPITAL * LEVERAGE * RISK_PCT / 100, 4),
        "sl_pct": round(sl_pct, 3),
        "tp_pct": round(sl_pct * RATIO_RR, 3),
        "ratio_rr": RATIO_RR,
        "atr": round(atr, 6),
    }

# ── BINGX ─────────────────────────────────────────────────────────────────────
def _sign(params):
    q = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(BINGX_API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

def _headers():
    return {"X-BX-APIKEY": BINGX_API_KEY, "Content-Type": "application/json"}

async def bingx_get(path, params):
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(BASE_URL + path, params=params, headers=_headers())
    r.raise_for_status()
    d = r.json()
    if d.get("code", 0) != 0:
        raise RuntimeError(f"BingX: {d.get('msg')} (code {d.get('code')})")
    return d

async def bingx_post(path, body):
    body["timestamp"] = int(time.time() * 1000)
    body["signature"] = _sign(body)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(BASE_URL + path, params=body, headers=_headers())
    r.raise_for_status()
    d = r.json()
    if d.get("code", 0) != 0:
        raise RuntimeError(f"BingX: {d.get('msg')} (code {d.get('code')})")
    return d

async def obtener_precio(simbolo):
    d = await bingx_get("/openApi/swap/v2/quote/price", {"symbol": simbolo})
    return float(d["data"]["price"])

async def ejecutar_orden(simbolo, orden):
    lado     = "BUY" if orden["accion"] == "BUY" else "SELL"
    pos_lado = "LONG" if lado == "BUY" else "SHORT"
    try:
        await bingx_post("/openApi/swap/v2/trade/leverage", {
            "symbol": simbolo, "side": pos_lado, "leverage": LEVERAGE})
    except Exception as e:
        log.warning(f"Leverage: {e}")
    return await bingx_post("/openApi/swap/v2/trade/order", {
        "symbol": simbolo, "side": lado, "positionSide": pos_lado,
        "type": "MARKET", "quoteOrderQty": orden["cantidad_usdt"],
        "stopLoss": str(orden["stop_loss"]),
        "takeProfit": str(orden["take_profit"]),
        "stopLossWorkingType": "MARK_PRICE",
        "takeProfitWorkingType": "MARK_PRICE",
    })

async def cerrar_posicion(simbolo, lado):
    lado_cierre = "SELL" if lado == "BUY" else "BUY"
    pos_lado    = "LONG" if lado == "BUY" else "SHORT"
    return await bingx_post("/openApi/swap/v2/trade/order", {
        "symbol": simbolo, "side": lado_cierre,
        "positionSide": pos_lado, "type": "MARKET", "reduceOnly": "true",
    })

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def tg(texto):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": texto,
                      "parse_mode": "HTML", "disable_web_page_preview": True})
    except Exception as e:
        log.warning(f"Telegram: {e}")

async def tg_entrada(simbolo, accion, orden):
    e = "🟢" if accion == "BUY" else "🔴"
    d = "LONG 🚀" if accion == "BUY" else "SHORT 📉"
    await tg(
        f"{e} <b>NUEVA OPERACIÓN — {simbolo}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Dirección: <b>{d}</b>\n"
        f"💰 Entrada:   <b>${orden['precio_entrada']:,.4f}</b>\n"
        f"🛑 Stop Loss: <b>${orden['stop_loss']:,.4f}</b>  ({orden['sl_pct']:.2f}%)\n"
        f"🎯 Take Profit: <b>${orden['take_profit']:,.4f}</b>  ({orden['tp_pct']:.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Posición: <b>${orden['cantidad_usdt']:.2f}</b>  |  "
        f"⚡ {LEVERAGE}x  |  📊 R:R 1:{RATIO_RR}\n"
        f"🎲 Riesgo: <b>${orden['riesgo_usdt']:.4f}</b>  |  "
        f"📈 ATR: {orden['atr']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Sniper Bot V26.1 | BingX Futures"
    )

async def tg_cierre(simbolo, lado, pnl):
    e = "✅" if pnl >= 0 else "❌"
    txt = f"GANANCIA: +${pnl:.4f}" if pnl >= 0 else f"PÉRDIDA: ${pnl:.4f}"
    await tg(f"{e} <b>CERRADA — {simbolo}</b>\n"
             f"📌 {'LONG' if lado=='BUY' else 'SHORT'}\n"
             f"💵 {txt} USDT\n"
             f"🤖 Sniper Bot V26.1")

# ── APP ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    log.info("Sniper Bot V26.1 iniciado")
    await tg("✅ <b>Sniper Bot V26.1</b> iniciado y escuchando señales")
    yield

app = FastAPI(title="Sniper Bot V26.1", lifespan=lifespan)

@app.get("/")
async def root():
    return {"bot": "Sniper V26.1", "status": "running"}

@app.get("/health")
async def health():
    return {"status": "ok", "posicion_abierta": estado.posicion_abierta}

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(400, "JSON invalido")

    if WEBHOOK_SECRET:
        if not hmac.compare_digest(data.get("secret", ""), WEBHOOK_SECRET):
            raise HTTPException(403, "Secreto incorrecto")

    accion  = data.get("action", "").upper()
    simbolo = data.get("symbol", "").upper()
    atr     = float(data.get("atr", 0))
    log.info(f"Signal: {accion} {simbolo} ATR={atr}")

    if accion == "CLOSE":
        if not estado.posicion_abierta:
            return JSONResponse({"msg": "Sin posicion abierta"})
        res = await cerrar_posicion(estado.simbolo, estado.lado)
        pnl = float(res.get("data", {}).get("pnl", 0))
        await tg_cierre(estado.simbolo, estado.lado, pnl)
        estado.limpiar()
        return JSONResponse({"msg": "Cerrada", "pnl": pnl})

    if estado.posicion_abierta:
        return JSONResponse({"msg": "Posicion ya abierta, ignorada"})

    if accion not in ("BUY", "SELL"):
        raise HTTPException(400, f"Accion desconocida: {accion}")
    if atr <= 0:
        raise HTTPException(400, "ATR debe ser > 0")

    precio = await obtener_precio(simbolo)
    orden  = calcular_orden(accion, precio, atr)

    try:
        await ejecutar_orden(simbolo, orden)
    except Exception as e:
        await tg(f"❌ Error {simbolo}: {e}")
        raise HTTPException(500, str(e))

    estado.abrir(simbolo, accion, orden)
    await tg_entrada(simbolo, accion, orden)
    return JSONResponse({"msg": "Orden ejecutada", "orden": orden})
