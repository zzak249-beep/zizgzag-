"""
zigzag.py — Indicador ZigZag estilo MT4 / ZigZag++ de TradingView.
Detecta picos y valles, devuelve resistencia y soporte para breakout.
"""
from __future__ import annotations
import numpy as np
from typing import List, Optional, Tuple


def get_breakout_levels(
    highs: List[float],
    lows:  List[float],
    depth: int = 12,
    deviation: int = 5,
    backstep:  int = 2,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Retorna (resistencia, soporte) = último pico y último valle del ZigZag.
    Resistencia: precio donde entra LONG si se rompe hacia arriba.
    Soporte:     precio donde entra SHORT si se rompe hacia abajo.
    """
    h = np.array(highs, dtype=float)
    l = np.array(lows,  dtype=float)
    n = len(h)
    if n < depth * 2 + backstep:
        return None, None

    min_dev = deviation / 10.0  # 5 → 0.5%
    pivots  = []
    last_type  = None
    last_price = 0.0
    last_idx   = -999

    for i in range(depth, n):
        start = max(0, i - depth)
        is_hi = float(h[i]) >= float(np.max(h[start:i + 1]))
        is_lo = float(l[i]) <= float(np.min(l[start:i + 1]))

        for kind, price in [("high", float(h[i])), ("low", float(l[i]))]:
            if kind == "high" and not is_hi:
                continue
            if kind == "low" and not is_lo:
                continue
            if abs(i - last_idx) < backstep:
                continue
            if last_price > 0:
                dev = abs(price - last_price) / last_price * 100
                if dev < min_dev:
                    # actualizar si es más extremo del mismo tipo
                    if pivots and kind == last_type:
                        if (kind == "high" and price > pivots[-1][1]) or \
                           (kind == "low"  and price < pivots[-1][1]):
                            pivots[-1] = (kind, price)
                            last_idx   = i
                            last_price = price
                    continue
            if kind == last_type:
                if pivots:
                    if (kind == "high" and price > pivots[-1][1]) or \
                       (kind == "low"  and price < pivots[-1][1]):
                        pivots[-1] = (kind, price)
                        last_idx   = i
                        last_price = price
                continue
            pivots.append((kind, price))
            last_type  = kind
            last_idx   = i
            last_price = price

    resistance = next((p for k, p in reversed(pivots) if k == "high"), None)
    support    = next((p for k, p in reversed(pivots) if k == "low"),  None)
    return resistance, support
