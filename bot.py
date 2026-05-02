# -*- coding: utf-8 -*-
"""bot.py -- Phantom Edge Bot ELITE v2.0
ZigZag(5m+15m) + Supertrend(15m) + VWAP + RSI + Engulfing + Session + Correlation
Analiza TODOS los pares USDT de BingX perpetuos.
"""
from __future__ import annotations
import asyncio, signal, sys
from collections import Counter

from loguru import logger
from aiohttp import web

from config import cfg
import client as ex
from scanner import fetch_universe, get_symbols
from strategy import get_signal, _in_dead_session
from pos_manager import (
    Trade, add_trade, open_symbols, trade_count, is_halted,
    manage_positions, sync_from_exchange, get_stats, consecutive_losses,
)
import notifier

logger.remove()
logger.add(sys.stdout, level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}")

_active_symbols: list[str] = []
_cycle: int = 0
SYMBOL_REFRESH_CYCLES = 60


async def _health(_: web.Request) -> web.Response:
    stats = get_stats()
    return web.json_response({
        "status":        "halted" if stats["halted"] else "ok",
        "version":       "phantom-elite-2.0",
        "open_trades":   stats["open"],
        "daily_pnl":     stats["daily_pnl"],
        "daily_wins":    stats["daily_wins"],
        "daily_losses":  stats["daily_losses"],
        "consec_losses": stats["consec_losses"],
        "total_symbols": len(_active_symbols),
        "symbols_open":  list(open_symbols()),
    })

async def start_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.health_port)
    await site.start()
    logger.info(f"Health :{cfg.health_port}")


async def enter_trade(sig) -> None:
    if sig.symbol in open_symbols(): return
    if trade_count() >= cfg.max_positions: return
    if is_halted(): return

    # Escalate min score after consecutive losses
    min_s = cfg.min_score + 2 if consecutive_losses() >= 3 else cfg.min_score
    if sig.score < min_s:
        logger.debug(f"[SKIP] {sig.symbol} score={sig.score}<{min_s}")
        return

    size   = max(cfg.trade_usdt, 5.0)
    lev    = cfg.leverage
    margin = (size / lev) * 1.3

    bal = await ex.get_balance()
    if bal < cfg.min_balance_usdt or bal < margin:
        logger.warning(f"[SKIP] {sig.symbol} balance {bal:.2f} insuficiente")
        return

    if sig.side == "BUY"  and (sig.sl >= sig.price or sig.tp <= sig.price): return
    if sig.side == "SELL" and (sig.sl <= sig.price or sig.tp >= sig.price): return
    if abs(sig.price - sig.sl) / sig.price * 100 < 0.08: return

    await ex.set_leverage(sig.symbol, lev)
    await asyncio.sleep(0.15)

    resp = await ex.place_market_order(
        symbol=sig.symbol, side=sig.side,
        size_usdt=size, sl=sig.sl, tp=sig.tp,
    )
    code = resp.get("code", -1)
    if code not in (0, 200, None):
        logger.warning(f"[FAIL] {sig.symbol} code={code} {resp.get('msg','')}")
        return

    od  = resp.get("data", {})
    if isinstance(od, dict): od = od.get("order", od)
    qty = float(od.get("executedQty", 0) or od.get("origQty", 0))
    if qty <= 0: qty = (size * lev) / sig.price

    add_trade(Trade(
        symbol=sig.symbol, side=sig.side,
        entry=sig.price, sl=sig.sl, tp=sig.tp,
        atr=sig.atr_5m, size_usdt=size, leverage=lev,
        qty=qty, score=sig.score, vol_ratio=sig.vol_ratio,
        delta1=sig.zz_high, delta2=sig.zz_low,
        order_id=str(od.get("orderId", "")),
        bot_opened=True,
    ))

    logger.success(
        f"[ENTRADA] {sig.symbol} {sig.side} @ {sig.price:.6f}\n"
        f"  SL={sig.sl:.6f} TP={sig.tp:.6f}\n"
        f"  ZZ5 H={sig.zz_high:.4f} L={sig.zz_low:.4f} trend={sig.zz_trend}\n"
        f"  ST={'BULL' if sig.st_bull_15m else 'BEAR'} | VWAP={sig.vwap:.4f} | "
        f"RSI={sig.rsi:.1f} | vol={sig.vol_ratio:.1f}x\n"
        f"  ATR_regime={sig.atr_regime} | score={sig.score}/12"
    )
    await notifier.notify_entry(
        symbol=sig.symbol, side=sig.side, price=sig.price,
        sl=sig.sl, tp=sig.tp, size_usdt=size, leverage=lev,
        qty=qty, score=sig.score,
        delta1=sig.zz_high, delta2=sig.zz_low, vol_ratio=sig.vol_ratio,
    )


