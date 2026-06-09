"""
QF×JP v3.5 PREDATOR — Multi-Symbol Scanner Bot
Scans ALL BingX perpetuals for TL Breakout + composite score signals.
No TradingView needed — everything computed from BingX OHLCV data.
"""
import asyncio
import logging
import os
import signal as _signal
import sys

from .config import settings
from .bingx_client import BingXClient
from .telegram_client import TelegramClient
from .engine import StrategyEngine
from .scanner import MultiSymbolScanner
from .risk_manager import RiskManager
from .state import BotState

# ── Logging ────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("qfjp.bot")

TIER_EMOJI = {"SUP": "⭐⭐⭐", "FUEL": "⭐⭐", "STD": "⭐", "PRE": "⚡"}


def build_message(sig: dict, order: dict) -> str:
    side_emoji = "🟢 LONG" if sig["side"] == "LONG" else "🔴 SHORT"
    stars = TIER_EMOJI.get(sig["tier"], "")
    extras = []
    if sig.get("tl_break_long") or sig.get("tl_break_short"):
        extras.append("📈 TL RUPTURA")
    if sig.get("vdi"):      extras.append("⚡VDI")
    if sig.get("sweep"):    extras.append("💧SWP")
    if sig.get("choch"):    extras.append("🔄CHoCH")
    if sig.get("cvd_div"):  extras.append("📊CVD÷")
    ctx = "  ".join(extras) if extras else "—"

    return (
        f"{stars} *QF×JP v3.5 — {side_emoji} {sig['tier']}*\n"
        f"Par: `{sig['symbol']}`\n"
        f"Score L:`{sig['score_long']}/100`  S:`{sig['score_short']}/100`\n"
        f"ADX: `{sig['adx']:.1f}` ({sig['reg_label']})  CVD:`{sig['cvd_score']:.2f}`\n"
        f"RSI: `{sig['rsi']:.0f}`  MFI:`{sig['mfi']:.0f}`  Sesión:`{sig['session']}`\n"
        f"HTF L:`{sig['htf_long']}`/3  S:`{sig['htf_short']}`/3  "
        f"Conv L:`{sig['conv_long']}`  S:`{sig['conv_short']}`\n"
        f"Entrada: `{order.get('price', '—')}`\n"
        f"SL: `{order.get('sl_price', '—')}`\n"
        f"TP1: `{order.get('tp1_price', '—')}`  TP2:`{order.get('tp2_price', '—')}`\n"
        f"R:R: `{order.get('rr1', '—')}`  Tamaño:`{order.get('origQty', '—')} u`\n"
        f"Contexto: {ctx}\n"
        f"ID: `{order.get('orderId', '—')}`"
    )


def build_scan_summary(signals: list[dict], total: int) -> str:
    if not signals:
        return f"🔍 Scan completo — {total} pares — sin señales activas"
    lines = [f"🔍 *Scan QF×JP* — {total} pares — {len(signals)} señal(es)\n"]
    for s in signals[:8]:  # top 8 to avoid spam
        side_e = "🟢" if s["side"] == "LONG" else "🔴"
        tl = "🔥TL" if (s["tl_break_long"] or s["tl_break_short"]) else ""
        lines.append(
            f"{side_e} `{s['symbol']:18s}` {s['tier']:4s} "
            f"L:{s['score_long']:3d} S:{s['score_short']:3d} {tl}"
        )
    return "\n".join(lines)


