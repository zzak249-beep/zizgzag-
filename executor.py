"""
Ejecucion de una señal del patron para un simbolo especifico. Mismo
patron MARKET+STOP_MARKET+TAKE_PROFIT_MARKET ya validado en produccion,
parametrizado por simbolo en vez de fijo a uno solo.
"""
import logging

import config as cfg
from bingx_client import BingXClient, BingXError
from state import StateManager
from telegram_notifier import TelegramNotifier

log = logging.getLogger("executor")


def _sides(direction: str):
    if direction == "SHORT":
        return "SELL", "SHORT"
    return "BUY", "LONG"


def _exit_sides(direction: str):
    if direction == "SHORT":
        return "BUY", "SHORT"
    return "SELL", "LONG"


async def open_signal(client: BingXClient, state: StateManager, notifier: TelegramNotifier,
                       symbol: str, direction: str, entry: float, sl: float, tp: float, pattern_key: str, tier: str = "altcoin") -> None:
    r_dist = abs(entry - sl)
    if r_dist <= 0:
        log.warning("%s: distancia de riesgo <= 0, se omite la señal.", symbol)
        return

    msg_common = (
        f"{'🔴 SHORT' if direction == 'SHORT' else '🟢 LONG'} {symbol} [Tres Montañas]\n"
        f"Entrada: {entry:.6g}  SL: {sl:.6g}  TP: {tp:.6g}\n"
        f"R:R: {abs(tp - entry) / r_dist:.2f}"
    )

    if cfg.MODE != "LIVE":
        state.open_position(symbol, direction, entry, sl, tp, pattern_key, tier)
        state.save()
        await notifier.send(f"[Papel] {msg_common}")
        log.info("[Papel] %s: señal registrada %s @ %.6g", symbol, direction, entry)
        return

    # ── MODE=LIVE: ordenes reales ──
    try:
        balance = await client.get_balance()
        equity = float(balance.get("balance", {}).get("equity", 0) or 0)
    except (BingXError, TypeError, ValueError, KeyError) as e:
        log.error("%s: no se pudo leer equity, señal ignorada: %s", symbol, e)
        return

    if equity <= 0:
        log.error("%s: equity=0 o no disponible, no se abre posicion.", symbol)
        return

    raw_qty = (equity * cfg.RISK_PCT / 100.0) / r_dist
    qty = client.round_qty(symbol, raw_qty)
    if qty <= 0:
        log.warning("%s: qty calculado es 0 tras redondear, se omite.", symbol)
        return

    side, pos_side = _sides(direction)

    try:
        await client.place_order(symbol, side, pos_side, "MARKET", quantity=qty)
    except BingXError as e:
        log.error("%s: fallo al abrir posicion: %s", symbol, e)
        await notifier.send(f"❌ {symbol}: fallo al abrir — {e.msg}")
        return

    real_qty, real_entry = qty, entry
    try:
        positions = await client.get_positions(symbol)
        for p in positions:
            amt = float(p.get("positionAmt", 0) or 0)
            if abs(amt) > 0:
                real_qty = abs(amt)
                real_entry = float(p.get("avgPrice", p.get("entryPrice", entry)) or entry)
                break
    except (BingXError, TypeError, ValueError) as e:
        log.warning("%s: no se pudo releer la posicion real: %s", symbol, e)

    sl_px = client.round_price(symbol, sl)
    tp_px = client.round_price(symbol, tp)
    exit_side, exit_pos_side = _exit_sides(direction)

    try:
        await client.place_order(symbol, exit_side, exit_pos_side, "STOP_MARKET",
                                  quantity=real_qty, stop_price=sl_px, reduce_only=True)
        await client.place_order(symbol, exit_side, exit_pos_side, "TAKE_PROFIT_MARKET",
                                  quantity=real_qty, stop_price=tp_px, reduce_only=True)
    except BingXError as e:
        log.error("%s: posicion abierta pero fallo al colocar SL/TP: %s -- REVISAR.", symbol, e)
        await notifier.send(f"🚨 {symbol}: posicion abierta SIN SL/TP completo ({e.msg}). Revisa manualmente.")

    state.open_position(symbol, direction, real_entry, sl, tp, pattern_key, tier)
    state.save()
    await notifier.send(f"[EN VIVO] {msg_common}\nQty real: {real_qty}")
    log.info("%s: posicion LIVE abierta %s qty=%.6g @ %.6g", symbol, direction, real_qty, real_entry)


async def check_paper_exit(state: StateManager, notifier: TelegramNotifier, symbol: str, last_close: float) -> None:
    """Solo para MODE=SIGNAL. Mismo criterio y misma limitacion documentada
    en el bot de un solo simbolo: compara el CIERRE mas reciente, no
    tick-a-tick -- hasta SCAN_INTERVAL_SEC de retraso frente a un
    movimiento muy rapido intra-vela."""
    if cfg.MODE == "LIVE":
        return
    pos = state.positions.get(symbol)
    if not pos:
        return
    direction = pos["dir"]
    sl, tp = pos["sl"], pos["tp"]
    hit_sl = last_close <= sl if direction == "LONG" else last_close >= sl
    hit_tp = last_close >= tp if direction == "LONG" else last_close <= tp
    if hit_sl:
        state.close_position(symbol, win=False)
        state.save()
        await notifier.send(f"[Papel] ❌ SL tocado — {symbol} {direction} @ {last_close:.6g}")
    elif hit_tp:
        state.close_position(symbol, win=True)
        state.save()
        await notifier.send(f"[Papel] ✅ TP tocado — {symbol} {direction} @ {last_close:.6g}")
