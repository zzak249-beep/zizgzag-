# -*- coding: utf-8 -*-
"""strategy.py -- Phantom Edge Bot v6: ZigZag + HMA + FutureTrend.

Traducción EXACTA del Pine Script al motor Python:

PINE ORIGINAL:
  LONG:  crossover(close, peak)   + HMA alcista + VolDelta > 0
  SHORT: crossunder(close, valley) + HMA bajista + VolDelta < 0

SCORE 0-6:
  +2  Ruptura ZigZag (crossover/crossunder de peak/valley)
  +1  HMA dirección correcta (slope + precio respecto a HMA)
  +1  FutureTrend (VolDelta 3 periodos) a favor
  +1  Confirmación 15m (HMA + FutureTrend en 15m alineados)
  +1  Volumen actual > media 20 periodos × 1.2

SALIDA:
  - HMA flip en 5m (más rápido)
  - FutureTrend flip (volumen se vuelve contra la posición)
  - Pivot break contrario en 5m
"""
from __future__ import annotations
import math
import datetime
import numpy as np
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────
# DATACLASS SEÑAL
# ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol:      str
    side:        str        # "BUY" | "SELL"
    price:       float
    sl:          float
    tp:          float
    atr_5m:      float
    peak:        float      # último swing high
    valley:      float      # último swing low
    hma_val:     float      # HMA actual
    ft_val:      float      # FutureTrend valor
    score:       int        # 0-6
    vol_ratio:   float
    reasons:     list       # debug
    # compat aliases
    atr:         float = 0.0
    zz_high:     float = 0.0
    zz_low:      float = 0.0
    zz_trend:    str   = "FLAT"
    st_bull_15m: bool  = True
    delta1:      float = 0.0
    delta2:      float = 0.0

    def __post_init__(self):
        self.atr     = self.atr_5m
        self.zz_high = self.peak
        self.zz_low  = self.valley
        self.delta1  = self.peak
        self.delta2  = self.valley


# ─────────────────────────────────────────────────────────────
# INDICADORES — Traducción exacta Pine → Python
# ─────────────────────────────────────────────────────────────

