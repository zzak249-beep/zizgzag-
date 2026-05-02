# -*- coding: utf-8 -*-
"""
Maki Bot v4 -- ZigZag + 20MA 4H + RSI + Volumen para BingX Futures
Alineado con indicador Pine Script de TradingView:
  - Señal en 5m  (entradas más precisas sobre/bajo ruptura)
  - Filtro MA20 en 4H
  - Crossover/crossunder fiel al indicador TV
  - TP/SL automático ATR (2x / 1x)
  - Notificaciones Telegram con PnL real
  - Resumen diario a las 00:00 UTC
  - Estado persistente en disco
  - Healthcheck HTTP para Railway
"""
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web

from bingx    import BingXClient
from strategy import signal as get_signal, tp_sl, risk_reward, get_rsi_value, get_atr_pct
import telegram as tg

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot")

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.environ["BINGX_API_KEY"]
API_SECRET = os.environ["BINGX_API_SECRET"]
TG_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT    = os.environ["TELEGRAM_CHAT_ID"]

TRADE_USDT   = float(os.environ.get("TRADE_AMOUNT_USDT", "10"))
MAX_TRADES   = int(os.environ.get("MAX_OPEN_TRADES", "5"))
SCAN_SEC     = int(os.environ.get("SCAN_INTERVAL_SECONDS", "30"))   # 30s — más reactivo en 5m
LEVERAGE     = int(os.environ.get("LEVERAGE", "10"))
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SECONDS", "300"))
SCAN_BATCH   = int(os.environ.get("SCAN_BATCH_SIZE", "10"))
MIN_VOLUME   = float(os.environ.get("MIN_VOLUME_USDT", "500000"))
MIN_RR       = float(os.environ.get("MIN_RR", "1.5"))
PORT         = int(os.environ.get("PORT", "8080"))

# Velas 5m: cuántas cargar. 200 = ~16h, suficiente para pivots + ATR
CANDLES_5M  = int(os.environ.get("CANDLES_5M", "200"))
CANDLES_4H  = int(os.environ.get("CANDLES_4H", "50"))

# ── Estado persistente ────────────────────────────────────────────────────────
STATE_FILE = Path("/tmp/maki_state.json")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"trades": {}, "cooldowns": {}, "daily": {}}


def _save_state():
    try:
        STATE_FILE.write_text(json.dumps({
            "trades":    open_trades,
            "cooldowns": cooldowns,
            "daily":     daily_stats,
        }, default=str))
    except Exception as e:
        logger.warning(f"save_state: {e}")


state        = _load_state()
open_trades: dict = state.get("trades", {})
cooldowns:   dict = state.get("cooldowns", {})
daily_stats: dict = state.get("daily", {
    "date": "", "trades": 0, "wins": 0, "losses": 0,
    "total_pnl": 0.0, "best": None, "worst": None,
})

# ── Helpers ───────────────────────────────────────────────────────────────────

async def notify(text: str):
    await tg.send(TG_TOKEN, TG_CHAT, text)


def _in_cooldown(symbol: str) -> bool:
    return datetime.now(timezone.utc).timestamp() < cooldowns.get(symbol, 0)


def _set_cooldown(symbol: str):
    cooldowns[symbol] = datetime.now(timezone.utc).timestamp() + COOLDOWN_SEC


def _reset_daily_if_new_day():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if daily_stats.get("date") != today:
        daily_stats.update({
            "date": today, "trades": 0, "wins": 0,
            "losses": 0, "total_pnl": 0.0,
            "best": None, "worst": None,
        })


def _record_closed_trade(symbol: str, pnl: float):
    _reset_daily_if_new_day()
    daily_stats["trades"] += 1
    daily_stats["total_pnl"] += pnl
    if pnl >= 0:
        daily_stats["wins"] += 1
        if daily_stats["best"] is None or pnl > daily_stats["best"][1]:
            daily_stats["best"] = [symbol, pnl]
    else:
        daily_stats["losses"] += 1
        if daily_stats["worst"] is None or pnl < daily_stats["worst"][1]:
            daily_stats["worst"] = [symbol, pnl]
    _save_state()


# ── Healthcheck ───────────────────────────────────────────────────────────────

