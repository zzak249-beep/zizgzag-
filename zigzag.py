"""
zigzag.py — Implementación del indicador ZigZag al estilo MT4 / ZigZag++ de TradingView.

Lógica:
  1. Detectar máximos y mínimos locales usando ventana `depth`
  2. Alternar entre pivotes HIGH y LOW (no dos HIGH seguidos)
  3. El pico más reciente = resistencia (nivel de entrada LONG)
  4. El valle más reciente = soporte   (nivel de entrada SHORT)
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class Pivot:
    idx:   int
    price: float
    kind:  str   # 'high' | 'low'
    label: str   # 'HH' | 'LH' | 'HL' | 'LL'


def _is_local_high(highs: np.ndarray, i: int, depth: int) -> bool:
    """True si highs[i] es máximo en la ventana [i-depth .. i]."""
    start = max(0, i - depth)
    return float(highs[i]) >= float(np.max(highs[start:i + 1]))


def _is_local_low(lows: np.ndarray, i: int, depth: int) -> bool:
    """True si lows[i] es mínimo en la ventana [i-depth .. i]."""
    start = max(0, i - depth)
    return float(lows[i]) <= float(np.min(lows[start:i + 1]))


def calculate_zigzag(
    highs:     List[float],
    lows:      List[float],
    depth:     int = 12,
    deviation: int = 5,
    backstep:  int = 2,
) -> List[Pivot]:
    """
    Calcula los pivotes ZigZag sobre los arrays de high/low.

    Args:
        highs     : lista de precios máximos por vela
        lows      : lista de precios mínimos por vela
        depth     : ventana mínima de búsqueda de pivote
        deviation : desviación mínima (%) × 10 entre pivotes
        backstep  : mínimo de barras entre pivotes del mismo tipo

    Returns:
        Lista de Pivot ordenada por índice (el último = más reciente).
    """
    h = np.array(highs, dtype=float)
    l = np.array(lows,  dtype=float)
    n = len(h)

    if n < depth + backstep + 2:
        return []

    # ------------------------------------------------------------------
    # Paso 1: detectar candidatos a pivote (sin alternar aún)
    # ------------------------------------------------------------------
    raw_highs: list[tuple[int, float]] = []
    raw_lows:  list[tuple[int, float]] = []

    for i in range(depth, n):
        if _is_local_high(h, i, depth):
            raw_highs.append((i, float(h[i])))
        if _is_local_low(l, i, depth):
            raw_lows.append((i, float(l[i])))

    # ------------------------------------------------------------------
    # Paso 2: mezclar y filtrar alternando HIGH / LOW
    # ------------------------------------------------------------------
    # Combina ambas listas y ordena por índice
    all_candidates = (
        [(idx, price, "high") for idx, price in raw_highs] +
        [(idx, price, "low")  for idx, price in raw_lows]
    )
    all_candidates.sort(key=lambda x: x[0])

    pivots: List[Tuple[int, float, str]] = []
    last_type: Optional[str] = None
    last_idx:  int           = -999
    last_price: float        = 0.0

    for idx, price, kind in all_candidates:
        # Respetar backstep
        if abs(idx - last_idx) < backstep:
            continue

        # Calcular desviación respecto al último pivote
        if last_price > 0:
            dev = abs(price - last_price) / last_price * 100.0
            min_dev = deviation / 10.0   # deviation=5 → 0.5 %
            if dev < min_dev:
                # No suficiente movimiento; si es del mismo tipo, actualizar
                if kind == last_type:
                    if pivots:
                        if (kind == "high" and price > pivots[-1][1]) or \
                           (kind == "low"  and price < pivots[-1][1]):
                            pivots[-1] = (idx, price, kind)
                            last_idx   = idx
                            last_price = price
                continue

        if kind == last_type:
            # Mismo tipo consecutivo: mantener el más extremo
            if pivots:
                if (kind == "high" and price > pivots[-1][1]) or \
                   (kind == "low"  and price < pivots[-1][1]):
                    pivots[-1] = (idx, price, kind)
                    last_idx   = idx
                    last_price = price
            continue

        pivots.append((idx, price, kind))
        last_type  = kind
        last_idx   = idx
        last_price = price

    # ------------------------------------------------------------------
    # Paso 3: etiquetar HH / LH / HL / LL
    # ------------------------------------------------------------------
    result: List[Pivot] = []
    last_high_price: Optional[float] = None
    last_low_price:  Optional[float] = None

    for idx, price, kind in pivots:
        if kind == "high":
            if last_high_price is None:
                label = "HH"
            else:
                label = "HH" if price > last_high_price else "LH"
            last_high_price = price
        else:
            if last_low_price is None:
                label = "HL"
            else:
                label = "HL" if price > last_low_price else "LL"
            last_low_price = price

        result.append(Pivot(idx=idx, price=price, kind=kind, label=label))

    return result


def get_breakout_levels(
    highs:     List[float],
    lows:      List[float],
    depth:     int = 12,
    deviation: int = 5,
    backstep:  int = 2,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Devuelve (resistencia, soporte) basados en el último pico/valle del ZigZag.

    - resistencia = precio del último pivote HIGH confirmado
    - soporte     = precio del último pivote LOW confirmado

    Estas son las líneas horizontales que dibuja la estrategia de Maki.
    """
    pivots = calculate_zigzag(highs, lows, depth, deviation, backstep)
    if not pivots:
        return None, None

    resistance: Optional[float] = None
    support:    Optional[float] = None

    for p in reversed(pivots):
        if p.kind == "high" and resistance is None:
            resistance = p.price
        if p.kind == "low" and support is None:
            support = p.price
        if resistance is not None and support is not None:
            break

    return resistance, support