def _f(a) -> np.ndarray:
    return np.nan_to_num(np.asarray(a, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)


# ── ZigZag: ta.pivothigh / ta.pivotlow ───────────────────────
def _pivot_highs(h: np.ndarray, n: int) -> np.ndarray:
    """ta.pivothigh(high, n, n) — confirma pivot cuando hay n velas a cada lado."""
    h   = _f(h)
    out = np.full(len(h), np.nan)
    for i in range(n, len(h) - n):
        if h[i] == h[i-n:i+n+1].max():
            out[i] = h[i]
    return out


def _pivot_lows(l: np.ndarray, n: int) -> np.ndarray:
    """ta.pivotlow(low, n, n)"""
    l   = _f(l)
    out = np.full(len(l), np.nan)
    for i in range(n, len(l) - n):
        if l[i] == l[i-n:i+n+1].min():
            out[i] = l[i]
    return out


def _peak_series(h: np.ndarray, n: int) -> np.ndarray:
    """
    var float peak = na
    if not na(ph): peak := ph
    → Forward-fill del último pivot confirmado.
    """
    ph  = _pivot_highs(h, n)
    out = np.full(len(h), np.nan)
    cur = np.nan
    for i in range(len(h)):
        if not np.isnan(ph[i]):
            cur = ph[i]
        out[i] = cur
    return out


def _valley_series(l: np.ndarray, n: int) -> np.ndarray:
    """var float valley = na → forward-fill."""
    pl  = _pivot_lows(l, n)
    out = np.full(len(l), np.nan)
    cur = np.nan
    for i in range(len(l)):
        if not np.isnan(pl[i]):
            cur = pl[i]
        out[i] = cur
    return out


def _crossover(series: np.ndarray, level: np.ndarray) -> bool:
    """ta.crossover(close, peak): prev <= level AND curr > level."""
    if len(series) < 2 or len(level) < 2: return False
    if np.isnan(level[-1]) or np.isnan(level[-2]): return False
    return bool(series[-2] <= level[-2] and series[-1] > level[-1])


def _crossunder(series: np.ndarray, level: np.ndarray) -> bool:
    """ta.crossunder(close, valley): prev >= level AND curr < level."""
    if len(series) < 2 or len(level) < 2: return False
    if np.isnan(level[-1]) or np.isnan(level[-2]): return False
    return bool(series[-2] >= level[-2] and series[-1] < level[-1])


# ── HMA: ta.hma(close, len) ──────────────────────────────────
def _wma(arr: np.ndarray, period: int) -> np.ndarray:
    """WMA ponderado lineal."""
    if len(arr) < period:
        return np.full(len(arr), arr[-1] if len(arr) > 0 else 0.0)
    w   = np.arange(1, period + 1, dtype=np.float64)
    ws  = w.sum()
    out = np.zeros(len(arr))
    for i in range(period - 1, len(arr)):
        out[i] = np.dot(arr[i-period+1:i+1], w) / ws
    return out


def _hma(c: np.ndarray, n: int) -> np.ndarray:
    """
    HMA = WMA(2×WMA(n/2) − WMA(n), sqrt(n))
    Pine: ta.hma(close, len)
    """
    c     = _f(c)
    if len(c) < n: return np.full(len(c), float(c[-1]) if len(c) else 0.0)
    half  = max(1, n // 2)
    sqrtn = max(1, int(math.sqrt(n)))
    raw   = 2.0 * _wma(c, half) - _wma(c, n)
    return _wma(raw, sqrtn)


def _hma_direction(hma_arr: np.ndarray, close_arr: np.ndarray) -> tuple[bool, bool]:
    """
    hma_alcista = close > hma AND hma > hma[1]
    hma_bajista = close < hma AND hma < hma[1]
    """
    if len(hma_arr) < 2: return False, False
    bull = bool(close_arr[-1] > hma_arr[-1] and hma_arr[-1] > hma_arr[-2])
    bear = bool(close_arr[-1] < hma_arr[-1] and hma_arr[-1] < hma_arr[-2])
    return bull, bear


# ── FutureTrend: VolDelta × 3 periodos ───────────────────────
def _future_trend(o: np.ndarray, c: np.ndarray, v: np.ndarray, period: int) -> float:
    """
    Traducción EXACTA Pine:
      delta_vol = close > open ? volume : (close < open ? -volume : 0)
      for i = 0 to ft_period-1:
          avg = mean(delta[i], delta[i+period], delta[i+period*2])
          sum += avg
      return sum / period
    """
    o, c, v = _f(o), _f(c), _f(v)
    n = len(c)
    if n < period * 3 + 1: return 0.0
    delta = np.where(c > o, v, np.where(c < o, -v, 0.0))
    total = 0.0
    for i in range(period):
        i0 = n - 1 - i
        i1 = n - 1 - i - period
        i2 = n - 1 - i - period * 2
        if i2 >= 0:
            total += (delta[i0] + delta[i1] + delta[i2]) / 3.0
    return total / period


# ── ATR (Wilder) ─────────────────────────────────────────────
def _atr(h, l, c, p=14) -> tuple[np.ndarray, float]:
    h, l, c = _f(h), _f(l), _f(c)
    prev = np.roll(c, 1); prev[0] = c[0]
    tr   = np.maximum(h-l, np.maximum(np.abs(h-prev), np.abs(l-prev)))
    out  = np.zeros(len(tr))
    if len(tr) < p: return out, float(np.mean(h-l) + 1e-12)
    out[p-1] = tr[:p].mean()
    for i in range(p, len(tr)):
        out[i] = (out[i-1]*(p-1) + tr[i]) / p
    return out, max(float(out[-1]), 1e-12)


# ── Session filter ────────────────────────────────────────────
def _in_dead_session() -> bool:
    """01:30-05:00 UTC — evitar zona muerta Asia/pre-London."""
    now = datetime.datetime.utcnow()
    t   = now.hour * 60 + now.minute
    return 90 <= t < 300


# ── Correlation groups ────────────────────────────────────────
_CORR = [
    {"BTC-USDT","ETH-USDT"},
    {"SOL-USDT","AVAX-USDT","APT-USDT","SUI-USDT","NEAR-USDT"},
    {"ARB-USDT","OP-USDT","MATIC-USDT"},
    {"DOGE-USDT","SHIB-USDT","PEPE-USDT","FLOKI-USDT","BONK-USDT","WIF-USDT"},
]

def is_correlated(symbol: str, open_syms: set) -> bool:
    for grp in _CORR:
        if symbol in grp:
            for s in open_syms:
                if s in grp and s != symbol:
                    return True
    return False


# ─────────────────────────────────────────────────────────────
# SEÑAL PRINCIPAL
# ─────────────────────────────────────────────────────────────

def get_signal(
    ohlcv_5m:       dict,
    ohlcv_15m:      dict | None,
    ohlcv_1h:       dict | None,   # compat, no usado
    symbol:         str,
    open_syms:      set   = None,
    # params
    pivot_len:      int   = 5,
    atr_period:     int   = 14,
    atr_mult:       float = 1.5,
    rr:             float = 2.5,
    min_vol_mult:   float = 0.6,
    hma_len:        int   = 50,
    ft_period:      int   = 25,
    min_atr_pct:    float = 0.10,
    min_score:      int   = 3,
    # compat (ignorados)
    st_period:      int   = 10,
    st_mult:        float = 3.0,
    adx_period:     int   = 14,
    adx_min:        float = 0.0,
    rsi_period:     int   = 14,
    zz_deviation:   float = 0.5,
    zz15_deviation: float = 0.8,
) -> tuple[Signal | None, str]:

    if open_syms is None:
        open_syms = set()

    if not ohlcv_5m:
        return None, "no_data"

    h5 = ohlcv_5m["high"];  l5 = ohlcv_5m["low"]
    c5 = ohlcv_5m["close"]; o5 = ohlcv_5m["open"]
    v5 = ohlcv_5m["volume"]

    need = max(hma_len + 10, ft_period * 3 + 10, pivot_len * 4)
    if len(c5) < need:
        return None, f"bars_insuf_{len(c5)}"

    price = float(c5[-1])
    if price <= 0: return None, "precio_cero"

    # Session filter
    if _in_dead_session():
        return None, "sesion_muerta"

    # ATR
    _, atr_val = _atr(h5, l5, c5, atr_period)
    if atr_val / price * 100 < min_atr_pct:
        return None, f"plano_{atr_val/price*100:.3f}pct"

    # Volume
    vol_ma   = float(np.mean(v5[-20:])) if len(v5) >= 20 else 1.0
    vol_last = float(v5[-1])
    if vol_ma <= 0 or vol_last < vol_ma * min_vol_mult:
        return None, f"vol_bajo_{vol_last/max(vol_ma,1):.2f}x"

    # Correlation
    if is_correlated(symbol, open_syms):
        return None, "correlacion"

    # ── ZigZag: series de peak/valley (Pine forward-fill) ────
    pk_ser  = _peak_series(h5, pivot_len)
    vl_ser  = _valley_series(l5, pivot_len)

    long_zz  = _crossover(c5,  pk_ser)
    short_zz = _crossunder(c5, vl_ser)

    if not long_zz and not short_zz:
        pk = float(pk_ser[-1]) if not np.isnan(pk_ser[-1]) else price
        vl = float(vl_ser[-1]) if not np.isnan(vl_ser[-1]) else price
        dp = abs(price - pk) / atr_val
        dv = abs(price - vl) / atr_val
        return None, f"sin_cruce pk_dist={dp:.1f}R vl_dist={dv:.1f}R"

    # ── HMA 5m ───────────────────────────────────────────────
    hma5    = _hma(c5, hma_len)
    hb5, hd5 = _hma_direction(hma5, c5)

    if long_zz  and not hb5: return None, "long_HMA_bajista"
    if short_zz and not hd5: return None, "short_HMA_alcista"

    # ── FutureTrend 5m ───────────────────────────────────────
    ft5 = _future_trend(o5, c5, v5, ft_period)
    if long_zz  and ft5 <= 0: return None, f"long_FT_negativo={ft5:.0f}"
    if short_zz and ft5 >= 0: return None, f"short_FT_positivo={ft5:.0f}"

    # ── SCORE ─────────────────────────────────────────────────
    score   = 0
    reasons = []

    # +2 ruptura ZigZag (condición principal Pine)
    score += 2
    pk_v = float(pk_ser[-1]) if not np.isnan(pk_ser[-1]) else price
    vl_v = float(vl_ser[-1]) if not np.isnan(vl_ser[-1]) else price
    reasons.append(f"ZZ{'↑' if long_zz else '↓'}{pk_v if long_zz else vl_v:.5g}")

    # +1 HMA dirección
    score += 1
    reasons.append(f"HMA{'↑' if long_zz else '↓'}{hma5[-1]:.5g}")

    # +1 FutureTrend
    score += 1
    reasons.append(f"FT{ft5:+.0f}")

    # +1 confirmación 15m
    if ohlcv_15m and len(ohlcv_15m["close"]) > max(hma_len, ft_period * 3):
        h15 = ohlcv_15m["high"];  l15 = ohlcv_15m["low"]
        c15 = ohlcv_15m["close"]; o15 = ohlcv_15m["open"]
        v15 = ohlcv_15m["volume"]
        hma15     = _hma(c15, hma_len)
        hb15, hd15 = _hma_direction(hma15, c15)
        ft15       = _future_trend(o15, c15, v15, ft_period)
        mtf_ok = (long_zz  and hb15 and ft15 > 0) or \
                 (short_zz and hd15 and ft15 < 0)
        if mtf_ok:
            score += 1
            reasons.append("MTF✓")

    # +1 volumen spike
    vol_ratio = vol_last / vol_ma if vol_ma > 0 else 0.0
    if vol_last > vol_ma * 1.2:
        score += 1
        reasons.append(f"VOL{vol_ratio:.1f}x")

    if score < min_score:
        return None, f"score_bajo={score}/{min_score}"

    # ── SL/TP dinámico ────────────────────────────────────────
    sl_dist = atr_val * atr_mult
    tp_dist = sl_dist * rr

    if long_zz:
        return Signal(
            symbol=symbol, side="BUY", price=price,
            sl=round(price - sl_dist, 8),
            tp=round(price + tp_dist, 8),
            atr_5m=atr_val, peak=pk_v, valley=vl_v,
            hma_val=float(hma5[-1]), ft_val=ft5,
            score=score, vol_ratio=round(vol_ratio, 2),
            reasons=reasons,
        ), "ok"

    return Signal(
        symbol=symbol, side="SELL", price=price,
        sl=round(price + sl_dist, 8),
        tp=round(price - tp_dist, 8),
        atr_5m=atr_val, peak=pk_v, valley=vl_v,
        hma_val=float(hma5[-1]), ft_val=ft5,
        score=score, vol_ratio=round(vol_ratio, 2),
        reasons=reasons,
        st_bull_15m=False,
    ), "ok"


# ─────────────────────────────────────────────────────────────
# EXIT LOGIC
# ─────────────────────────────────────────────────────────────

def check_trail_exit(
    ohlcv_5m:    dict,
    ohlcv_15m:   dict | None,
    trade_side:  str,
    pivot_len:   int   = 5,
    st_period:   int   = 10,
    st_mult:     float = 3.0,
    rsi_period:  int   = 14,
    zz_deviation: float = 0.5,
    peak_r:      float = 0.0,
    hma_len:     int   = 50,
    ft_period:   int   = 25,
) -> str | None:
    """
    Prioridad de salida:
    1. HMA flip en 5m (más rápido, ~2-5 velas)
    2. FutureTrend flip (flujo de órdenes cambia)
    3. HMA flip en 15m
    4. Pivot break contrario
    """
    h5 = ohlcv_5m["high"]; l5 = ohlcv_5m["low"]
    c5 = ohlcv_5m["close"]; o5 = ohlcv_5m["open"]
    v5 = ohlcv_5m["volume"]

    if len(c5) < hma_len + 5:
        return None

    # 1. HMA 5m flip
    hma5      = _hma(c5, hma_len)
    hb5, hd5  = _hma_direction(hma5, c5)
    if trade_side == "BUY"  and hd5: return "HMA_FLIP"
    if trade_side == "SELL" and hb5: return "HMA_FLIP"

    # 2. FutureTrend flip
    if len(c5) > ft_period * 3:
        ft5 = _future_trend(o5, c5, v5, ft_period)
        if trade_side == "BUY"  and ft5 < 0: return "FT_FLIP"
        if trade_side == "SELL" and ft5 > 0: return "FT_FLIP"

    # 3. HMA 15m flip
    if ohlcv_15m and len(ohlcv_15m["close"]) > hma_len + 3:
        hma15      = _hma(ohlcv_15m["close"], hma_len)
        hb15, hd15 = _hma_direction(hma15, ohlcv_15m["close"])
        if trade_side == "BUY"  and hd15: return "HMA15_FLIP"
        if trade_side == "SELL" and hb15: return "HMA15_FLIP"

    # 4. Pivot break contrario
    if len(c5) > pivot_len * 4:
        price = float(c5[-1])
        if trade_side == "BUY":
            vl = _valley_series(l5, pivot_len)
            if not np.isnan(vl[-1]) and price < vl[-1]: return "PIVOT_BREAK"
        if trade_side == "SELL":
            pk = _peak_series(h5, pivot_len)
            if not np.isnan(pk[-1]) and price > pk[-1]: return "PIVOT_BREAK"

    return None
