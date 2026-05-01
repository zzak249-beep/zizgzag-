# -*- coding: utf-8 -*-
"""strategy.py -- Three Step Future-Trend + Double Top/Bottom filter.

PRIMARY SIGNAL:
  delta_vol = close > open ? +volume : -volume
  delta1    = sum(delta_vol, period)
  delta2    = sum(delta_vol, period*2) - delta1
  LONG  when delta1 crosses ABOVE 0  AND delta2 >= 0
  SHORT when delta1 crosses BELOW 0  AND delta2 <= 0

DOUBLE TOP / DOUBLE BOTTOM FILTER (from the @mrk_rsifx article):
  Only take LONG  signals where a Double Bottom is detected (neckline break up)
  Only take SHORT signals where a Double Top  is detected (neckline break down)
  This keeps the bot in range-market conditions with high-probability reversals.

Detection algo:
  - Find two local highs/lows within dt_lookback bars
  - Both peaks/troughs within dt_tolerance * ATR of each other (similar level)
  - Neckline = lowest low between two tops / highest high between two bottoms
  - Confirmation = current price has broken through the neckline
"""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol:    str
    side:      str      # "BUY" | "SELL"
    price:     float
    sl:        float
    tp:        float
    atr:       float
    delta1:    float
    delta2:    float
    delta3:    float
    pattern:   str = ""  # "DOUBLE_BOTTOM" | "DOUBLE_TOP" | "DELTA_ONLY"


@dataclass
class DoublePattern:
    kind:      str    # "DOUBLE_TOP" | "DOUBLE_BOTTOM"
    peak1:     float
    peak2:     float
    neckline:  float
    confirmed: bool


# ── Internal helpers ───────────────────────────────────────────────────────────

