"""
Notificaciones Telegram
Mensajes detallados en cada operación
"""
import os
import logging
import httpx

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
BASE_URL  = f"https://api.telegram.org/bot{BOT_TOKEN}"

TIMEOUT = httpx.Timeout(8.0)

EMOJI_LONG  = "🟢"
EMOJI_SHORT = "🔴"
EMOJI_TP    = "✅"
EMOJI_SL    = "❌"
EMOJI_INFO  = "ℹ️"


async def _enviar(texto: str):
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            await client.post(
                BASE_URL + "/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": texto,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as e:
        log.error(f"Error enviando Telegram: {e}")


async def notificar_entrada(simbolo: str, accion: str, orden: dict, resultado: dict):
    emoji = EMOJI_LONG if accion == "BUY" else EMOJI_SHORT
    direccion = "LONG 🚀" if accion == "BUY" else "SHORT 📉"

    # Extraer precio real de fill si está disponible
    precio_fill = orden["precio_entrada"]
    try:
        datos = resultado.get("data", {}).get("order", {})
        if datos.get("avgPrice"):
            precio_fill = float(datos["avgPrice"])
    except Exception:
        pass

    texto = (
        f"{emoji} <b>NUEVA OPERACIÓN — {simbolo}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Dirección: <b>{direccion}</b>\n"
        f"💰 Entrada:   <b>${precio_fill:,.4f}</b>\n"
        f"🛑 Stop Loss: <b>${orden['stop_loss']:,.4f}</b>  ({orden['sl_pct']:.2f}%)\n"
        f"🎯 Take Profit: <b>${orden['take_profit']:,.4f}</b>  ({orden['tp_pct']:.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Tamaño posición: <b>${orden['cantidad_usdt']:.2f}</b>\n"
        f"⚡ Apalancamiento: <b>{os.environ.get('APALANCAMIENTO','10')}x</b>\n"
        f"🎲 Riesgo máximo: <b>${orden['riesgo_usdt']:.4f}</b>\n"
        f"📊 R:R objetivo: <b>1:{orden['ratio_rr']:.1f}</b>\n"
        f"📈 ATR usado: <b>{orden['atr']:.6f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Sniper Bot V26.1 | BingX Futures"
    )
    await _enviar(texto)


async def notificar_cierre(simbolo: str, lado: str, pnl: float):
    if pnl >= 0:
        emoji = EMOJI_TP
        resultado_txt = f"GANANCIA: +${pnl:.4f} USDT"
    else:
        emoji = EMOJI_SL
        resultado_txt = f"PÉRDIDA: ${pnl:.4f} USDT"

    direccion = "LONG" if lado == "BUY" else "SHORT"

    texto = (
        f"{emoji} <b>OPERACIÓN CERRADA — {simbolo}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Fue: <b>{direccion}</b>\n"
        f"💵 PnL realizado: <b>{resultado_txt}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Sniper Bot V26.1 | BingX Futures"
    )
    await _enviar(texto)


async def notificar_error(mensaje: str):
    texto = f"{EMOJI_INFO} <b>Bot Info</b>\n{mensaje}"
    await _enviar(texto)
