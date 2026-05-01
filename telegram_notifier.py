"""
telegram_notifier.py — Envía notificaciones al bot de Telegram.

Todos los eventos importantes del bot se notifican:
  - Bot iniciado / detenido
  - Señal de entrada detectada
  - Orden colocada (con detalles de precio, TP, SL)
  - Orden cerrada (con P&L)
  - Errores críticos
"""

from __future__ import annotations
import logging
import requests
from datetime import datetime

import config

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"


def _send(message: str, parse_mode: str = "HTML") -> bool:
    """Envía un mensaje al chat configurado."""
    try:
        resp = requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id":    config.TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": parse_mode,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Error al enviar Telegram: %s", e)
        return False


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


# ─────────────────────────────────────────────────────────────────────────────
# Mensajes específicos
# ─────────────────────────────────────────────────────────────────────────────

def send_bot_started(symbol: str, leverage: int) -> None:
    msg = (
        "🤖 <b>ZigZag Bot INICIADO</b>\n"
        f"📊 Símbolo:      <code>{symbol}</code>\n"
        f"⚡ Apalancamiento: <code>{leverage}x</code>\n"
        f"⏱  Timeframe:    <code>{config.TIMEFRAME}</code>\n"
        f"🎯 TP:           <code>{config.TP_PIPS} pips</code>\n"
        f"🛑 SL:           <code>{config.SL_PIPS} pips</code>\n"
        f"💰 Capital/op:   <code>{config.CAPITAL_PER_TRADE} USDT</code>\n"
        f"🕒 {_now()}"
    )
    _send(msg)


def send_bot_stopped(reason: str = "Manual") -> None:
    msg = (
        "🔴 <b>ZigZag Bot DETENIDO</b>\n"
        f"Motivo: {reason}\n"
        f"🕒 {_now()}"
    )
    _send(msg)


def send_signal_detected(
    direction:  str,    # "LONG" | "SHORT"
    symbol:     str,
    price:      float,
    resistance: float,
    support:    float,
) -> None:
    emoji    = "🟢" if direction == "LONG" else "🔴"
    trigger  = f"↗️ Rompe resistencia <code>{resistance:.4f}</code>" \
               if direction == "LONG" \
               else f"↘️ Rompe soporte <code>{support:.4f}</code>"
    msg = (
        f"{emoji} <b>SEÑAL {direction} detectada</b>\n"
        f"📊 {symbol}\n"
        f"💹 Precio actual: <code>{price:.4f}</code>\n"
        f"{trigger}\n"
        f"🏔  Resistencia ZZ: <code>{resistance:.4f}</code>\n"
        f"🏔  Soporte ZZ:     <code>{support:.4f}</code>\n"
        f"🕒 {_now()}"
    )
    _send(msg)


def send_order_placed(
    direction:  str,
    symbol:     str,
    entry:      float,
    tp:         float,
    sl:         float,
    quantity:   float,
    leverage:   int,
    capital:    float,
) -> None:
    emoji = "🟢" if direction == "LONG" else "🔴"
    msg = (
        f"{emoji} <b>ORDEN ABIERTA — {direction}</b>\n"
        f"📊 {symbol}\n"
        f"📈 Entrada:  <code>{entry:.4f}</code>\n"
        f"🎯 TP:       <code>{tp:.4f}</code>\n"
        f"🛑 SL:       <code>{sl:.4f}</code>\n"
        f"📦 Cantidad: <code>{quantity:.4f}</code>\n"
        f"⚡ Leverage: <code>{leverage}x</code>\n"
        f"💰 Capital:  <code>{capital:.2f} USDT</code>\n"
        f"🕒 {_now()}"
    )
    _send(msg)


def send_order_closed(
    direction:  str,
    symbol:     str,
    entry:      float,
    exit_price: float,
    pnl:        float,
    reason:     str,     # "TP" | "SL" | "Manual"
) -> None:
    if pnl >= 0:
        emoji  = "✅"
        result = f"+{pnl:.2f} USDT 🎉"
    else:
        emoji  = "❌"
        result = f"{pnl:.2f} USDT 😔"

    msg = (
        f"{emoji} <b>ORDEN CERRADA — {direction}</b>\n"
        f"📊 {symbol}\n"
        f"📈 Entrada:  <code>{entry:.4f}</code>\n"
        f"📉 Salida:   <code>{exit_price:.4f}</code>\n"
        f"💵 P&L:      <b>{result}</b>\n"
        f"📌 Motivo:   {reason}\n"
        f"🕒 {_now()}"
    )
    _send(msg)


def send_zigzag_levels(
    symbol:     str,
    resistance: float,
    support:    float,
    current:    float,
) -> None:
    msg = (
        f"📐 <b>Niveles ZigZag actualizados</b>\n"
        f"📊 {symbol}\n"
        f"🏔  Resistencia: <code>{resistance:.4f}</code>\n"
        f"🏔  Soporte:     <code>{support:.4f}</code>\n"
        f"💹  Precio:      <code>{current:.4f}</code>\n"
        f"↕️  Rango:       <code>{(resistance - support):.4f}</code>\n"
        f"🕒 {_now()}"
    )
    _send(msg)


def send_error(error: str) -> None:
    msg = (
        f"⚠️ <b>ERROR en ZigZag Bot</b>\n"
        f"<code>{error[:300]}</code>\n"
        f"🕒 {_now()}"
    )
    _send(msg)


def send_balance(balance_usdt: float) -> None:
    msg = (
        f"💼 <b>Balance actual</b>\n"
        f"💰 <code>{balance_usdt:.2f} USDT</code>\n"
        f"🕒 {_now()}"
    )
    _send(msg)