async def _health(_: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_healthcheck():
    app    = web.Application()
    app.router.add_get("/",       _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Healthcheck en puerto {PORT}")


# ── Monitor de posiciones abiertas ────────────────────────────────────────────

async def monitor(client: BingXClient):
    """
    1. Detecta cierres automáticos por BingX (TP/SL ejecutado).
    2. Red de seguridad manual si no se ejecutó la orden.
    """
    if not open_trades:
        return

    try:
        real_positions = {p["symbol"]: p for p in await client.get_open_positions()}
    except Exception:
        real_positions = {}

    for symbol, trade in list(open_trades.items()):
        try:
            side  = trade["side"]
            entry = float(trade["entry"])
            qty   = float(trade["qty"])
            tp    = float(trade["tp"])
            sl    = float(trade["sl"])
            usdt  = float(trade.get("usdt", TRADE_USDT))
            lev   = int(trade.get("leverage", LEVERAGE))

            # ── Cierre automático detectado (posición ya no existe en BingX) ──
            if symbol not in real_positions:
                price = await client.last_price(symbol)
                if side == "LONG":
                    pnl_pct = (price - entry) / entry * 100
                else:
                    pnl_pct = (entry - price) / entry * 100
                pnl_usdt = usdt * lev * (pnl_pct / 100)
                reason   = "TP (auto)" if pnl_usdt >= 0 else "SL (auto)"

                msg = tg.msg_trade_close(symbol, side, entry, price, qty, usdt, lev, reason)
                logger.info(f"Auto-cerrado {symbol} {side} {reason} pnl={pnl_usdt:+.4f} USDT")
                await notify(msg)
                _record_closed_trade(symbol, pnl_usdt)
                del open_trades[symbol]
                _set_cooldown(symbol)
                _save_state()
                continue

            # ── Red de seguridad manual ───────────────────────────────────────
            price  = await client.last_price(symbol)
            hit_tp = price >= tp if side == "LONG" else price <= tp
            hit_sl = price <= sl if side == "LONG" else price >= sl

            if hit_tp or hit_sl:
                reason = "TP" if hit_tp else "SL"
                try:
                    exit_price = await client.close_position(symbol, side, qty)
                    await client.cancel_all_orders(symbol)
                except Exception as e:
                    logger.warning(f"close_position {symbol}: {e}")
                    exit_price = price

                if side == "LONG":
                    pnl_pct = (exit_price - entry) / entry * 100
                else:
                    pnl_pct = (entry - exit_price) / entry * 100
                pnl_usdt = usdt * lev * (pnl_pct / 100)

                msg = tg.msg_trade_close(symbol, side, entry, exit_price, qty, usdt, lev, reason)
                logger.info(f"Cerrado {symbol} {side} {reason} @ {exit_price:.6f} pnl={pnl_usdt:+.4f}")
                await notify(msg)
                _record_closed_trade(symbol, pnl_usdt)
                del open_trades[symbol]
                _set_cooldown(symbol)
                _save_state()

        except Exception as e:
            logger.warning(f"monitor {symbol}: {e}")


# ── Scanner ───────────────────────────────────────────────────────────────────

async def scan_symbol(client: BingXClient, symbol: str) -> bool:
    if symbol in open_trades or _in_cooldown(symbol):
        return False
    if len(open_trades) >= MAX_TRADES:
        return False

    try:
        # 5m para señal (más velas para tener pivots bien formados), 4H para filtro MA
        c5m, c4h = await asyncio.gather(
            client.klines(symbol, "5m",  limit=CANDLES_5M),
            client.klines(symbol, "4h",  limit=CANDLES_4H),
        )

        if len(c5m) < 20 or len(c4h) < 22:
            return False

        sig = get_signal(c5m, c4h)
        if sig is None:
            return False

        # Precio de entrada: última vela 5m cerrada
        entry  = c5m[-2]["c"]
        tp, sl = tp_sl(entry, sig, c5m)

        rr = risk_reward(tp, sl, entry, sig)
        if rr < MIN_RR:
            logger.debug(f"{symbol} {sig}: R/R={rr:.2f} < {MIN_RR}, skip")
            return False

        atr_pct = get_atr_pct(c5m)
        rsi_val = get_rsi_value(c5m)

        order_id, qty, real_entry = await client.open_order(
            symbol, sig, TRADE_USDT, tp, sl, leverage=LEVERAGE,
        )

        # Recalcular TP/SL con el precio real de ejecución
        tp, sl = tp_sl(real_entry, sig, c5m)

        open_trades[symbol] = {
            "side": sig, "entry": real_entry, "tp": tp, "sl": sl,
            "qty": qty, "order_id": order_id,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "usdt": TRADE_USDT, "leverage": LEVERAGE,
        }
        _save_state()

        msg = tg.msg_trade_open(
            symbol, sig, real_entry, tp, sl, qty,
            TRADE_USDT, LEVERAGE, rr, atr_pct, rsi_val,
        )
        logger.info(f"Abierto: {symbol} {sig} @ {real_entry:.6f} R/R={rr:.2f}")
        await notify(msg)
        return True

    except Exception as e:
        logger.warning(f"scan {symbol}: {e}")
        return False


async def scan_all(client: BingXClient, symbols: list[str]):
    if len(open_trades) >= MAX_TRADES:
        return

    total   = len(symbols)
    scanned = 0
    opened  = 0

    for i in range(0, total, SCAN_BATCH):
        if len(open_trades) >= MAX_TRADES:
            break
        batch   = symbols[i: i + SCAN_BATCH]
        results = await asyncio.gather(
            *[scan_symbol(client, s) for s in batch],
            return_exceptions=True,
        )
        opened  += sum(1 for r in results if r is True)
        scanned += len(batch)
        await asyncio.sleep(0.5)   # menos espera entre batches — señales 5m son fugaces

    logger.info(
        f"Scan: {scanned}/{total} pares | "
        f"abiertos={opened} | total pos={len(open_trades)}"
    )


# ── Resumen diario ────────────────────────────────────────────────────────────

_last_summary_hour = -1


async def maybe_send_daily_summary():
    global _last_summary_hour
    hour = datetime.now(timezone.utc).hour
    if hour == 0 and _last_summary_hour != 0:
        _last_summary_hour = 0
        _reset_daily_if_new_day()
        if daily_stats.get("trades", 0) > 0:
            await notify(tg.msg_daily_summary(daily_stats))
    elif hour != 0:
        _last_summary_hour = hour


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main():
    await start_healthcheck()

    client = BingXClient(API_KEY, API_SECRET)

    logger.info("Cargando metadatos de contratos...")
    try:
        await client.load_contracts_cache()
    except Exception as e:
        logger.warning(f"load_contracts_cache: {e}")

    try:
        balance = await client.balance_usdt()
        logger.info(f"Balance USDT: {balance:.2f}")
    except Exception as e:
        logger.critical(f"Error balance: {e}")
        await notify(f"<b>Error al iniciar Maki Bot v4</b>\n<code>{e}</code>")
        await client.close()
        return

    try:
        ticker_map  = await client.ticker_map()
        symbols_raw = [
            sym for sym, t in ticker_map.items()
            if sym.endswith("-USDT")
            and float(t.get("quoteVolume", 0) or 0) >= MIN_VOLUME
        ]
        symbols_raw.sort(
            key=lambda s: float(ticker_map[s].get("quoteVolume", 0) or 0),
            reverse=True,
        )
    except Exception as e:
        logger.critical(f"Error al obtener pares: {e}")
        await notify(f"<b>Error pares</b>\n<code>{e}</code>")
        await client.close()
        return

    symbols = symbols_raw
    logger.info(f"Pares con volumen >= {MIN_VOLUME:,.0f} USDT: {len(symbols)}")

    if open_trades:
        logger.info(f"Trades recuperados del estado: {list(open_trades.keys())}")

    await notify(tg.msg_bot_start(balance, len(symbols), TRADE_USDT, LEVERAGE, MAX_TRADES))

    loop       = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        logger.info("Apagando bot...")
        stop_event.set()

    for sig_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig_name, _shutdown)
        except NotImplementedError:
            pass

    # Refrescar lista de pares cada ~1h
    symbol_refresh_ticks = 0
    REFRESH_EVERY = max(1, 3600 // SCAN_SEC)

    while not stop_event.is_set():
        try:
            _reset_daily_if_new_day()
            await monitor(client)
            await scan_all(client, symbols)
            await maybe_send_daily_summary()

            symbol_refresh_ticks += 1
            if symbol_refresh_ticks >= REFRESH_EVERY:
                symbol_refresh_ticks = 0
                try:
                    ticker_map = await client.ticker_map()
                    symbols = [
                        sym for sym, t in ticker_map.items()
                        if sym.endswith("-USDT")
                        and float(t.get("quoteVolume", 0) or 0) >= MIN_VOLUME
                    ]
                    symbols.sort(
                        key=lambda s: float(ticker_map[s].get("quoteVolume", 0) or 0),
                        reverse=True,
                    )
                    logger.info(f"Pares actualizados: {len(symbols)}")
                except Exception as e:
                    logger.warning(f"refresh symbols: {e}")

        except Exception as e:
            logger.error(f"Loop error: {e}", exc_info=True)
            await notify(tg.msg_error(str(e)))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SCAN_SEC)
        except asyncio.TimeoutError:
            pass

    await client.close()
    await notify("<b>Maki Bot v4 detenido.</b>")


if __name__ == "__main__":
    asyncio.run(main())