async def scan_cycle(ohlcv_map: dict) -> None:
    signals:    list = []
    rejections: Counter = Counter()
    open_syms = open_symbols()

    for sym, data in ohlcv_map.items():
        if sym in open_syms: continue

        sig, reason = get_signal(
            ohlcv_5m      = data.get(cfg.timeframe,      {}),
            ohlcv_15m     = data.get(cfg.timeframe_slow, None),
            ohlcv_1h      = None,
            symbol        = sym,
            open_syms     = open_syms,
            atr_period    = cfg.atr_period,
            atr_mult      = cfg.atr_mult,
            rr             = cfg.rr,
            min_vol_mult  = cfg.min_vol_mult,
            st_period     = cfg.st_period,
            st_mult       = cfg.st_mult,
            rsi_period    = cfg.rsi_period,
            min_atr_pct   = cfg.min_atr_pct,
            min_score     = cfg.min_score,
            zz_deviation  = cfg.zz_deviation,
            zz15_deviation = cfg.zz15_deviation,
        )
        if sig:
            signals.append(sig)
        else:
            bucket = reason.split("_")[0].split("=")[0].split(" ")[0]
            rejections[bucket] += 1

    scanned = len(ohlcv_map) - len(open_syms)
    logger.info(
        f"[ANALISIS] {scanned} pares | {len(signals)} senales | "
        f"rechazos top: {dict(rejections.most_common(6))}"
    )

    if not signals: return

    signals.sort(key=lambda s: s.score, reverse=True)
    logger.info("[TOP SENALES ELITE]")
    for s in signals[:6]:
        logger.info(
            f"  {s.symbol:16s} {s.side:4s} {s.score:2d}/12 "
            f"trend={s.zz_trend:4s} ST={'B' if s.st_bull_15m else 'S'} "
            f"RSI={s.rsi:4.0f} vol={s.vol_ratio:.1f}x "
            f"atr={s.atr_regime} VWAP={'OK' if (s.side=='BUY') == (s.price > s.vwap) else 'NO'}"
        )

    for sig in signals:
        if trade_count() >= cfg.max_positions: break
        await enter_trade(sig)


async def main_loop() -> None:
    global _active_symbols, _cycle

    await start_health_server()
    _active_symbols = await get_symbols(cfg.symbols_raw)

    logger.info("=" * 70)
    logger.info("  PHANTOM EDGE BOT ELITE v2.0")
    logger.info("  ZigZag(5m+15m) + Supertrend(15m) + VWAP + RSI")
    logger.info("  + Engulfing/Pinbar + Session + Correlation Filter")
    logger.info("  SALIDA: 25%@1R + 25%@2R + Trail (ST5/ZZ/RSI)")
    logger.info(f"  ZZ={cfg.zz_deviation}% ZZ15={cfg.zz15_deviation}% "
                f"ST({cfg.st_period},{cfg.st_mult})")
    logger.info(f"  ATRx{cfg.atr_mult} RR1:{cfg.rr} Score≥{cfg.min_score}/12")
    logger.info(f"  {max(cfg.trade_usdt,5)}USDT x{cfg.leverage} MaxPos={cfg.max_positions}")
    logger.info(f"  Simbolos: {len(_active_symbols)}")
    logger.info("=" * 70)

    bal = await ex.get_balance()
    logger.info(f"  Balance: {bal:.2f} USDT")

    await notifier.test_telegram()
    await notifier.notify(
        f"Phantom Edge Bot ELITE v2.0\n"
        f"ZigZag(5m+15m) + ST(15m) + VWAP + RSI + Engulfing\n"
        f"Session filter + Correlation filter\n"
        f"Exit: 25%@1R + 25%@2R + Trail dinamico\n"
        f"Score min {cfg.min_score}/12 | RR 1:{cfg.rr} | x{cfg.leverage}\n"
        f"Pares: {len(_active_symbols)} | Balance: {bal:.2f} USDT"
    )

    await sync_from_exchange()

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, lambda: stop_event.set())

    while not stop_event.is_set():
        try:
            if is_halted():
                await asyncio.sleep(60); continue

            if _cycle > 0 and _cycle % SYMBOL_REFRESH_CYCLES == 0:
                _active_symbols = await get_symbols(cfg.symbols_raw)
                logger.info(f"[SYMBOLS] {len(_active_symbols)} pares")

            _cycle += 1
            t0 = loop.time()

            dead = _in_dead_session()
            logger.info(
                f"CICLO {_cycle:04d} | {len(_active_symbols)} pares | "
                f"open={trade_count()}/{cfg.max_positions} | "
                f"consec_loss={consecutive_losses()} | "
                f"{'SESION MUERTA' if dead else 'sesion OK'}"
            )

            ohlcv_map = await fetch_universe(
                _active_symbols,
                tf_5m  = cfg.timeframe,
                tf_15m = cfg.timeframe_slow,
                tf_1h  = cfg.timeframe_1h,
                max_concurrent = cfg.max_concurrent,
                min_vol_mult   = cfg.min_vol_mult,
            )

            await manage_positions(ohlcv_map)

            # Skip new entries during dead session (manage still runs)
            if not dead:
                await scan_cycle(ohlcv_map)
            else:
                logger.info("[DEAD SESSION] Solo gestion de posiciones abiertas")

            elapsed   = loop.time() - t0
            sleep_for = max(0.0, cfg.scan_interval - elapsed)
            logger.info(f"Ciclo {elapsed:.1f}s | siguiente en {sleep_for:.0f}s")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[ERROR] {e}")
            await notifier.notify(f"Error: {e}")
            await asyncio.sleep(30)

    logger.info("Bot detenido limpiamente")
    await ex.close_session()


if __name__ == "__main__":
    asyncio.run(main_loop())
