"""
Sniper Bot V26.1 — Servidor principal
Railway + BingX Futures + Telegram
"""
import os
import hmac
import hashlib
import time
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from app.risk import calcular_orden
from app.bingx import ejecutar_orden, cerrar_posicion, obtener_precio
from app.telegram import notificar_entrada, notificar_cierre, notificar_error
from app.state import estado

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Sniper Bot iniciado")
    await notificar_error("✅ Bot iniciado y escuchando señales")
    yield
    log.info("Bot detenido")


app = FastAPI(title="Sniper Bot V26.1", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "posicion_abierta": estado.posicion_abierta}


@app.post("/webhook")
async def webhook(request: Request):
    # Validar secreto
    body = await request.body()
    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(400, "JSON inválido")

    secret = data.get("secret", "")
    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        log.warning("Webhook rechazado: secreto incorrecto")
        raise HTTPException(403, "Secreto incorrecto")

    accion = data.get("action", "").upper()   # BUY / SELL / CLOSE
    simbolo = data.get("symbol", "").upper()  # ej: BTC-USDT
    atr     = float(data.get("atr", 0))

    log.info(f"Señal recibida: {accion} {simbolo} ATR={atr}")

    # ── CERRAR posición existente ──────────────────────────────────────────
    if accion == "CLOSE":
        if not estado.posicion_abierta:
            return JSONResponse({"msg": "Sin posición que cerrar"})
        resultado = await cerrar_posicion(estado.simbolo_activo, estado.lado_activo)
        pnl = resultado.get("pnl", 0)
        await notificar_cierre(estado.simbolo_activo, estado.lado_activo, pnl)
        estado.limpiar()
        return JSONResponse({"msg": "Posición cerrada", "pnl": pnl})

    # ── Guardia: solo 1 posición a la vez ─────────────────────────────────
    if estado.posicion_abierta:
        log.info("Señal ignorada: ya hay posición abierta")
        return JSONResponse({"msg": "Posición ya abierta, señal ignorada"})

    if accion not in ("BUY", "SELL"):
        raise HTTPException(400, f"Acción desconocida: {accion}")

    if atr <= 0:
        raise HTTPException(400, "ATR debe ser > 0")

    # ── Calcular orden ─────────────────────────────────────────────────────
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

    # ── Ejecutar en BingX ──────────────────────────────────────────────────
    try:
        resultado = await ejecutar_orden(simbolo, orden)
    except Exception as e:
        msg = f"❌ Error ejecutando orden {simbolo}: {e}"
        log.error(msg)
        await notificar_error(msg)
        raise HTTPException(500, str(e))

    # ── Guardar estado ─────────────────────────────────────────────────────
    estado.abrir(simbolo, accion, orden)

    # ── Notificar Telegram ─────────────────────────────────────────────────
    await notificar_entrada(simbolo, accion, orden, resultado)

    return JSONResponse({"msg": "Orden ejecutada", "orden": orden})
