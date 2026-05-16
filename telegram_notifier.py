"""
bot/telegram_notifier.py
Notificaciones ricas en Telegram con toda la información de la operación.

Mensajes implementados:
  - 🚀 Arranque del bot
  - 📈 Entrada LONG / 📉 Entrada SHORT
  - 🎯 Salida con PnL
  - ⚠️  Error crítico
  - 📊 Resumen diario (heartbeat cada hora)
"""
import logging
from datetime import datetime
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError
from telegram.constants import ParseMode

from bot.strategy import SignalResult

logger = logging.getLogger(__name__)


class TelegramNotifier:

    def __init__(self, token: str, chat_id: str):
        self._bot     = Bot(token=token)
        self._chat_id = chat_id

    async def _send(self, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML
            )
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")

    # ─────────────────────────────────────────
    # ARRANQUE
    # ─────────────────────────────────────────

    async def send_startup(self, config) -> None:
        symbols = ", ".join(config.SYMBOLS)
        mode    = "🔴 REAL MONEY" if not config.TESTNET else "🟡 TESTNET"
        msg = (
            f"🤖 <b>SNIPER BOT V49 — INICIADO</b>\n"
            f"{'─'*30}\n"
            f"📍 Modo: <b>{mode}</b>\n"
            f"💱 Pares: <code>{symbols}</code>\n"
            f"⏱ Timeframe: <code>{config.TIMEFRAME}</code>\n"
            f"🔧 Apalancamiento: <b>{config.LEVERAGE}x</b>\n"
            f"⚠️ Riesgo/trade: <b>{config.RISK_PER_TRADE}%</b>\n"
            f"🛑 Max DD diario: <b>{config.MAX_DAILY_LOSS_PCT}%</b>\n"
            f"⏰ <i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</i>"
        )
        await self._send(msg)

    # ─────────────────────────────────────────
    # ENTRADA
    # ─────────────────────────────────────────

    async def send_entry(self, symbol: str, side: str,
                         order: dict, signal: SignalResult,
                         balance: float) -> None:
        emoji  = "📈" if side == "LONG" else "📉"
        regime_emoji = {
            "TENDENCIA": "🔥", "RANGO": "🌊", "TRANSICION": "⚡"
        }.get(signal.regime, "❓")

        reasons_str = "\n".join(f"  • {r}" for r in signal.reasons)

        msg = (
            f"{emoji} <b>NUEVA OPERACIÓN — {side}</b>\n"
            f"{'─'*30}\n"
            f"💱 Par: <b>{symbol}</b>\n"
            f"💵 Precio entrada: <code>{signal.entry_price:.4f}</code>\n"
            f"📦 Cantidad: <code>{order['qty']}</code>\n"
            f"🎯 TP: <code>{order['tp']:.4f}</code>\n"
            f"🛑 SL: <code>{order['sl']:.4f}</code>\n"
            f"{'─'*30}\n"
            f"🧠 <b>ANÁLISIS</b>\n"
            f"{regime_emoji} Régimen: <b>{signal.regime}</b>\n"
            f"📊 ADX: <code>{signal.adx:.1f}</code>\n"
            f"🎲 Prob Bull: <code>{signal.prob_bull:.1f}%</code>\n"
            f"🎲 Prob Bear: <code>{signal.prob_bear:.1f}%</code>\n"
            f"📉 RVOL: <code>{signal.rvol:.2f}x</code>\n"
            f"🔁 STC: <code>{signal.stc:.1f}</code>\n"
            f"💧 VWAP: <code>{signal.vwap:.4f}</code>\n"
            f"🏛 POC: <code>{signal.poc:.4f}</code>\n"
            f"🌊 RSI: <code>{signal.rsi_val:.1f}</code>\n"
            f"📐 Kotegawa: <code>{signal.pct_below_ma:.2f}% bajo MA25</code>\n"
            f"⭐ Score: <b>{signal.score:.0f}/100</b>\n"
            f"{'─'*30}\n"
            f"<b>Razones:</b>\n{reasons_str}\n"
            f"{'─'*30}\n"
            f"💼 Balance: <code>${balance:.2f} USDT</code>\n"
            f"⏰ <i>{datetime.utcnow().strftime('%H:%M:%S UTC')}</i>"
        )
        await self._send(msg)

    # ─────────────────────────────────────────
    # SALIDA
    # ─────────────────────────────────────────

    async def send_exit(self, symbol: str, reason: str,
                        pnl_usdt: float, pnl_pct: float,
                        balance: float, signal: Optional[SignalResult] = None) -> None:
        win   = pnl_usdt >= 0
        emoji = "✅" if win else "❌"
        sign  = "+" if win else ""

        reason_map = {
            "TP":   "🎯 Take Profit alcanzado",
            "SL":   "🛑 Stop Loss activado",
            "TIME": "⏱ Barrera de tiempo"
        }
        reason_txt = reason_map.get(reason, reason)

        msg = (
            f"{emoji} <b>OPERACIÓN CERRADA — {symbol}</b>\n"
            f"{'─'*30}\n"
            f"📌 Motivo: <b>{reason_txt}</b>\n"
            f"💰 PnL: <b>{sign}{pnl_usdt:.4f} USDT</b> "
            f"(<code>{sign}{pnl_pct:.2f}%</code>)\n"
            f"💼 Balance: <code>${balance:.2f} USDT</code>\n"
            f"⏰ <i>{datetime.utcnow().strftime('%H:%M:%S UTC')}</i>"
        )
        await self._send(msg)

    # ─────────────────────────────────────────
    # HEARTBEAT / RESUMEN
    # ─────────────────────────────────────────

    async def send_heartbeat(self, balance: float, daily_pnl: float,
                             open_pos: int, daily_loss_pct: float,
                             symbols_status: dict) -> None:
        status_lines = ""
        for sym, sig in symbols_status.items():
            r = sig.regime if sig else "—"
            status_lines += f"  {sym}: <code>{r}</code>\n"

        daily_emoji = "📈" if daily_pnl >= 0 else "📉"
        msg = (
            f"💓 <b>HEARTBEAT — SNIPER BOT</b>\n"
            f"{'─'*30}\n"
            f"💼 Balance: <code>${balance:.2f} USDT</code>\n"
            f"{daily_emoji} PnL diario: <code>{daily_pnl:+.4f} USDT</code>\n"
            f"📊 Pos. abiertas: <b>{open_pos}</b>\n"
            f"⚠️ DD diario: <code>{daily_loss_pct:.2f}%</code>\n"
            f"{'─'*30}\n"
            f"<b>Estado pares:</b>\n{status_lines}"
            f"⏰ <i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>"
        )
        await self._send(msg)

    # ─────────────────────────────────────────
    # ERROR
    # ─────────────────────────────────────────

    async def send_error(self, error_msg: str) -> None:
        msg = (
            f"⚠️ <b>ERROR CRÍTICO</b>\n"
            f"{'─'*30}\n"
            f"<code>{error_msg[:500]}</code>\n"
            f"⏰ <i>{datetime.utcnow().strftime('%H:%M:%S UTC')}</i>"
        )
        await self._send(msg)

    async def send_paused(self, reason: str) -> None:
        msg = (
            f"⛔ <b>BOT PAUSADO</b>\n"
            f"Motivo: {reason}\n"
            f"⏰ <i>{datetime.utcnow().strftime('%H:%M:%S UTC')}</i>"
        )
        await self._send(msg)
