"""
strategy.py — Estrategia ZigZag Breakout multi-símbolo.

Por cada símbolo activo:
  1. Calcular ZigZag++ → obtener resistencia y soporte
  2. Si precio rompe resistencia → LONG  (TP +45 pips, SL -30 pips, 10x)
  3. Si precio rompe soporte    → SHORT (TP -45 pips, SL +30 pips, 10x)
  4. Monitorizar posiciones abiertas → detectar cierre → notificar P&L
"""
from __future__ import annotations
import logging, time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, Optional

import config
import bingx_client as bx
import telegram_notifier as tg
from zigzag import get_breakout_levels

logger = logging.getLogger(__name__)


# ── Posición activa ────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol:    str
    direction: str        # "LONG" | "SHORT"
    entry:     float
    tp:        float
    sl:        float
    qty:       float
    opened_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_sig:  str = ""   # evitar re-entrada en mismo nivel


# ── Estado global ──────────────────────────────────────────────────────────

positions:    Dict[str, Position] = {}   # symbol → Position
signal_cache: Dict[str, str]      = {}   # symbol → last signal_id
stats = {
    "trades": 0, "wins": 0, "losses": 0,
    "pnl": 0.0, "day": date.today(),
}


# ── Utilidades ─────────────────────────────────────────────────────────────

def _pip(symbol: str) -> float:
    return bx.pip_size(symbol)

def _tp(entry: float, direction: str, symbol: str) -> float:
    p = config.TP_PIPS * _pip(symbol)
    return entry + p if direction == "LONG" else entry - p

def _sl(entry: float, direction: str, symbol: str) -> float:
    p = config.SL_PIPS * _pip(symbol)
    return entry - p if direction == "LONG" else entry + p

def _in_hours() -> bool:
    h = datetime.utcnow().hour
    return config.TRADE_START_HOUR <= h <= config.TRADE_END_HOUR

def _reset_daily():
    today = date.today()
    if stats["day"] != today:
        stats.update({"trades":0,"wins":0,"losses":0,"pnl":0.0,"day":today})


# ── Verificar cierres ──────────────────────────────────────────────────────

def check_all_closed() -> None:
    """
    Compara posiciones abiertas en BingX con las que tenemos registradas.
    Si una posición ya no existe en BingX → fue cerrada (TP/SL) → notificar P&L.
    """
    if not positions:
        return
    try:
        open_on_exchange = {p["symbol"] for p in bx.get_all_open_positions()}
    except Exception as e:
        logger.error("check_all_closed: %s", e)
        return

    for sym in list(positions.keys()):
        if sym not in open_on_exchange:
            pos = positions.pop(sym)
            _handle_closed(pos)


def _handle_closed(pos: Position) -> None:
    """Calcula P&L y notifica cierre de posición."""
    try:
        ticker     = bx.get_ticker(pos.symbol)
        exit_price = float(ticker.get("lastPrice", pos.entry))
    except Exception:
        exit_price = pos.entry

    if pos.direction == "LONG":
        pnl    = (exit_price - pos.entry) * pos.qty
        reason = "✅ Take Profit" if exit_price >= pos.tp * 0.998 else "🛑 Stop Loss"
    else:
        pnl    = (pos.entry - exit_price) * pos.qty
        reason = "✅ Take Profit" if exit_price <= pos.tp * 1.002 else "🛑 Stop Loss"

    if pnl >= 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    stats["pnl"] += pnl

    tg.order_closed(pos.direction, pos.symbol, pos.entry, exit_price, pnl, reason)
    logger.info("CERRADA %s %s | entrada %.6g | salida %.6g | P&L %.2f USDT | %s",
                pos.direction, pos.symbol, pos.entry, exit_price, pnl, reason)


# ── Abrir posición ─────────────────────────────────────────────────────────