async def main_loop(
    bingx: BingXClient,
    telegram: TelegramClient,
    engine: StrategyEngine,
    scanner: MultiSymbolScanner,
    risk: RiskManager,
    state: BotState,
):
    log.info(f"⏱  Scan interval: {settings.SCAN_INTERVAL}s | TF: {settings.TIMEFRAME} | "
             f"MaxSymbols: {settings.MAX_SYMBOLS}")

    scan_count = 0

    while True:
        try:
            scan_count += 1
            log.info(f"🔍 Scan #{scan_count} starting…")

            # ── 1. Get all symbols ────────────────────────────────────────
            all_symbols = await bingx.get_all_symbols(
                min_volume=settings.MIN_VOLUME_USDT
            )
            symbols = all_symbols[: settings.MAX_SYMBOLS]
            log.info(f"   {len(symbols)} symbols loaded (filtered by volume)")

            # ── 2. Scan all symbols ───────────────────────────────────────
            signals = await scanner.scan_all(bingx, engine, symbols)
            log.info(f"   {len(signals)} actionable signals found")

            # ── 3. Summary to Telegram every 10 scans ────────────────────
            if scan_count % 10 == 1:
                summary = build_scan_summary(signals, len(symbols))
                await telegram.send(summary)

            # ── 4. Execute top signals ────────────────────────────────────
            for sig in signals:
                if not risk.can_trade(state, sig):
                    log.debug(f"  Blocked {sig['symbol']}: {risk.last_reject_reason}")
                    continue

                price = await bingx.get_price(sig["symbol"])
                if price <= 0:
                    continue

                order = await bingx.place_order(sig, risk, price)
                if order:
                    state.record_trade(sig, order)
                    msg = build_message(sig, order)
                    await telegram.send(msg)
                    log.info(
                        f"✅ {sig['side']} {sig['tier']} {sig['symbol']} "
                        f"@ {price} | order {order.get('orderId')}"
                    )
                else:
                    await telegram.send(
                        f"⚠️ Orden fallida: `{sig['symbol']}` {sig['side']}"
                    )

                # Don't spam — wait a bit between orders
                await asyncio.sleep(1.5)

            # ── 5. Periodic status ────────────────────────────────────────
            if state.should_send_status(every=30):
                balance = await bingx.get_balance()
                await telegram.send(
                    f"📊 *Status QF×JP v3.5*\n"
                    f"{state.summary}\n"
                    f"Balance disponible: `${balance:.2f} USDT`\n"
                    f"Scans realizados: `{scan_count}`"
                )

        except Exception as exc:
            log.exception(f"Main loop error: {exc}")
            await telegram.send(f"⚠️ Error en el bot:\n`{exc}`")
            await asyncio.sleep(30)

        await asyncio.sleep(settings.SCAN_INTERVAL)


async def run():
    log.info("🚀 QF×JP v3.5 PREDATOR Multi-Symbol Scanner starting…")

    bingx    = BingXClient(settings.BINGX_API_KEY, settings.BINGX_SECRET_KEY)
    telegram = TelegramClient(settings.TELEGRAM_TOKEN, settings.TELEGRAM_CHAT_ID)
    engine   = StrategyEngine(settings)
    scanner  = MultiSymbolScanner(settings)
    risk     = RiskManager(settings)
    state    = BotState()

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig, lambda: asyncio.create_task(_shutdown(telegram))
            )
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    await telegram.send(
        f"🤖 *QF×JP v3.5 PREDATOR* iniciado ✅\n"
        f"TF: `{settings.TIMEFRAME}` | Scan: cada `{settings.SCAN_INTERVAL}s`\n"
        f"Pares: top `{settings.MAX_SYMBOLS}` por volumen\n"
        f"Capital: `${settings.CAPITAL}` | Riesgo: `{settings.RISK_PCT}%` | "
        f"Leverage: `{settings.LEVERAGE}x`\n"
        f"Tier mínimo: `{settings.MIN_TIER}` | "
        f"TL break requerido: `{settings.REQUIRE_TL_BREAK}`\n"
        f"Max trades: `{settings.MAX_OPEN_TRADES}` open / `{settings.MAX_DAILY_TRADES}` diarios"
    )

    await main_loop(bingx, telegram, engine, scanner, risk, state)


async def _shutdown(telegram: TelegramClient):
    log.info("Shutting down…")
    await telegram.send("⛔ Bot detenido.")
    asyncio.get_event_loop().stop()


if __name__ == "__main__":
    asyncio.run(run())
