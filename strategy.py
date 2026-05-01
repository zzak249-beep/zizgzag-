"""
Estrategia Maki v2 — ZigZag + 20MA 4H
TP dinámico basado en ATR | SL dinámico basado en ATR
Apalancamiento: 10x
"""
from typing import Optional

# ── Parámetros de estrategia ──────────────────────────────────────────────────
PIVOT_BARS   = 5      # velas a cada lado para confirmar pivot
ATR_PERIOD   = 14     # período ATR para TP/SL dinámicos
ATR_TP_MULT  = 1.5    # TP = entrada ± ATR * multiplicador
ATR_SL_MULT  = 1.0    # SL = entrada ∓ ATR * multiplicador
MIN_ATR_PCT  = 0.003  # ATR mínimo respecto al precio (0.3%) — filtra pares sin movimiento
MAX_ATR_PCT  = 0.04   # ATR máximo (4%) — filtra pares demasiado volátiles

# Fallback fijo si el ATR no está disponible
TP_PCT_FIXED = 0.0045
SL_PCT_FIXED = 0.0030


# ── Indicadores ──────────────────────────────────────────────────────────────

def _sma(values: list[float], period: int) -> list[Optional[float]]:
    result: list[Optional[float]] = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i - period + 1: i + 1]) / period)
    return result


def _atr(candles: list[dict], period: int = ATR_PERIOD) -> Optional[float]:
    """Average True Range — mide volatilidad real del mercado."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h  = candles[i]["h"]
        l  = candles[i]["l"]
        pc = candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def _last_pivot_high(highs: list[float]) -> Optional[float]:
    """Último pivot high confirmado (excluye la vela en formación)."""
    n = len(highs)
    for i in range(n - PIVOT_BARS - 2, PIVOT_BARS - 1, -1):
        h = highs[i]
        if (
            all(h > highs[i - j] for j in range(1, PIVOT_BARS + 1))
            and all(h > highs[i + j] for j in range(1, PIVOT_BARS + 1))
        ):
            return h
    return None


def _last_pivot_low(lows: list[float]) -> Optional[float]:
    """Último pivot low confirmado."""
    n = len(lows)
    for i in range(n - PIVOT_BARS - 2, PIVOT_BARS - 1, -1):
        lo = lows[i]
        if (
            all(lo < lows[i - j] for j in range(1, PIVOT_BARS + 1))
            and all(lo < lows[i + j] for j in range(1, PIVOT_BARS + 1))
        ):
            return lo
    return None


# ── Señal principal ──────────────────────────────────────────────────────────

def signal(candles_15m: list[dict], candles_4h: list[dict]) -> Optional[str]:
    """
    Retorna "LONG", "SHORT" o None.
    Condiciones LONG:
      1. MA20 4H con pendiente alcista
      2. Precio sobre MA20 4H
      3. Ruptura alcista de pivot high en 15m
      4. Extensión de ruptura < 0.5% (no entrar tarde)
      5. ATR dentro de rango de volatilidad aceptable
    Condiciones SHORT: simétricas.
    """
    if len(candles_15m) < PIVOT_BARS * 2 + 5 or len(candles_4h) < 22:
        return None

    # ── 1. MA20 4H ────────────────────────────────────────────────────────────
    closes_4h = [c["c"] for c in candles_4h]
    ma20_vals = _sma(closes_4h, 20)
    valid_ma  = [v for v in ma20_vals if v is not None]
    if len(valid_ma) < 3:
        return None

    slope     = valid_ma[-1] - valid_ma[-3]
    threshold = valid_ma[-1] * 0.0001  # pendiente mínima del 0.01%

    ma_up   = slope >  threshold
    ma_down = slope < -threshold
    if not ma_up and not ma_down:
        return None  # MA plana

    last_4h_close = closes_4h[-1]
    above_ma = last_4h_close > valid_ma[-1]
    below_ma = last_4h_close < valid_ma[-1]

    # ── 2. ATR 15m — filtro de volatilidad ───────────────────────────────────
    atr   = _atr(candles_15m)
    price = candles_15m[-2]["c"]
    if atr is not None:
        atr_pct = atr / price
        if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
            return None  # demasiado quieto o demasiado volátil

    # ── 3. Pivotes en 15m ─────────────────────────────────────────────────────
    highs  = [c["h"] for c in candles_15m]
    lows   = [c["l"] for c in candles_15m]
    peak   = _last_pivot_high(highs)
    valley = _last_pivot_low(lows)
    if peak is None or valley is None:
        return None

    prev_c = candles_15m[-2]["c"]
    last_c = candles_15m[-1]["c"]

    # ── LONG ──────────────────────────────────────────────────────────────────
    if prev_c <= peak < last_c and ma_up and above_ma:
        ext = (last_c - peak) / peak
        if ext < 0.005:
            return "LONG"

    # ── SHORT ─────────────────────────────────────────────────────────────────
    if prev_c >= valley > last_c and ma_down and below_ma:
        ext = (valley - last_c) / valley
        if ext < 0.005:
            return "SHORT"

    return None


# ── TP / SL dinámicos basados en ATR ────────────────────────────────────────

def tp_sl(entry: float, side: str, candles_15m: list[dict] | None = None) -> tuple[float, float]:
    """
    Calcula TP y SL.
    Si se pasan las velas usa ATR dinámico; si no, usa porcentajes fijos.
    """
    atr = _atr(candles_15m) if candles_15m else None

    if atr:
        if side == "LONG":
            tp = entry + atr * ATR_TP_MULT
            sl = entry - atr * ATR_SL_MULT
        else:
            tp = entry - atr * ATR_TP_MULT
            sl = entry + atr * ATR_SL_MULT
    else:
        if side == "LONG":
            tp = entry * (1 + TP_PCT_FIXED)
            sl = entry * (1 - SL_PCT_FIXED)
        else:
            tp = entry * (1 - TP_PCT_FIXED)
            sl = entry * (1 + SL_PCT_FIXED)

    return tp, sl


def risk_reward(tp: float, sl: float, entry: float, side: str) -> float:
    """Calcula ratio reward/risk."""
    if side == "LONG":
        reward = tp - entry
        risk   = entry - sl
    else:
        reward = entry - tp
        risk   = sl - entry
    return reward / risk if risk > 0 else 0.0
