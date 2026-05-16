"""telegram_bot.py — Notificaciones Telegram V50 Ultimate"""
import logging
import asyncio
import aiohttp
from datetime import datetime
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, SYMBOL, MODE, LEVERAGE

log = logging.getLogger("telegram")
BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


async def _send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado.")
        return False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{BASE}/sendMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def send(text: str) -> None:
    asyncio.run(_send(text))


def _ts() -> str:
    return datetime.utcnow().strftime("%H:%M:%S UTC")

def _date() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def _funding_line(rate: float, extreme: bool) -> str:
    pct  = rate * 100
    icon = "⚡" if extreme else "💤"
    dir_ = "longs pagan" if rate > 0 else "shorts pagan"
    return f"{icon} Funding: <code>{pct:+.4f}%</code>  [{dir_}]"

def _liq_line(liq: dict) -> str:
    cnt = liq["zone_count"]
    boost = ""
    if liq["in_long_zone"]:  boost = " — zona liq LONG activa"
    if liq["in_short_zone"]: boost = " — zona liq SHORT activa"
    return f"🗺 Liq zones: <code>{cnt}</code> detectadas{boost}"

def _score_bar(score: float) -> str:
    filled = int(score / 10)
    return "█" * filled + "░" * (10 - filled) + f" {score:.0f}/100"


def msg_start(symbol, timeframe, mode) -> str:
    icon = "🟡" if mode == "paper" else "🟢"
    return (
        f"{icon} <b>Sniper Bot V50 Ultimate — Iniciado</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Par:        <code>{symbol}</code>\n"
        f"⏱ Timeframe:  <code>{timeframe}</code>\n"
        f"⚡ Leverage:   <code>{LEVERAGE}x</code>\n"
        f"🔧 Modo:       <code>{mode.upper()}</code>\n"
        f"🕐 Inicio:     <code>{_date()}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Motor: Markov 200v + ADX Adapt. + STC + POC\n"
        f"Extra: Funding Rate + Liquidation Map"
    )


def msg_signal(direction: str, ind: dict, qty: float,
               tp: float, sl: float, balance: float,
               score: float) -> str:
    arrow  = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    price  = ind["close"]
    rr     = round(abs(tp-price)/abs(sl-price), 2) if abs(sl-price) > 0 else 0
    regime = "TENDENCIA" if ind["is_trending"] else ("RANGO" if ind["is_ranging"] else "TRANSICIÓN")
    mode_l = "🟡 PAPER" if MODE == "paper" else "💰 LIVE"
    liq    = ind["liq"]
    boost  = ""
    if direction == "long"  and ind["liq_boost_long"]:  boost = "\n🗺 <b>Boost: zona liquidación LONG</b>"
    if direction == "short" and ind["liq_boost_short"]: boost = "\n🗺 <b>Boost: zona liquidación SHORT</b>"

    return (
        f"{arrow} <b>ENTRADA</b>  {mode_l}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Par:       <code>{SYMBOL}</code>\n"
        f"💵 Precio:    <code>{price:.4f}</code>\n"
        f"📦 Cantidad:  <code>{qty:.4f}</code>  ({LEVERAGE}x)\n"
        f"🎯 TP:        <code>{tp:.4f}</code>\n"
        f"🛡 SL:        <code>{sl:.4f}</code>\n"
        f"⚖️ R:R:       <code>1:{rr}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 Confianza: {_score_bar(score)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>Indicadores</b>\n"
        f"  Slope:      <code>{ind['slope']:.1f}%</code>  (umbral {ind['adaptive_slope']:.1f}%)\n"
        f"  ADX:        <code>{ind['adx']:.1f}</code>  [{regime}]\n"
        f"  Markov Bull:<code>{ind['prob_bull']:.1f}%</code>\n"
        f"  Markov Bear:<code>{ind['prob_bear']:.1f}%</code>\n"
        f"  RVOL:       <code>{ind['rvol']:.2f}x</code>\n"
        f"  STC:        <code>{ind['stc']:.1f}</code>\n"
        f"  VWAP:       <code>{ind['vwap']:.4f}</code>\n"
        f"  POC:        <code>{ind['poc']:.4f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_funding_line(ind['funding_rate'], ind['funding_extreme'])}\n"
        f"{_liq_line(liq)}"
        f"{boost}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Balance:   <code>${balance:.2f} USDT</code>\n"
        f"🕐 <code>{_ts()}</code>"
    )


def msg_close(direction, entry, exit_price, qty, reason, balance) -> str:
    pnl  = (exit_price - entry)*qty if direction == "long" else (entry - exit_price)*qty
    pct  = (pnl / (entry*qty))*100 if entry*qty > 0 else 0
    icon = "✅" if pnl >= 0 else "❌"
    return (
        f"{icon} <b>CIERRE DE POSICIÓN</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Par:       <code>{SYMBOL}</code>\n"
        f"↗️  Dirección: <code>{'LONG' if direction=='long' else 'SHORT'}</code>\n"
        f"🔵 Entrada:   <code>{entry:.4f}</code>\n"
        f"🔴 Salida:    <code>{exit_price:.4f}</code>\n"
        f"📦 Cantidad:  <code>{qty:.4f}</code>\n"
        f"💰 PnL:       <code>{'+' if pnl>=0 else ''}{pnl:.4f} USDT ({pct:+.2f}%)</code>\n"
        f"📋 Motivo:    <code>{reason}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Balance:   <code>${balance:.2f} USDT</code>\n"
        f"🕐 <code>{_ts()}</code>"
    )


def msg_heartbeat(ind, balance, trades_today, pnl_day) -> str:
    regime = "📈 TENDENCIA" if ind["is_trending"] else ("📉 RANGO" if ind["is_ranging"] else "↔️ TRANSICIÓN")
    pnl_icon = "✅" if pnl_day >= 0 else "❌"
    return (
        f"💓 <b>Heartbeat — Sniper V50</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {SYMBOL}  <code>{_ts()}</code>\n"
        f"💵 Precio:    <code>{ind['close']:.4f}</code>\n"
        f"📊 Régimen:   {regime}  (ADX {ind['adx']:.1f})\n"
        f"🧠 Markov:    Bull <code>{ind['prob_bull']:.1f}%</code>  Bear <code>{ind['prob_bear']:.1f}%</code>\n"
        f"📦 RVOL:      <code>{ind['rvol']:.2f}x</code>  STC <code>{ind['stc']:.1f}</code>\n"
        f"{_funding_line(ind['funding_rate'], ind['funding_extreme'])}\n"
        f"{_liq_line(ind['liq'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Balance:   <code>${balance:.2f} USDT</code>\n"
        f"🔢 Trades hoy: <code>{trades_today}</code>\n"
        f"{pnl_icon} PnL hoy:    <code>{'+' if pnl_day>=0 else ''}{pnl_day:.4f} USDT</code>"
    )


def msg_funding_alert(symbol, rate, direction) -> str:
    pct  = rate * 100
    side = "LONG" if rate < 0 else "SHORT"
    return (
        f"⚡ <b>Funding Rate Extremo</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Par:     <code>{symbol}</code>\n"
        f"📊 Rate:    <code>{pct:+.4f}%</code>\n"
        f"💡 Sesgo:   <code>{side} favorecido</code>\n"
        f"🕐 <code>{_ts()}</code>"
    )


def msg_error(context, error) -> str:
    return (
        f"⚠️ <b>ERROR — Sniper V50</b>\n"
        f"📍 <code>{context}</code>\n"
        f"❗ <code>{error}</code>\n"
        f"🕐 <code>{_ts()}</code>"
    )
