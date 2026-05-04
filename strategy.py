# -*- coding: utf-8 -*-
"""strategy.py -- Phantom Edge Bot v6 — ZigZag Institutional Elite V6.
Matches Pine Script EXACTLY:
  LONG:  ta.crossover(close, peak)  + volume > vol_ma*vol_mult + close > open
  SHORT: ta.crossunder(close, valley) + volume > vol_ma*vol_mult + close < open
  SL:    valley (long) / peak (short)  ← pivot level, NOT ATR
  TP:    close + (close-sl)*rr
"""
from __future__ import annotations
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────
# DATACLASS
# ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol:    str
    side:      str
    price:     float
    sl:        float
    tp:        float
    atr_5m:    float
    peak:      float
    valley:    float
    score:     int
    vol_ratio: float
    reasons:   list = field(default_factory=list)
    # compat aliases
    atr:       float = 0.0
    zz_high:   float = 0.0
    zz_low:    float = 0.0
    hma_val:   float = 0.0
    ft_val:    float = 0.0
    zz_trend:  str   = "FLAT"
    st_bull_15m: bool = True
    delta1:    float = 0.0
    delta2:    float = 0.0

    def __post_init__(self):
        self.atr     = self.atr_5m
        self.zz_high = self.peak
        self.zz_low  = self.valley
        self.delta1  = self.peak
        self.delta2  = self.valley


# ─────────────────────────────────────────────────────────────
# CACHE (evita recomputar si misma vela)
# ─────────────────────────────────────────────────────────────
_sig_cache: dict[str, tuple[float, Optional[Signal]]] = {}


# ─────────────────────────────────────────────────────────────
# INDICADORES VECTORIZADOS
# ─────────────────────────────────────────────────────────────

def _f(a) -> np.ndarray:
    return np.nan_to_num(np.asarray(a, dtype=np.float64), nan=0., posinf=0., neginf=0.)


def _pivot_series_v(arr: np.ndarray, n: int, is_high: bool) -> np.ndarray:
    """
    ta.pivothigh / ta.pivotlow + forward-fill (Pine 'var float peak = na').
    Vectorized via sliding_window_view — O(n) instead of O(n×2p).
    """
    L = len(arr)
    if L < 2 * n + 1:
        return np.full(L, np.nan)

    wins = sliding_window_view(arr, 2 * n + 1)
    ctr  = arr[n : L - n]
    ref  = wins.max(axis=1) if is_high else wins.min(axis=1)

    confirmed        = np.where(ctr == ref, ctr, np.nan)
    result           = np.full(L, np.nan)
    result[n : L-n]  = confirmed

    # Forward-fill (Pine: var float peak = na / if not na(ph): peak := ph)
    cur = np.nan
    for i in range(L):
        if not np.isnan(result[i]):
            cur = result[i]
        result[i] = cur
    return result


def _atr_v(h, l, c, p: int = 14) -> float:
    h, l, c = _f(h), _f(l), _f(c)
    prev     = np.roll(c, 1); prev[0] = c[0]
    tr       = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    if len(tr) < p:
        return float(np.mean(h - l)) + 1e-12
    atr = tr[:p].mean()
    for x in tr[p:]:
        atr = (atr * (p - 1) + x) / p
    return max(float(atr), 1e-12)


def _in_dead_session() -> bool:
    """Pine Script doesn't filter sessions — always False to match behavior."""
    return False


# ─────────────────────────────────────────────────────────────
# SEÑAL PRINCIPAL  (Pine Script exact match)
# ─────────────────────────────────────────────────────────────

