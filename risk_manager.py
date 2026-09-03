"""
risk_manager.py — Tamaño de posición (genérico, reutilizado del bot
wavelet). El SL/TP específico de esta estrategia vive en
sweep_engine.compute_sweep_sl_tp, porque depende del nivel barrido,
no de un ATR simétrico como en wavelet.
"""

import math
from dataclasses import dataclass


@dataclass
class SizingResult:
    quantity: float
    notional: float
    ok: bool
    reason: str = ""


def round_step(value: float, precision: int) -> float:
    factor = 10 ** precision
    return math.floor(value * factor) / factor


def compute_position_size(equity: float, qty_pct: float, price: float,
                           quantity_precision: int, trade_min_quantity: float,
                           trade_min_usdt: float) -> SizingResult:
    if price <= 0 or equity <= 0:
        return SizingResult(0.0, 0.0, False, "precio o equity inválidos")

    raw_qty = (equity * (qty_pct / 100.0)) / price
    qty = round_step(raw_qty, quantity_precision)
    notional = qty * price

    if qty <= 0:
        return SizingResult(0.0, 0.0, False, "cantidad redondeada a 0")
    if trade_min_quantity and qty < trade_min_quantity:
        return SizingResult(qty, notional, False, f"por debajo de tradeMinQuantity ({trade_min_quantity})")
    if trade_min_usdt and notional < trade_min_usdt:
        return SizingResult(qty, notional, False, f"nocional por debajo de tradeMinUSDT ({trade_min_usdt})")

    return SizingResult(qty, notional, True)