def _rolling_sum(arr: np.ndarray, n: int) -> np.ndarray:
    cs  = np.cumsum(arr)
    out = cs.copy()
    out[n:] = cs[n:] - cs[:-n]
    out[:n] = cs[:n]
    return out


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows,
         np.maximum(np.abs(highs - prev_close),
                    np.abs(lows  - prev_close)))
    atr_arr = np.zeros_like(tr)
    atr_arr[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr_arr[i] = (atr_arr[i - 1] * (period - 1) + tr[i]) / period
    return atr_arr


def _local_highs(arr: np.ndarray, window: int = 5) -> list[int]:
    peaks = []
    for i in range(window, len(arr) - window):
        if arr[i] == arr[i - window:i + window + 1].max():
            peaks.append(i)
    return peaks


def _local_lows(arr: np.ndarray, window: int = 5) -> list[int]:
    troughs = []
    for i in range(window, len(arr) - window):
        if arr[i] == arr[i - window:i + window + 1].min():
            troughs.append(i)
    return troughs


# ── Delta computation ──────────────────────────────────────────────────────────

def compute_deltas(
    opens: np.ndarray, closes: np.ndarray, volumes: np.ndarray, period: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    delta_vol = np.where(closes > opens, volumes, -volumes)
    d1 = _rolling_sum(delta_vol, period)
    d2 = _rolling_sum(delta_vol, period * 2) - d1
    d3 = _rolling_sum(delta_vol, period * 3) - d1 - d2
    return d1, d2, d3


# ── Double Top / Bottom detection ──────────────────────────────────────────────

def detect_double_top(
    highs:     np.ndarray,
    lows:      np.ndarray,
    closes:    np.ndarray,
    atr:       float,
    lookback:  int   = 60,
    tolerance: float = 0.5,
    pivot_win: int   = 5,
) -> DoublePattern | None:
    """
    Double Top: two peaks at similar price level, neckline broken downward.
    """
    safe_n = min(lookback + pivot_win, len(highs) - pivot_win - 1)
    h_sl = highs[-(safe_n):-pivot_win]
    l_sl = lows[ -(safe_n):-pivot_win]
    c_price = closes[-2]

    peaks = _local_highs(h_sl, window=pivot_win)
    if len(peaks) < 2:
        return None

    p1_idx, p2_idx = peaks[-2], peaks[-1]
    p1_val = h_sl[p1_idx]
    p2_val = h_sl[p2_idx]

    if abs(p1_val - p2_val) > tolerance * atr:
        return None
    if p2_idx <= p1_idx:
        return None

    neckline = float(l_sl[p1_idx:p2_idx + 1].min())
    confirmed = c_price < neckline

    return DoublePattern(
        kind="DOUBLE_TOP",
        peak1=p1_val, peak2=p2_val,
        neckline=neckline,
        confirmed=confirmed,
    )


def detect_double_bottom(
    highs:     np.ndarray,
    lows:      np.ndarray,
    closes:    np.ndarray,
    atr:       float,
    lookback:  int   = 60,
    tolerance: float = 0.5,
    pivot_win: int   = 5,
) -> DoublePattern | None:
    """
    Double Bottom: two troughs at similar price level, neckline broken upward.
    """
    safe_n = min(lookback + pivot_win, len(lows) - pivot_win - 1)
    h_sl = highs[-(safe_n):-pivot_win]
    l_sl = lows[ -(safe_n):-pivot_win]
    c_price = closes[-2]

    troughs = _local_lows(l_sl, window=pivot_win)
    if len(troughs) < 2:
        return None

    t1_idx, t2_idx = troughs[-2], troughs[-1]
    t1_val = l_sl[t1_idx]
    t2_val = l_sl[t2_idx]

    if abs(t1_val - t2_val) > tolerance * atr:
        return None
    if t2_idx <= t1_idx:
        return None

    neckline = float(h_sl[t1_idx:t2_idx + 1].max())
    confirmed = c_price > neckline

    return DoublePattern(
        kind="DOUBLE_BOTTOM",
        peak1=t1_val, peak2=t2_val,
        neckline=neckline,
        confirmed=confirmed,
    )


# ── Main signal function ───────────────────────────────────────────────────────

def get_signal(
    ohlcv:           dict,
    symbol:          str,
    period:          int   = 25,
    atr_period:      int   = 14,
    atr_mult:        float = 2.0,
    rr:              float = 2.0,
    dt_lookback:     int   = 60,
    dt_tolerance:    float = 0.5,
    dt_pivot_win:    int   = 5,
    require_pattern: bool  = True,
) -> Signal | None:
    """
    Signal = Three Step delta cross + confirmed Double Top/Bottom (when require_pattern=True).
    Set REQUIRE_PATTERN=false in env to use delta signal only.
    """
    opens   = ohlcv["open"]
    highs   = ohlcv["high"]
    lows    = ohlcv["low"]
    closes  = ohlcv["close"]
    volumes = ohlcv["volume"]

    min_bars = period * 3 + atr_period + dt_lookback + 15
    if len(closes) < min_bars:
        return None

    delta1, delta2, _ = compute_deltas(opens, closes, volumes, period)
    atr_arr = _atr(highs, lows, closes, atr_period)

    d1_prev = delta1[-3]
    d1_curr = delta1[-2]
    d2_curr = delta2[-2]
    atr_val = atr_arr[-2]
    price   = closes[-2]

    if atr_val <= 0 or price <= 0:
        return None

    sl_dist = atr_val * atr_mult
    tp_dist = sl_dist * rr

    long_delta  = (d1_prev <= 0 < d1_curr) and (d2_curr >= 0)
    short_delta = (d1_prev >= 0 > d1_curr) and (d2_curr <= 0)

    if not long_delta and not short_delta:
        return None

    # ── Double pattern filter ─────────────────────────────────────────────
    pattern_name = "DELTA_ONLY"

    if require_pattern:
        if long_delta:
            db = detect_double_bottom(
                highs, lows, closes, atr_val,
                lookback=dt_lookback, tolerance=dt_tolerance, pivot_win=dt_pivot_win,
            )
            if db is None or not db.confirmed:
                return None
            pattern_name = "DOUBLE_BOTTOM"

        else:  # short_delta
            dt = detect_double_top(
                highs, lows, closes, atr_val,
                lookback=dt_lookback, tolerance=dt_tolerance, pivot_win=dt_pivot_win,
            )
            if dt is None or not dt.confirmed:
                return None
            pattern_name = "DOUBLE_TOP"

    # ── Build signal ──────────────────────────────────────────────────────
    if long_delta:
        return Signal(
            symbol=symbol, side="BUY", price=price,
            sl=round(price - sl_dist, 8),
            tp=round(price + tp_dist, 8),
            atr=atr_val, delta1=d1_curr, delta2=d2_curr, delta3=0,
            pattern=pattern_name,
        )
    else:
        return Signal(
            symbol=symbol, side="SELL", price=price,
            sl=round(price + sl_dist, 8),
            tp=round(price - tp_dist, 8),
            atr=atr_val, delta1=d1_curr, delta2=d2_curr, delta3=0,
            pattern=pattern_name,
        )


# ── Trailing exit helper ───────────────────────────────────────────────────────

def delta1_flipped(ohlcv: dict, period: int, trade_side: str) -> bool:
    """Return True when delta1 flips against the open trade (trailing exit signal)."""
    opens   = ohlcv["open"]
    closes  = ohlcv["close"]
    volumes = ohlcv["volume"]
    if len(closes) < period * 3 + 5:
        return False
    delta1, _, _ = compute_deltas(opens, closes, volumes, period)
    curr = delta1[-2]
    if trade_side == "BUY"  and curr < 0:
        return True
    if trade_side == "SELL" and curr > 0:
        return True
    return False
