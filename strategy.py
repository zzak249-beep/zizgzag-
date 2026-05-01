"""
strategy.py — Lógica principal de la estrategia ZigZag Breakout.

Estrategia de Maki@テクニカル先生 (1M visitas TikTok):
────────────────────────────────────────────────────
  1. Calcular ZigZag++ sobre las últimas N velas de 15 minutos
  2. Identificar último PICO (resistencia) y último VALLE (soporte)
  3. Si el precio cierra POR ENCIMA de la resistencia → LONG
  4. Si el precio cierra POR DEBAJO del soporte       → SHORT
  5. TP = entrada ± 45 pips
  6. SL = entrada ∓ 30 pips
  7. Apalancamiento: 10x
────────────────────────────────────────────────────
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config
import bingx_client as bx
import telegram_notifier as tg
from zigzag import get_breakout_levels

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Estado del bot (en memoria; persiste mientras el proceso esté vivo)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BotState:
    in_trade:       bool  = False
    direction:      str   = ""          # "LONG" | "SHORT"
    entry_price:    float = 0.0
    tp_price:       float = 0.0
    sl_price:       float = 0.0
    quantity:       float = 0.0
    resistance:     float = 0.0
    support:        float = 0.0
    last_signal:    str   = ""          # para no repetir misma señal
    trades_today:   int   = 0
    wins:           int   = 0
    losses:         int   = 0
    total_pnl:      float = 0.0
    candle_counter: int   = 0


state = BotState()


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────────────────────

def _pip_size() -> float:
    """Tamaño de 1 pip para el símbolo configurado."""
    sym = config.SYMBOL
    if "BTC" in sym:
        return 1.0
    elif "ETH" in sym:
        return 0.1
    elif "XRP" in sym or "ADA" in sym or "DOGE" in sym:
        return 0.00001
    else:
        return 0.0001   # Forex / otros


def _tp_price(entry: float, direction: str) -> float:
    pips = config.TP_PIPS * _pip_size()
    return entry + pips if direction == "LONG" else entry - pips


def _sl_price(entry: float, direction: str) -> float:
    pips = config.SL_PIPS * _pip_size()
    return entry - pips if direction == "LONG" else entry + pips


def _is_trading_hours() -> bool:
    """Verifica si estamos dentro del horario de trading permitido."""
    hour = datetime.utcnow().hour
    return config.TRADE_START_HOUR <= hour <= config.TRADE_END_HOUR


# ─────────────────────────────────────────────────────────────────────────────
# Verificación de cierre de posición (TP/SL alcanzado)
# ─────────────────────────────────────────────────────────────────────────────

def check_position_closed() -> None:
    """
    Comprueba si la posición se ha cerrado (TP o SL alcanzado).
    BingX cierra la posición automáticamente; aquí detectamos el cierre
    y enviamos la notificación con P&L.
    """
    if not state.in_trade:
        return

    try:
        positions = bx.get_open_positions(config.SYMBOL)
        still_open = any(
            float(p.get("positionAmt", 0)) != 0 for p in positions
        )

        if not still_open:
            # Posición cerrada — calcular P&L aproximado
            ticker     = bx.get_ticker(config.SYMBOL)
            exit_price = float(ticker.get("lastPrice", state.entry_price))

            if state.direction == "LONG":
                pnl = (exit_price - state.entry_price) * state.quantity * config.LEVERAGE
            else:
                pnl = (state.entry_price - exit_price) * state.quantity * config.LEVERAGE

            # Determinar motivo
            if state.direction == "LONG":
                if exit_price >= state.tp_price * 0.999:
                    reason = "✅ Take Profit"
                    state.wins += 1
                else:
                    reason = "🛑 Stop Loss"
                    state.losses += 1
            else:
                if exit_price <= state.tp_price * 1.001:
                    reason = "✅ Take Profit"
                    state.wins += 1
                else:
                    reason = "🛑 Stop Loss"
                    state.losses += 1

            state.total_pnl += pnl

            tg.send_order_closed(
                direction  = state.direction,
                symbol     = config.SYMBOL,
                entry      = state.entry_price,
                exit_price = exit_price,
                pnl        = pnl,
                reason     = reason,
            )

            logger.info(
                "Posición cerrada | %s | Entrada: %.4f | Salida: %.4f | P&L: %.2f USDT | %s",
                state.direction, state.entry_price, exit_price, pnl, reason,
            )

            # Resetear estado
            state.in_trade    = False
            state.direction   = ""
            state.entry_price = 0.0
            state.last_signal = ""

    except Exception as e:
        logger.error("Error comprobando posición: %s", e)
        tg.send_error(f"check_position_closed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Apertura de trade
# ─────────────────────────────────────────────────────────────────────────────

def open_trade(direction: str, current_price: float) -> None:
    """Abre una posición en BingX con TP y SL automáticos."""
    try:
        entry = current_price
        tp    = _tp_price(entry, direction)
        sl    = _sl_price(entry, direction)
        qty   = bx.calculate_quantity(config.CAPITAL_PER_TRADE, entry, config.LEVERAGE)

        side  = "BUY" if direction == "LONG" else "SELL"

        order = bx.place_market_order(
            symbol   = config.SYMBOL,
            side     = side,
            quantity = qty,
            tp_price = round(tp, 4),
            sl_price = round(sl, 4),
        )

        # Guardar estado
        state.in_trade    = True
        state.direction   = direction
        state.entry_price = entry
        state.tp_price    = tp
        state.sl_price    = sl
        state.quantity    = qty
        state.trades_today += 1

        tg.send_order_placed(
            direction = direction,
            symbol    = config.SYMBOL,
            entry     = entry,
            tp        = tp,
            sl        = sl,
            quantity  = qty,
            leverage  = config.LEVERAGE,
            capital   = config.CAPITAL_PER_TRADE,
        )

        logger.info(
            "Trade abierto | %s | Entrada: %.4f | TP: %.4f | SL: %.4f | Qty: %.4f",
            direction, entry, tp, sl, qty,
        )

    except Exception as e:
        logger.error("Error abriendo trade: %s", e)
        tg.send_error(f"open_trade: {e}")
        state.in_trade = False


# ─────────────────────────────────────────────────────────────────────────────
# Ciclo principal de estrategia
# ─────────────────────────────────────────────────────────────────────────────

def run_strategy() -> None:
    """
    Se ejecuta al cierre de cada vela de 15 minutos.

    Flujo:
      1. Verificar si hay posición abierta (¿se cerró por TP/SL?)
      2. Si no hay posición, buscar señal de breakout
      3. Si hay señal, abrir trade
    """
    state.candle_counter += 1
    logger.info("─── Tick #%d ─── %s", state.candle_counter, datetime.utcnow().isoformat())

    # ── 1. Comprobar si la posición activa se cerró ──────────────────────
    check_position_closed()

    if not _is_trading_hours():
        logger.info("Fuera de horario de trading. Saltando.")
        return

    # ── 2. No operar si ya estamos en posición ───────────────────────────
    if state.in_trade:
        logger.info(
            "Posición activa: %s | Entrada: %.4f | TP: %.4f | SL: %.4f",
            state.direction, state.entry_price, state.tp_price, state.sl_price,
        )
        return

    # ── 3. Obtener velas y calcular ZigZag ───────────────────────────────
    try:
        candles = bx.get_klines(
            symbol   = config.SYMBOL,
            interval = config.BINGX_INTERVAL,
            limit    = config.ZZ_LOOKBACK + 10,
        )
    except Exception as e:
        logger.error("Error obteniendo velas: %s", e)
        tg.send_error(f"get_klines: {e}")
        return

    if len(candles) < config.ZZ_LOOKBACK:
        logger.warning("Pocas velas disponibles: %d", len(candles))
        return

    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]

    resistance, support = get_breakout_levels(
        highs     = highs,
        lows      = lows,
        depth     = config.ZZ_DEPTH,
        deviation = config.ZZ_DEVIATION,
        backstep  = config.ZZ_BACKSTEP,
    )

    if resistance is None or support is None:
        logger.warning("ZigZag no encontró niveles suficientes. Esperando más datos.")
        return

    current_close = closes[-1]
    current_price = current_close

    state.resistance = resistance
    state.support    = support

    logger.info(
        "ZigZag | Resistencia: %.4f | Soporte: %.4f | Precio: %.4f",
        resistance, support, current_price,
    )

    # Enviar niveles cada 8 velas (~2 horas) para no saturar Telegram
    if state.candle_counter % 8 == 0:
        tg.send_zigzag_levels(config.SYMBOL, resistance, support, current_price)

    # ── 4. Detectar breakout ─────────────────────────────────────────────
    signal = ""

    if current_close > resistance:
        signal = "LONG"
    elif current_close < support:
        signal = "SHORT"

    if not signal:
        logger.info("Sin señal. Precio dentro del rango ZigZag.")
        return

    # Evitar entrar dos veces seguidas con la misma señal en el mismo nivel
    signal_id = f"{signal}_{resistance:.4f}_{support:.4f}"
    if signal_id == state.last_signal:
        logger.info("Señal duplicada ignorada: %s", signal_id)
        return

    state.last_signal = signal_id

    logger.info("¡SEÑAL DETECTADA! %s | Precio: %.4f", signal, current_price)

    tg.send_signal_detected(
        direction  = signal,
        symbol     = config.SYMBOL,
        price      = current_price,
        resistance = resistance,
        support    = support,
    )

    # ── 5. Configurar leverage y abrir trade ─────────────────────────────
    try:
        bx.set_leverage(config.SYMBOL, config.LEVERAGE)
    except Exception as e:
        logger.warning("No se pudo establecer leverage (puede ya estar configurado): %s", e)

    open_trade(direction=signal, current_price=current_price)


def get_stats_summary() -> str:
    """Resumen de estadísticas del bot para reporte diario."""
    winrate = (state.wins / (state.wins + state.losses) * 100) if (state.wins + state.losses) > 0 else 0
    return (
        f"📊 <b>Estadísticas del bot</b>\n"
        f"Trades hoy:  {state.trades_today}\n"
        f"Wins:        {state.wins}\n"
        f"Losses:      {state.losses}\n"
        f"Win rate:    {winrate:.1f}%\n"
        f"P&L total:   {state.total_pnl:+.2f} USDT\n"
    )
