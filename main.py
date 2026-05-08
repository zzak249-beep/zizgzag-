"""
Sniper Bot V26.1 — Servidor principal
Railway + BingX Futures + Telegram
"""
import os
import hmac
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from app.risk import calcular_orden
from app.bingx import ejecutar_orden, cerrar_posicion, obtener_precio
from app.telegram import notificar_entrada, notificar_cierre, notificar_error
from app.state import estado

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Sniper Bot V26.1 arrancando...")
    try:
        await notificar_error("✅ Sniper Bot V26.1 iniciado")
    except Exception as e:
        log.warning(f"Telegram al arrancar: {e}")
    yield
    log.info("Bot detenido")


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

    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    if webhook_secret:
        secret = data.get("secret", "")
        if not hmac.compare_digest(secret, webhook_secret):
            raise HTTPException(403, "Secreto incorrecto")

    accion  = data.get("action", "").upper()
    simbolo = data.get("symbol", "").upper()
    atr     = float(data.get("atr", 0))

    log.info(f"Signal: {accion} {simbolo} ATR={atr}")

    if accion == "CLOSE":
        if not estado.posicion_abierta:
            return JSONResponse({"msg": "Sin posicion que cerrar"})
        resultado = await cerrar_posicion(estado.simbolo_activo, estado.lado_activo)
        pnl = resultado.get("pnl", 0)
        await notificar_cierre(estado.simbolo_activo, estado.lado_activo, pnl)
        estado.limpiar()
        return JSONResponse({"msg": "Posicion cerrada", "pnl": pnl})

    if estado.posicion_abierta:
        return JSONResponse({"msg": "Posicion ya abierta, ignorada"})

    if accion not in ("BUY", "SELL"):
        raise HTTPException(400, f"Accion desconocida: {accion}")

    if atr <= 0:
        raise HTTPException(400, "ATR debe ser > 0")

    precio_actual = await obtener_precio(simbolo)
    orden = calcular_orden(
        accion=accion,
        precio=precio_actual,
        atr=atr,
        capital=float(os.environ.get("CAPITAL_USDT", "8")),
        apalancamiento=int(os.environ.get("APALANCAMIENTO", "10")),
        riesgo_pct=float(os.environ.get("RIESGO_PCT", "1.0")),
        ratio_rr=float(os.environ.get("RATIO_RR", "3.0")),
    )

    try:
        resultado = await ejecutar_orden(simbolo, orden)
    except Exception as e:
        msg = f"Error ejecutando orden {simbolo}: {e}"
        log.error(msg)
        await notificar_error(f"❌ {msg}")
        raise HTTPException(500, str(e))

    estado.abrir(simbolo, accion, orden)
    await notificar_entrada(simbolo, accion, orden, resultado)
    return JSONResponse({"msg": "Orden ejecutada", "orden": orden})
