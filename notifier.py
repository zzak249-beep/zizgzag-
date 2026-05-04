# -*- coding: utf-8 -*-
"""notifier.py -- Phantom Edge Bot v6 — Telegram notifications."""
from __future__ import annotations
import asyncio
from loguru import logger


async def _send(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        return
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    logger.warning(f"[TG] HTTP {r.status}")
    except Exception as e:
        logger.warning(f"[TG] {e}")


def _cfg():
    from config import cfg
    return cfg.telegram_token, cfg.telegram_chat_id


async def notify(text: str) -> None:
    token, chat_id = _cfg()
    await _send(token, chat_id, text)


async def test_telegram() -> None:
    token, chat_id = _cfg()
    if not token or not chat_id:
        logger.warning("[TG] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID no configurados — notificaciones desactivadas")
        return
    await _send(token, chat_id, "🤖 Phantom Edge Bot v6.2 — Telegram OK")


async def notify_entry(
    symbol: str, side: str, price: float,
    sl: float, tp: float, size_usdt: float,
    leverage: int, qty: float, score: int,
    delta1: float, delta2: float, vol_ratio: float,
) -> None:
    emoji = "🟢" if side == "BUY" else "🔴"
    rr = abs(tp - price) / abs(price - sl) if abs(price - sl) > 0 else 0
    text = (
        f"{emoji} <b>ENTRADA {side}</b> — {symbol}\n"
        f"💰 Precio: <code>{price:.6f}</code>\n"
        f"🛑 SL: <code>{sl:.6f}</code>\n"
        f"🎯 TP: <code>{tp:.6f}</code>\n"
        f"📊 RR: 1:{rr:.1f} | Score: {score}/6\n"
        f"📦 {size_usdt:.1f} USDT × {leverage}x | Vol: {vol_ratio:.1f}x\n"
        f"🔵 Peak: {delta1:.6f} | Valley: {delta2:.6f}"
    )
    await notify(text)


async def notify_exit(
    symbol: str, side: str, entry: float,
    exit_price: float, qty: float, size_usdt: float,
    leverage: int, r_achieved: float, peak_r: float,
    exit_reason: str,
) -> None:
    pct = ((exit_price - entry) / entry * 100) if side == "BUY" else ((entry - exit_price) / entry * 100)
    pnl_usdt = pct / 100 * size_usdt * leverage
    emoji = "✅" if pnl_usdt >= 0 else "❌"
    text = (
        f"{emoji} <b>SALIDA {exit_reason}</b> — {symbol}\n"
        f"📍 Entry: <code>{entry:.6f}</code> → Exit: <code>{exit_price:.6f}</code>\n"
        f"💵 PnL: <b>{pnl_usdt:+.4f} USDT</b> ({pct:+.2f}%)\n"
        f"📈 R: {r_achieved:.2f} | PeakR: {peak_r:.2f}"
    )
    await notify(text)


async def notify_partial(
    symbol: str, qty_closed: float, qty_remaining: float,
    price: float, pnl_usdt: float,
) -> None:
    text = (
        f"🟡 <b>PARCIAL</b> — {symbol}\n"
        f"💰 Precio: <code>{price:.6f}</code>\n"
        f"📦 Cerrado: {qty_closed:.6f} | Restante: {qty_remaining:.6f}\n"
        f"💵 PnL parcial: {pnl_usdt:+.4f} USDT"
    )
    await notify(text)


async def notify_breakeven(symbol: str, side: str, entry: float, r: float) -> None:
    text = (
        f"🔒 <b>BREAKEVEN</b> — {symbol}\n"
        f"SL movido a entrada: <code>{entry:.6f}</code> | R={r:.2f}"
    )
    await notify(text)


async def notify_daily_summary(
    trades: int, wins: int, losses: int, pnl: float, balance: float,
) -> None:
    wr = wins / trades * 100 if trades > 0 else 0
    text = (
        f"📊 <b>RESUMEN DIARIO</b>\n"
        f"Trades: {trades} | W: {wins} | L: {losses} | WR: {wr:.0f}%\n"
        f"PnL: <b>{pnl:+.4f} USDT</b>\n"
        f"Balance: {balance:.4f} USDT"
    )
    await notify(text)
