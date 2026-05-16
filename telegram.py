"""
Telegram Notifications — Sniper Bot V50.6
Sends rich HTML-formatted signals, alerts and daily performance summaries.
"""
import logging
import aiohttp
from datetime import datetime
from typing import Optional
from config.settings import Settings
from src.strategy import Signal

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.token   = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def stop(self):
        if self._session:
            await self._session.close()

    # ── Core sender ───────────────────────────────────────────────────────────

    async def _send(self, text: str):
        if not self.token or not self.chat_id:
            logger.warning("Telegram not configured — skipping notification")
            return
        url = BASE_URL.format(token=self.token)
        payload = {
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Telegram error {resp.status}: {body[:200]}")
        except Exception as exc:
            logger.warning(f"Telegram send failed: {exc}")

    # ── Signal alert ──────────────────────────────────────────────────────────

    async def send_signal(self, sig: Signal):
        emoji = "🟢🚀" if sig.direction == "LONG" else "🔴🩸"
        dir_word = "LONG" if sig.direction == "LONG" else "SHORT"
        rr = abs(sig.tp_price - sig.entry_price) / (abs(sig.sl_price - sig.entry_price) + 1e-12)

        msg = (
            f"{emoji} <b>SNIPER SIGNAL — {sig.symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Direction:</b> {dir_word}\n"
            f"💲 <b>Entry:</b>     <code>{sig.entry_price}</code>\n"
            f"🎯 <b>Take Profit:</b> <code>{sig.tp_price}</code>\n"
            f"🛑 <b>Stop Loss:</b>  <code>{sig.sl_price}</code>\n"
            f"📊 <b>R:R Ratio:</b>  {rr:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>RVOL:</b>  {sig.rvol:.2f}x\n"
            f"⚡ <b>ADX:</b>   {sig.adx:.1f}\n"
            f"📡 <b>RSI:</b>   {sig.rsi:.1f}\n"
            f"📐 <b>Slope:</b> {sig.slope:.1f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Sniper Bot V50.6 | {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"⚠️ <i>Not financial advice — DYOR</i>"
        )
        await self._send(msg)
        logger.info(f"Signal sent to Telegram: {sig.symbol} {sig.direction}")

    # ── Trade executed ─────────────────────────────────────────────────────────

    async def send_trade_opened(self, sig: Signal, order_id: str, qty: float):
        emoji = "🟢" if sig.direction == "LONG" else "🔴"
        msg = (
            f"{emoji} <b>TRADE OPENED</b>\n"
            f"Symbol:   <b>{sig.symbol}</b>\n"
            f"Dir:      {sig.direction}\n"
            f"Entry:    <code>{sig.entry_price}</code>\n"
            f"Qty:      {qty}\n"
            f"TP:       <code>{sig.tp_price}</code>\n"
            f"SL:       <code>{sig.sl_price}</code>\n"
            f"Order ID: <code>{order_id}</code>"
        )
        await self._send(msg)

    async def send_trade_closed(self,
                                 symbol: str,
                                 direction: str,
                                 entry: float,
                                 exit_price: float,
                                 pnl_usdt: float,
                                 reason: str):
        win  = pnl_usdt > 0
        emoji = "✅" if win else "❌"
        msg = (
            f"{emoji} <b>TRADE CLOSED — {symbol}</b>\n"
            f"Dir:    {direction}\n"
            f"Entry:  <code>{entry}</code>\n"
            f"Exit:   <code>{exit_price}</code>\n"
            f"PnL:    <b>{'+'if win else ''}{pnl_usdt:.2f} USDT</b>\n"
            f"Reason: {reason}"
        )
        await self._send(msg)

    async def send_breakeven_moved(self, symbol: str):
        await self._send(
            f"🔒 <b>SL → Breakeven</b>\n"
            f"Symbol: {symbol}\n"
            f"50% TP hit — risk eliminated."
        )

    # ── Daily summary ──────────────────────────────────────────────────────────

    async def send_daily_summary(self,
                                  total_pnl: float,
                                  win_rate: float,
                                  total_trades: int,
                                  balance: float,
                                  best_trade: str,
                                  worst_trade: str):
        emoji = "📈" if total_pnl >= 0 else "📉"
        msg = (
            f"{'📊'} <b>DAILY SUMMARY — Sniper Bot V50.6</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>Total PnL:</b>    {'+'if total_pnl>0 else ''}{total_pnl:.2f} USDT\n"
            f"🎯 <b>Win Rate:</b>    {win_rate*100:.1f}%\n"
            f"🔢 <b>Trades:</b>      {total_trades}\n"
            f"💼 <b>Balance:</b>     {balance:.2f} USDT\n"
            f"🏆 <b>Best trade:</b>  {best_trade}\n"
            f"💀 <b>Worst trade:</b> {worst_trade}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Next scan running…"
        )
        await self._send(msg)

    # ── Misc alerts ───────────────────────────────────────────────────────────

    async def send_startup(self, symbols: list, version: str = "V50.6"):
        sym_str = "\n".join(f"  • {s}" for s in symbols)
        msg = (
            f"🤖 <b>Sniper Bot {version} started!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Monitoring symbols:\n{sym_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ Multi-TF • ADX • Kelly • Session filters ACTIVE"
        )
        await self._send(msg)

    async def send_error(self, error: str):
        await self._send(f"🚨 <b>BOT ERROR</b>\n<code>{error[:500]}</code>")