def open_trade(symbol: str, direction: str, price: float) -> bool:
    """Abre una posición en BingX con TP y SL automáticos."""
    try:
        bx.set_leverage(symbol, config.LEVERAGE)
        tp  = _tp(price, direction, symbol)
        sl  = _sl(price, direction, symbol)
        qty = bx.calc_quantity(config.CAPITAL_PER_TRADE, price, config.LEVERAGE)

        if qty <= 0:
            logger.warning("Cantidad cero para %s precio %.6g", symbol, price)
            return False

        side = "BUY" if direction == "LONG" else "SELL"
        bx.place_market_order(symbol, side, qty, round(tp,8), round(sl,8))

        pos = Position(symbol=symbol, direction=direction,
                       entry=price, tp=tp, sl=sl, qty=qty)
        positions[symbol] = pos
        stats["trades"] += 1

        tg.order_opened(direction, symbol, price, tp, sl, qty, config.CAPITAL_PER_TRADE)
        logger.info("ABIERTA %s %s | precio %.6g | TP %.6g | SL %.6g | qty %.4f",
                    direction, symbol, price, tp, sl, qty)
        return True

    except Exception as e:
        logger.error("open_trade %s: %s", symbol, e)
        tg.error(f"No se pudo abrir {symbol}: {e}")
        return False


# ── Escanear un símbolo ────────────────────────────────────────────────────

def scan_symbol(symbol: str) -> str:
    """
    Analiza un símbolo. Retorna: 'long' | 'short' | 'none' | 'error'
    """
    try:
        candles = bx.get_klines(symbol, config.BINGX_INTERVAL, config.ZZ_LOOKBACK + 10)
    except Exception as e:
        logger.debug("klines %s: %s", symbol, e)
        return "error"

    if len(candles) < config.ZZ_LOOKBACK:
        return "none"

    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]

    resistance, support = get_breakout_levels(
        highs, lows, config.ZZ_DEPTH, config.ZZ_DEVIATION, config.ZZ_BACKSTEP
    )
    if resistance is None or support is None:
        return "none"

    price = closes[-1]
    sig_id = f"{resistance:.8g}_{support:.8g}"

    # Evitar re-entrada en el mismo nivel
    if signal_cache.get(symbol) == sig_id:
        return "none"

    if price > resistance:
        signal_cache[symbol] = sig_id
        return "long"
    elif price < support:
        signal_cache[symbol] = sig_id
        return "short"

    return "none"


# ── Ciclo principal ────────────────────────────────────────────────────────

def run_scan_cycle(symbols: list) -> None:
    """
    Ejecuta un ciclo completo: verifica cierres + escanea todos los símbolos.
    """
    _reset_daily()
    logger.info("═══ Ciclo iniciado | %d símbolos | %d posiciones abiertas ═══",
                len(symbols), len(positions))

    # 1. Comprobar si alguna posición se cerró
    check_all_closed()

    if not _in_hours():
        logger.info("Fuera de horario de trading (UTC %dh–%dh).",
                    config.TRADE_START_HOUR, config.TRADE_END_HOUR)
        return

    # 2. Escanear símbolos
    new_signals  = 0
    skipped      = 0
    errors       = 0

    for symbol in symbols:
        # Límite de posiciones simultáneas
        if len(positions) >= config.MAX_OPEN_POSITIONS:
            skipped += len(symbols) - symbols.index(symbol)
            logger.info("Límite de %d posiciones alcanzado.", config.MAX_OPEN_POSITIONS)
            break

        # Ya tenemos posición abierta en este símbolo
        if symbol in positions:
            time.sleep(config.SCAN_DELAY_SEC * 0.5)
            continue

        signal = scan_symbol(symbol)
        time.sleep(config.SCAN_DELAY_SEC)   # respetar rate limit

        if signal in ("long", "short"):
            direction = signal.upper()
            try:
                ticker = bx.get_ticker(symbol)
                price  = float(ticker.get("lastPrice", 0))
            except Exception:
                errors += 1
                continue

            if price <= 0:
                continue

            # Notificar señal
            try:
                candles = bx.get_klines(symbol, config.BINGX_INTERVAL, config.ZZ_LOOKBACK + 10)
                highs = [c["high"] for c in candles]
                lows  = [c["low"]  for c in candles]
                r, s  = get_breakout_levels(highs, lows, config.ZZ_DEPTH,
                                            config.ZZ_DEVIATION, config.ZZ_BACKSTEP)
                tg.signal_detected(direction, symbol, price, r or 0, s or 0)
            except Exception:
                pass

            if open_trade(symbol, direction, price):
                new_signals += 1

        elif signal == "error":
            errors += 1

    logger.info("Ciclo completo | señales: %d | omitidos: %d | errores: %d",
                new_signals, skipped, errors)

    # Notificar resumen solo si hay actividad o cada 4 ciclos
    if new_signals > 0 or skipped > 0:
        tg.scan_summary(len(symbols), new_signals, skipped)