def get_signal(
    ohlcv_5m:     dict,
    ohlcv_15m:    dict | None,
    ohlcv_1h:     dict | None,
    symbol:       str,
    open_syms:    set   = None,
    pivot_len:    int   = 5,
    atr_period:   int   = 14,
    atr_mult:     float = 2.0,   # kept for R-distance calc in pos_manager
    rr:           float = 2.0,   # Pine: tp_mult = 2.0
    min_vol_mult: float = 1.5,   # Pine: vol_mult = 1.5
    hma_len:      int   = 50,    # unused (kept for compat)
    ft_period:    int   = 25,    # unused (kept for compat)
    min_atr_pct:  float = 0.0,   # optional pre-filter
    min_score:    int   = 2,
    # compat kwargs (ignored)
    **kwargs,
) -> tuple[Signal | None, str]:

    if open_syms is None:
        open_syms = set()
    if not ohlcv_5m:
        return None, "no_data"

    c5 = ohlcv_5m.get("close")
    h5 = ohlcv_5m.get("high")
    l5 = ohlcv_5m.get("low")
    o5 = ohlcv_5m.get("open")
    v5 = ohlcv_5m.get("volume")

    if c5 is None or len(c5) < pivot_len * 4 + 5:
        return None, f"bars_{0 if c5 is None else len(c5)}"

    # ── Cache: skip if same candle ────────────────────────────
    last_ts = hash((float(c5[-1]), float(c5[-2]), float(c5[-3])))
    cached  = _sig_cache.get(symbol)
    if cached and cached[0] == last_ts:
        sig = cached[1]
        return (sig, "ok") if sig else (None, "cached_none")

    price = float(c5[-1])
    if price <= 0:
        return None, "precio_cero"

    # ── ATR (for R-distance in pos_manager only) ──────────────
    atr_val = _atr_v(h5, l5, c5, atr_period)

    # ── Pine: vol_ma = ta.sma(volume, 20) ────────────────────
    # Pine checks current bar volume (v5[-1] = closed or forming candle)
    # We use the second-to-last as reference SMA so current bar can spike
    if len(v5) < 22:
        return None, "vol_insuf"
    vol_ma   = float(np.mean(v5[-21:-1]))   # SMA of last 20 closed bars
    vol_last = float(v5[-2])                # last CLOSED bar (not forming)
    if vol_ma <= 0:
        return None, "vol_ma_cero"
    vol_ratio = vol_last / vol_ma

    # Pine: institucional_vol = volume > (vol_ma * vol_mult)
    institucional_vol = vol_ratio >= min_vol_mult

    # ── ATR pct pre-filter (optional, avoid flat pairs) ───────
    if min_atr_pct > 0 and atr_val / price * 100 < min_atr_pct:
        _sig_cache[symbol] = (last_ts, None)
        return None, "plano"

    # ── ZigZag: pivot series (forward-filled) ────────────────
    pk_ser = _pivot_series_v(_f(h5), pivot_len, is_high=True)
    vl_ser = _pivot_series_v(_f(l5), pivot_len, is_high=False)

    # Pine: ta.crossover(close, peak)  → prev <= peak AND curr > peak
    # We use -2/-3 because -1 is the forming candle, -2 is last closed
    # The "current" candle we evaluate is the LAST CLOSED bar (index -2)
    curr_c   = float(c5[-2])
    prev_c   = float(c5[-3])
    curr_o   = float(o5[-2])
    curr_pk  = pk_ser[-2] if not np.isnan(pk_ser[-2]) else pk_ser[-1]
    curr_vl  = vl_ser[-2] if not np.isnan(vl_ser[-2]) else vl_ser[-1]
    prev_pk  = pk_ser[-3] if not np.isnan(pk_ser[-3]) else curr_pk
    prev_vl  = vl_ser[-3] if not np.isnan(vl_ser[-3]) else curr_vl

    if np.isnan(curr_pk) or np.isnan(curr_vl):
        _sig_cache[symbol] = (last_ts, None)
        return None, "no_pivot"

    # Pine crossover/crossunder
    long_cross  = (prev_c <= prev_pk) and (curr_c > curr_pk)
    short_cross = (prev_c >= prev_vl) and (curr_c < curr_vl)

    if not long_cross and not short_cross:
        _sig_cache[symbol] = (last_ts, None)
        return None, f"sin_cruce pk={curr_pk:.5g} vl={curr_vl:.5g} c={curr_c:.5g}"

    # Pine: close > open (bull candle) / close < open (bear candle)
    bull_candle = curr_c > curr_o
    bear_candle = curr_c < curr_o

    # ── BUILD SIGNAL ──────────────────────────────────────────
    reasons = []
    score   = 0

    # ── LONG ──────────────────────────────────────────────────
    if long_cross and institucional_vol and bull_candle:
        sl = float(curr_vl)           # Pine: sl = valley
        tp = curr_c + (curr_c - sl) * rr  # Pine: tp = close + (close-sl)*tp_mult

        if sl <= 0 or sl >= curr_c:
            _sig_cache[symbol] = (last_ts, None)
            return None, f"sl_invalido sl={sl:.6f} c={curr_c:.6f}"

        score   = 2
        reasons = [
            f"CROSS↑{curr_pk:.5g}",
            f"VOL{vol_ratio:.1f}x",
            "BULL🕯️",
        ]

        # Optional: 15m confirmation
        if ohlcv_15m:
            c15 = ohlcv_15m.get("close")
            h15 = ohlcv_15m.get("high")
            if c15 is not None and len(c15) > pivot_len * 4 + 5:
                pk15 = _pivot_series_v(_f(h15), pivot_len, is_high=True)
                if not np.isnan(pk15[-2]) and float(c15[-2]) > float(pk15[-2]):
                    score += 1
                    reasons.append("15M✓")

        if score < min_score:
            _sig_cache[symbol] = (last_ts, None)
            return None, f"score={score}"

        sig = Signal(
            symbol=symbol, side="BUY",
            price=curr_c, sl=round(sl, 8), tp=round(tp, 8),
            atr_5m=atr_val, peak=float(curr_pk), valley=float(curr_vl),
            score=score, vol_ratio=round(vol_ratio, 2),
            reasons=reasons, st_bull_15m=True,
        )
        _sig_cache[symbol] = (last_ts, sig)
        return sig, "ok"

    # ── SHORT ─────────────────────────────────────────────────
    if short_cross and institucional_vol and bear_candle:
        sl = float(curr_pk)           # Pine: sl = peak
        tp = curr_c - (sl - curr_c) * rr  # Pine: tp = close - (sl-close)*tp_mult

        if sl <= 0 or sl <= curr_c:
            _sig_cache[symbol] = (last_ts, None)
            return None, f"sl_invalido sl={sl:.6f} c={curr_c:.6f}"

        score   = 2
        reasons = [
            f"CROSS↓{curr_vl:.5g}",
            f"VOL{vol_ratio:.1f}x",
            "BEAR🕯️",
        ]

        if ohlcv_15m:
            c15 = ohlcv_15m.get("close")
            l15 = ohlcv_15m.get("low")
            if c15 is not None and len(c15) > pivot_len * 4 + 5:
                vl15 = _pivot_series_v(_f(l15), pivot_len, is_high=False)
                if not np.isnan(vl15[-2]) and float(c15[-2]) < float(vl15[-2]):
                    score += 1
                    reasons.append("15M✓")

        if score < min_score:
            _sig_cache[symbol] = (last_ts, None)
            return None, f"score={score}"

        sig = Signal(
            symbol=symbol, side="SELL",
            price=curr_c, sl=round(sl, 8), tp=round(tp, 8),
            atr_5m=atr_val, peak=float(curr_pk), valley=float(curr_vl),
            score=score, vol_ratio=round(vol_ratio, 2),
            reasons=reasons, st_bull_15m=False,
        )
        _sig_cache[symbol] = (last_ts, sig)
        return sig, "ok"

    # ── Rejection reason (crossover present but other conditions failed) ──
    if long_cross:
        if not institucional_vol:
            r = f"vol_bajo_{vol_ratio:.2f}x"
        else:
            r = "no_bull_candle"
    elif short_cross:
        if not institucional_vol:
            r = f"vol_bajo_{vol_ratio:.2f}x"
        else:
            r = "no_bear_candle"
    else:
        r = "sin_cruce"

    _sig_cache[symbol] = (last_ts, None)
    return None, r


