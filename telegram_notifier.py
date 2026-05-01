"""
telegram_notifier.py — Notificaciones Telegram para el bot multi-símbolo.
"""
from __future__ import annotations
import logging, requests
from datetime import datetime
import config

logger  = logging.getLogger(__name__)
API_URL = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"

def _now() -> str:
    return datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")

def send(text: str) -> bool:
    try:
        r = requests.post(f"{API_URL}/sendMessage", json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text, "parse_mode": "HTML",
        }, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error("Telegram error: %s", e)
        return False

# ── Mensajes ───────────────────────────────────────────────────────────────

def bot_started(symbols_count: int) -> None:
    send(
        f"🤖 <b>ZigZag Multi-Bot INICIADO</b>\n"
        f"🔍 Monitorizando: <b>{symbols_count} monedas</b>\n"
        f"⚡ Leverage:  <code>{config.LEVERAGE}x</code>\n"
        f"🎯 TP: <code>{config.TP_PIPS} pips</code>  "
        f"🛑 SL: <code>{config.SL_PIPS} pips</code>\n"
        f"💰 Capital/op: <code>{config.CAPITAL_PER_TRADE} USDT</code>\n"
        f"📊 Max posiciones: <code>{config.MAX_OPEN_POSITIONS}</code>\n"
        f"🕒 {_now()}"
    )

def bot_stopped(reason: str = "Manual") -> None:
    send(f"🔴 <b>Bot DETENIDO</b>\nMotivo: {reason}\n🕒 {_now()}")

def scan_summary(scanned: int, signals: int, skipped: int) -> None:
    send(
        f"🔎 <b>Escaneo completado</b>\n"
        f"Analizadas:  <code>{scanned}</code> monedas\n"
        f"Señales:     <code>{signals}</code>\n"
        f"Omitidas:    <code>{skipped}</code> (límite posiciones)\n"
        f"🕒 {_now()}"
    )

def signal_detected(direction: str, symbol: str, price: float,
                    resistance: float, support: float) -> None:
    e = "🟢" if direction == "LONG" else "🔴"
    lvl = f"Rompe resistencia <code>{resistance:.6g}</code>" \
          if direction == "LONG" else \
          f"Rompe soporte <code>{support:.6g}</code>"
    send(
        f"{e} <b>SEÑAL {direction}</b> — {symbol}\n"
        f"💹 Precio: <code>{price:.6g}</code>\n"
        f"📐 {lvl}\n"
        f"🏔 Resist: <code>{resistance:.6g}</code>  "
        f"🏔 Soporte: <code>{support:.6g}</code>\n"
        f"🕒 {_now()}"
    )

def order_opened(direction: str, symbol: str, entry: float,
                 tp: float, sl: float, qty: float, capital: float) -> None:
    e = "🟢" if direction == "LONG" else "🔴"
    send(
        f"{e} <b>ORDEN ABIERTA — {direction}</b>\n"
        f"📊 <b>{symbol}</b>\n"
        f"📈 Entrada:  <code>{entry:.6g}</code>\n"
        f"🎯 TP:       <code>{tp:.6g}</code>\n"
        f"🛑 SL:       <code>{sl:.6g}</code>\n"
        f"📦 Cantidad: <code>{qty:.4f}</code>\n"
        f"⚡ {config.LEVERAGE}x  💰 {capital:.2f} USDT\n"
        f"🕒 {_now()}"
    )

def order_closed(direction: str, symbol: str, entry: float,
                 exit_p: float, pnl: float, reason: str) -> None:
    if pnl >= 0:
        e, res = "✅", f"<b>+{pnl:.2f} USDT 🎉</b>"
    else:
        e, res = "❌", f"<b>{pnl:.2f} USDT 😔</b>"
    send(
        f"{e} <b>CERRADA — {direction}</b>\n"
        f"📊 <b>{symbol}</b>\n"
        f"📈 Entrada: <code>{entry:.6g}</code>\n"
        f"📉 Salida:  <code>{exit_p:.6g}</code>\n"
        f"💵 P&amp;L:    {res}\n"
        f"📌 Motivo:  {reason}\n"
        f"🕒 {_now()}"
    )

def daily_report(trades: int, wins: int, losses: int,
                 total_pnl: float, balance: float) -> None:
    wr  = f"{wins/(wins+losses)*100:.1f}%" if (wins+losses) > 0 else "—"
    pnl = f"+{total_pnl:.2f}" if total_pnl >= 0 else f"{total_pnl:.2f}"
    send(
        f"📊 <b>REPORTE DIARIO</b>\n"
        f"Operaciones:  <code>{trades}</code>\n"
        f"✅ Wins:      <code>{wins}</code>\n"
        f"❌ Losses:    <code>{losses}</code>\n"
        f"🎯 Win rate:  <code>{wr}</code>\n"
        f"💵 P&amp;L día: <b>{pnl} USDT</b>\n"
        f"💼 Balance:   <code>{balance:.2f} USDT</code>\n"
        f"🕒 {_now()}"
    )

def error(msg: str) -> None:
    send(f"⚠️ <b>ERROR</b>\n<code>{str(msg)[:300]}</code>\n🕒 {_now()}")
