"""
Notificaciones Telegram — v2
Mensajes detallados de entrada, salida, PnL y resumen diario.
"""
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone

logger = logging.getLogger("telegram")

MAX_RETRIES  = 3
RETRY_DELAY  = 2


async def send(token: str, chat_id: str, text: str) -> bool:
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(url, json=payload) as r:
                    body = await r.json(content_type=None)
                    if r.status == 200 and body.get("ok"):
                        return True
                    logger.warning(f"Telegram intento {attempt}: {r.status} {body}")
        except Exception as e:
            logger.warning(f"Telegram intento {attempt}: {e}")
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)
    logger.error("Telegram: mensaje perdido")
    return False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _pnl_emoji(pnl: float) -> str:
    if pnl > 0:
        return "💚"
    elif pnl < 0:
        return "❤️"
    return "⬜"


# ── Mensajes de trading ───────────────────────────────────────────────────────

def msg_bot_start(balance: float, total_pairs: int, trade_usdt: float, leverage: int, max_trades: int) -> str:
    return (
        f"🤖 <b>Maki Bot iniciado</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Balance: <code>{balance:.2f} USDT</code>\n"
        f"🔍 Escaneando: <code>{total_pairs}</code> pares\n"
        f"💵 Por trade: <code>{trade_usdt} USDT</code> × <code>{leverage}x</code> "
        f"= <code>{trade_usdt * leverage:.0f} USDT nocional</code>\n"
        f"📊 Max posiciones: <code>{max_trades}</code>\n"
        f"⏰ {_now()}"
    )


def msg_trade_open(
    symbol: str, side: str, entry: float,
    tp: float, sl: float, qty: float,
    usdt: float, leverage: int, rr: float,
    atr_pct: float | None = None,
) -> str:
    emoji  = "📈" if side == "LONG" else "📉"
    color  = "🟢" if side == "LONG" else "🔴"
    tp_pct = abs(tp - entry) / entry * 100
    sl_pct = abs(sl - entry) / entry * 100
    nocional = usdt * leverage
    riesgo   = usdt * (sl_pct / 100) * leverage  # pérdida máxima en USDT

    atr_line = f"\n📐 ATR: <code>{atr_pct:.2f}%</code>" if atr_pct else ""

    return (
        f"{emoji} <b>{side} abierto — {symbol}</b> {color}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Entrada:   <code>{entry:.6f}</code>\n"
        f"✅ Take Profit: <code>{tp:.6f}</code>  (+{tp_pct:.2f}%)\n"
        f"🛑 Stop Loss:   <code>{sl:.6f}</code>  (-{sl_pct:.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Qty: <code>{qty:.4f}</code> | Nocional: <code>{nocional:.2f} USDT</code>\n"
        f"⚠️ Riesgo máx: <code>~{riesgo:.2f} USDT</code>\n"
        f"⚖️ R/R: <code>{rr:.2f}</code>{atr_line}\n"
        f"⏰ {_now()}"
    )


def msg_trade_close(
    symbol: str, side: str,
    entry: float, exit_price: float,
    qty: float, usdt_margin: float, leverage: int,
    reason: str,
) -> str:
    # PnL bruto en USDT (sin comisiones)
    if side == "LONG":
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100
    pnl_usdt = usdt_margin * leverage * (pnl_pct / 100)

    hit_tp  = "TP" in reason.upper()
    hit_sl  = "SL" in reason.upper()
    emoji   = "🏆" if hit_tp else ("💀" if hit_sl else "🚪")
    pnlemj  = _pnl_emoji(pnl_usdt)

    return (
        f"{emoji} <b>{reason} — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Dirección: <code>{side}</code>\n"
        f"🎯 Entrada: <code>{entry:.6f}</code>\n"
        f"🏁 Salida:  <code>{exit_price:.6f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{pnlemj} PnL: <code>{pnl_usdt:+.2f} USDT</code>  ({pnl_pct:+.2f}%)\n"
        f"⏰ {_now()}"
    )


def msg_daily_summary(stats: dict) -> str:
    """
    stats = {
      'trades': int, 'wins': int, 'losses': int,
      'total_pnl': float, 'best': (symbol, pnl), 'worst': (symbol, pnl)
    }
    """
    trades   = stats.get("trades", 0)
    wins     = stats.get("wins", 0)
    losses   = stats.get("losses", 0)
    total    = stats.get("total_pnl", 0.0)
    wr       = (wins / trades * 100) if trades > 0 else 0
    best     = stats.get("best")
    worst    = stats.get("worst")
    pnlemj   = _pnl_emoji(total)

    best_line  = f"\n🥇 Mejor: <code>{best[0]}</code> +{best[1]:.2f} USDT"   if best  else ""
    worst_line = f"\n💔 Peor:  <code>{worst[0]}</code> {worst[1]:.2f} USDT" if worst else ""

    return (
        f"📊 <b>Resumen del día</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Trades: <code>{trades}</code>  |  ✅ Wins: <code>{wins}</code>  |  ❌ Losses: <code>{losses}</code>\n"
        f"🎯 Win rate: <code>{wr:.1f}%</code>\n"
        f"{pnlemj} PnL total: <code>{total:+.2f} USDT</code>"
        f"{best_line}{worst_line}\n"
        f"⏰ {_now()}"
    )


def msg_error(text: str) -> str:
    return f"⚠️ <b>Error en bot</b>\n<code>{text[:300]}</code>\n⏰ {_now()}"


def msg_scan_status(scanned: int, total: int, open_trades: int, max_trades: int, balance: float) -> str:
    return (
        f"🔍 <b>Escaneo completado</b>\n"
        f"Pares revisados: <code>{scanned}/{total}</code>\n"
        f"Posiciones: <code>{open_trades}/{max_trades}</code>\n"
        f"Balance: <code>{balance:.2f} USDT</code>\n"
        f"⏰ {_now()}"
    )