def clear_signal_cache(symbol: str) -> None:
    _sig_cache.pop(symbol, None)


# ─────────────────────────────────────────────────────────────
# EXIT LOGIC — pivot break (matches Pine trail exit)
# ─────────────────────────────────────────────────────────────

def check_trail_exit(
    ohlcv_5m:   dict,
    ohlcv_15m:  dict | None,
    trade_side: str,
    pivot_len:  int   = 5,
    hma_len:    int   = 50,   # unused, kept for compat
    ft_period:  int   = 25,   # unused, kept for compat
    peak_r:     float = 0.,
    **kwargs,
) -> str | None:
    """
    Exit when price crosses back through the opposing pivot level.
    Mirrors Pine: strategy.exit(..., trail_price=..., trail_offset=...)
    Only activates after peak_r >= 1.0 (breakeven territory).
    """
    if not ohlcv_5m or peak_r < 1.0:
        return None

    c5 = ohlcv_5m.get("close")
    h5 = ohlcv_5m.get("high")
    l5 = ohlcv_5m.get("low")

    if c5 is None or len(c5) < pivot_len * 4 + 5:
        return None

    curr_c = float(c5[-2])
    prev_c = float(c5[-3])

    if trade_side == "BUY":
        # Exit if price breaks back below valley
        vl = _pivot_series_v(_f(l5), pivot_len, is_high=False)
        vl_now = vl[-2] if not np.isnan(vl[-2]) else float('nan')
        if not np.isnan(vl_now) and prev_c > vl_now and curr_c < vl_now:
            return "TRAIL_VALLEY"
    else:
        # Exit if price breaks back above peak
        pk = _pivot_series_v(_f(h5), pivot_len, is_high=True)
        pk_now = pk[-2] if not np.isnan(pk[-2]) else float('nan')
        if not np.isnan(pk_now) and prev_c < pk_now and curr_c > pk_now:
            return "TRAIL_PEAK"

    return None
