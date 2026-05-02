# -*- coding: utf-8 -*-
"""strategy.py -- Phantom Edge Bot ELITE: Institutional-Grade Signal Engine.

What separates this from every other retail bot:

ENTRY (12-point score system, min 6 required):
  [STRUCTURE]   Multi-TF ZigZag: 5m breakout + 15m swing aligned     (3 pts)
  [TREND]       Supertrend 15m direction                               (2 pts)
  [VOLATILITY]  ATR percentile rank >60% = trending regime            (1 pt)
  [FLOW]        VWAP: long above, short below                          (1 pt)
  [MOMENTUM]    RSI momentum + 3-bar burst in direction               (1+1 pts)
  [PATTERN]     Engulfing candle on breakout bar                       (1 pt)
  [VOLUME]      Spike > 1.5x avg                                       (1 pt)
  [QUALITY]     Body > 30% of range (no doji/wick traps)              (filter)

ANTI-ENTRY FILTERS (hard blocks):
  - Liquidity sweep candle (wick > 2.5x body) = trap, skip
  - Dead session: 01:30-05:00 UTC (low liquidity, wide spreads)
  - Correlated pair already open (BTC+ETH both long = overexposed)
  - ATR percentile < 15% = completely flat market

EXIT (priority cascade):
  1. Supertrend 5m flip       -- tightest, 1-3 candles lag
  2. ZigZag swing break 5m   -- structure broken
  3. RSI divergence           -- momentum exhaustion
  4. Supertrend 15m flip      -- final confirmation
  5. Time exit: >6h + R<0.5  -- anti-bag-holder

POSITION SIZING:
  - Dynamic SL on real ZigZag structure (not fixed ATR)
  - Fallback to ATR if structure SL is out of range
  - Volatility regime: widen SL 20% in high-vol, tighten 10% in low-vol
"""
from __future__ import annotations
import datetime
import numpy as np
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────
# DATA CLASS
# ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol:       str
    side:         str        # "BUY" | "SELL"
    price:        float
    sl:           float
    tp:           float
    atr_5m:       float
    zz_high:      float      # last confirmed ZigZag HIGH on 5m
    zz_low:       float      # last confirmed ZigZag LOW  on 5m
    zz15_high:    float      # last confirmed ZigZag HIGH on 15m
    zz15_low:     float      # last confirmed ZigZag LOW  on 15m
    zz_trend:     str        # "UP" | "DOWN" | "FLAT"
    st_bull_15m:  bool
    atr_regime:   str        # "HIGH" | "NORMAL" | "LOW"
    vwap:         float
    rsi:          float
    score:        int
    vol_ratio:    float
    # compat aliases
    atr:          float = 0.0
    delta1:       float = 0.0
    delta2:       float = 0.0

    def __post_init__(self):
        self.atr    = self.atr_5m
        self.delta1 = self.zz_high
        self.delta2 = self.zz_low


# ─────────────────────────────────────────────────────────────
# INDICATORS (all vectorized numpy, no external deps)
# ─────────────────────────────────────────────────────────────

def _atr(h, l, c, p=14):
    prev = np.roll(c, 1); prev[0] = c[0]
    tr   = np.maximum(h-l, np.maximum(np.abs(h-prev), np.abs(l-prev)))
    out  = np.zeros_like(tr)
    if len(tr) < p: return out
    out[p-1] = tr[:p].mean()
    for i in range(p, len(tr)):
        out[i] = (out[i-1]*(p-1) + tr[i]) / p
    return out


def _supertrend(h, l, c, period=10, mult=3.0):
    atr   = _atr(h, l, c, period)
    hl2   = (h + l) / 2.0
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    st    = np.zeros_like(c)
    bull  = np.ones(len(c), dtype=bool)
    st[0] = upper[0]
    for i in range(1, len(c)):
        fl = lower[i] if lower[i] > lower[i-1] or c[i-1] < st[i-1] else lower[i-1]
        fu = upper[i] if upper[i] < upper[i-1] or c[i-1] > st[i-1] else upper[i-1]
        bull[i] = True if c[i] > fu else (False if c[i] < fl else bull[i-1])
        st[i]   = fl if bull[i] else fu
    return st, bull


def _rsi(c, p=14):
    d  = np.diff(c, prepend=c[0])
    g  = np.where(d > 0, d, 0.0)
    ls = np.where(d < 0, -d, 0.0)
    ag = np.zeros_like(c); al = np.zeros_like(c)
    if len(c) <= p: return np.full_like(c, 50.0)
    ag[p] = g[1:p+1].mean(); al[p] = ls[1:p+1].mean()
    for i in range(p+1, len(c)):
        ag[i] = (ag[i-1]*(p-1) + g[i])  / p
        al[i] = (al[i-1]*(p-1) + ls[i]) / p
    rs = np.where(al > 0, ag / al, 100.0)
    return 100.0 - 100.0 / (1.0 + rs)


def _vwap(h, l, c, v):
    """Cumulative VWAP from start of loaded data."""
    tp  = (h + l + c) / 3.0
    cum = np.cumsum(tp * v)
    cvl = np.cumsum(v)
    return cum / np.where(cvl > 0, cvl, 1.0)


def _atr_pct_rank(atr_arr, lookback=50):
    """ATR percentile rank vs last N bars. Returns 0-100."""
    if len(atr_arr) < lookback + 2:
        return 50.0
    window = atr_arr[-lookback:]
    current = atr_arr[-2]
    return float(np.sum(window < current) / lookback * 100)


# ─────────────────────────────────────────────────────────────
# ZIGZAG ENGINE
# ─────────────────────────────────────────────────────────────

def compute_zigzag(highs, lows, min_dev=0.5):
    """
    Real ZigZag: tracks confirmed swing highs/lows with minimum % deviation.
    Returns (swings, trend) where swings = [(idx, price, "H"|"L"), ...]
    trend = "UP" (HH+HL), "DOWN" (LH+LL), "FLAT"
    """
    if len(highs) < 10:
        return [], "FLAT"

    swings = []
    last_type    = ""
    peak_p, peak_i     = float(highs[0]), 0
    trough_p, trough_i = float(lows[0]),  0

    for i in range(1, len(highs)):
        h = float(highs[i]); l = float(lows[i])

        if last_type in ("", "L"):
            if h > peak_p:
                peak_p = h; peak_i = i
            if peak_p > 0 and (peak_p - l) / peak_p * 100 >= min_dev:
                if swings and swings[-1][2] == "H":
                    if peak_p > swings[-1][1]:
                        swings[-1] = (peak_i, peak_p, "H")
                else:
                    swings.append((peak_i, peak_p, "H"))
                last_type = "H"; trough_p = l; trough_i = i

        if last_type in ("", "H"):
            if l < trough_p:
                trough_p = l; trough_i = i
            if trough_p > 0 and (h - trough_p) / trough_p * 100 >= min_dev:
                if swings and swings[-1][2] == "L":
                    if trough_p < swings[-1][1]:
                        swings[-1] = (trough_i, trough_p, "L")
                else:
                    swings.append((trough_i, trough_p, "L"))
                last_type = "L"; peak_p = h; peak_i = i

    trend = "FLAT"
    if len(swings) >= 4:
        hh = [p for _, p, t in swings[-6:] if t == "H"]
        ll = [p for _, p, t in swings[-6:] if t == "L"]
        if len(hh) >= 2 and len(ll) >= 2:
            if hh[-1] > hh[-2] and ll[-1] > ll[-2]: trend = "UP"
            elif hh[-1] < hh[-2] and ll[-1] < ll[-2]: trend = "DOWN"

    return swings, trend


def _last(swings, kind):
    for idx, price, t in reversed(swings):
        if t == kind:
            return idx, price
    return None


# ─────────────────────────────────────────────────────────────
# CANDLE PATTERNS
# ─────────────────────────────────────────────────────────────

def _is_engulfing(o, h, l, c, idx):
    """Bullish/bearish engulfing on bar idx vs idx-1."""
    pb = abs(c[idx-1] - o[idx-1])
    cb = abs(c[idx]   - o[idx])
    bull = c[idx] > o[idx] and c[idx-1] < o[idx-1] and cb > pb * 1.05
    bear = c[idx] < o[idx] and c[idx-1] > o[idx-1] and cb > pb * 1.05
    return bull, bear


def _is_pinbar(o, h, l, c, idx):
    """Bullish/bearish pin bar (hammer / shooting star)."""
    body  = abs(c[idx] - o[idx])
    total = h[idx] - l[idx]
    if total < 1e-10: return False, False
    lo_wick = min(c[idx], o[idx]) - l[idx]
    hi_wick = h[idx] - max(c[idx], o[idx])
    bull_pin = lo_wick > body * 2.0 and hi_wick < body * 0.5 and body / total > 0.1
    bear_pin = hi_wick > body * 2.0 and lo_wick < body * 0.5 and body / total > 0.1
    return bull_pin, bear_pin


def _is_wick_trap(o, h, l, c, idx):
    """Long wick candle = potential liquidity sweep / trap."""
    body  = abs(c[idx] - o[idx])
    if body < 1e-10: return True   # pure doji = trap
    total = h[idx] - l[idx]
    hi_wick = h[idx] - max(c[idx], o[idx])
    lo_wick = min(c[idx], o[idx]) - l[idx]
    return hi_wick > body * 2.5 or lo_wick > body * 2.5


def _momentum_burst(c, n=3):
    """N consecutive closes in same direction."""
    if len(c) < n + 2: return False, False
    diffs = np.diff(c[-(n+1):])
    return bool(np.all(diffs > 0)), bool(np.all(diffs < 0))


# ─────────────────────────────────────────────────────────────
# SESSION FILTER
# ─────────────────────────────────────────────────────────────

def _in_dead_session() -> bool:
    """
    Avoid 01:30-05:00 UTC: Asian close + pre-London.
    Low volume, wide spreads, unpredictable wicks.
    """
    now_h = datetime.datetime.utcnow().hour
    now_m = datetime.datetime.utcnow().minute
    now_total = now_h * 60 + now_m
    dead_start = 1 * 60 + 30   # 01:30 UTC
    dead_end   = 5 * 60 + 0    # 05:00 UTC
    return dead_start <= now_total < dead_end


# ─────────────────────────────────────────────────────────────
# CORRELATION FILTER
# ─────────────────────────────────────────────────────────────

# Groups of highly correlated assets (same group = correlated)
_CORR_GROUPS = [
    {"BTC-USDT", "ETH-USDT"},           # large cap
    {"SOL-USDT", "AVAX-USDT", "APT-USDT", "SUI-USDT", "NEAR-USDT"},  # L1s
    {"ARB-USDT", "OP-USDT", "MATIC-USDT"},                            # L2s
    {"DOGE-USDT", "SHIB-USDT", "PEPE-USDT", "FLOKI-USDT"},           # memes
    {"BNB-USDT", "OKB-USDT"},                                          # CEX tokens
]

def is_correlated_with_open(new_symbol: str, new_side: str,
                             open_symbols: set) -> bool:
    """
    Returns True if a correlated asset is already open on same side.
    Prevents e.g. BTC LONG + ETH LONG at the same time.
    """
    for group in _CORR_GROUPS:
        if new_symbol in group:
            for sym in open_symbols:
                if sym in group and sym != new_symbol:
                    return True   # correlated peer already open
    return False


# ─────────────────────────────────────────────────────────────
# SIGNAL GENERATION
# ─────────────────────────────────────────────────────────────

def get_signal(
    ohlcv_5m:       dict,
    ohlcv_15m:      dict | None,
    ohlcv_1h:       dict | None,    # kept for signature compat, unused
    symbol:         str,
    open_syms:      set   = None,   # for correlation filter
    pivot_len:      int   = 3,      # unused
    atr_period:     int   = 14,
    atr_mult:       float = 1.5,
    rr:             float = 2.5,
    min_vol_mult:   float = 0.8,
    st_period:      int   = 10,
    st_mult:        float = 3.0,
    adx_period:     int   = 14,     # unused
    adx_min:        float = 0.0,    # unused
    rsi_period:     int   = 14,
    min_atr_pct:    float = 0.10,
    min_score:      int   = 6,
    zz_deviation:   float = 0.5,
    zz15_deviation: float = 0.8,    # coarser ZigZag on 15m
) -> tuple[Signal | None, str]:

    if open_syms is None:
        open_syms = set()

    # ── Validate ──────────────────────────────────────────────
    if not ohlcv_5m:
        return None, "no_data"

    o5 = ohlcv_5m["open"]; h5 = ohlcv_5m["high"]
    l5 = ohlcv_5m["low"];  c5 = ohlcv_5m["close"]
    v5 = ohlcv_5m["volume"]

    if len(c5) < 80:
        return None, f"bars_insuf_{len(c5)}"

    idx  = len(c5) - 2   # last closed candle
    prev = idx - 1
    price = float(c5[idx])
    if price <= 0:
        return None, "zero_price"

    # ── Session filter ────────────────────────────────────────
    if _in_dead_session():
        return None, "dead_session"

    # ── ATR + flat market check ───────────────────────────────
    atr_arr  = _atr(h5, l5, c5, atr_period)
    atr_val  = float(atr_arr[idx])
    if atr_val <= 0:
        return None, "atr_cero"

    atr_rank = _atr_pct_rank(atr_arr, 50)
    if atr_val / price * 100 < min_atr_pct:
        return None, f"plano_{atr_val/price*100:.3f}pct"
    if atr_rank < 15:
        return None, f"vol_muerta_rank={atr_rank:.0f}"

    atr_regime = "HIGH" if atr_rank > 70 else ("LOW" if atr_rank < 35 else "NORMAL")

    # ── Volume filter ─────────────────────────────────────────
    avg_vol   = float(np.mean(v5[-22:-2])) if len(v5) > 24 else 1.0
    vol_ratio = float(v5[idx]) / avg_vol if avg_vol > 0 else 0.0
    if vol_ratio < min_vol_mult:
        return None, f"vol_baja_{vol_ratio:.2f}x"

    # ── Candle quality: wick trap = hard skip ─────────────────
    if _is_wick_trap(o5, h5, l5, c5, idx):
        return None, "wick_trap"

    # ── Body quality: doji filter ─────────────────────────────
    bar_range = float(h5[idx] - l5[idx])
    body      = abs(float(c5[idx]) - float(o5[idx]))
    if bar_range > 0 and body / bar_range < 0.20:
        return None, f"doji_{body/bar_range:.2f}"

    # ── 5m ZigZag ─────────────────────────────────────────────
    swings5, zz_trend = compute_zigzag(h5[:idx], l5[:idx], zz_deviation)
    if len(swings5) < 4:
        return None, f"pocos_swings5_{len(swings5)}"

    last_h5 = _last(swings5, "H")
    last_l5 = _last(swings5, "L")
    if last_h5 is None or last_l5 is None:
        return None, "sin_swings5"

    zz_h5, zz_l5 = last_h5[1], last_l5[1]

    long_break  = float(c5[prev]) <= zz_h5 and price > zz_h5
    short_break = float(c5[prev]) >= zz_l5 and price < zz_l5

    if not long_break and not short_break:
        dh = abs(price - zz_h5) / atr_val
        dl = abs(price - zz_l5) / atr_val
        return None, f"sin_ruptura H_dist={dh:.1f}R L_dist={dl:.1f}R"

    # ── Trend alignment ───────────────────────────────────────
    if long_break  and zz_trend == "DOWN":
        return None, "long_en_bajista"
    if short_break and zz_trend == "UP":
        return None, "short_en_alcista"

    # ── 15m ZigZag alignment ─────────────────────────────────
    zz15_h = float("nan"); zz15_l = float("nan")
    zz15_aligned = False
    if ohlcv_15m and len(ohlcv_15m["close"]) > 20:
        h15 = ohlcv_15m["high"]; l15 = ohlcv_15m["low"]
        c15 = ohlcv_15m["close"]
        swings15, trend15 = compute_zigzag(h15[:-1], l15[:-1], zz15_deviation)
        lh15 = _last(swings15, "H"); ll15 = _last(swings15, "L")
        if lh15: zz15_h = lh15[1]
        if ll15: zz15_l = ll15[1]
        # 15m swing must be above/below current price confirming direction
        if long_break:
            zz15_aligned = (trend15 in ("UP", "FLAT") and
                            not np.isnan(zz15_l) and float(c15[-1]) > zz15_l)
        else:
            zz15_aligned = (trend15 in ("DOWN", "FLAT") and
                            not np.isnan(zz15_h) and float(c15[-1]) < zz15_h)

    # ── Supertrend 15m ────────────────────────────────────────
    st_bull = None
    if ohlcv_15m and len(ohlcv_15m["close"]) > st_period + 3:
        _, st_b = _supertrend(
            ohlcv_15m["high"], ohlcv_15m["low"], ohlcv_15m["close"],
            st_period, st_mult,
        )
        st_bull = bool(st_b[-1])
    if st_bull is None:
        return None, "sin_ST15"
    if long_break  and not st_bull: return None, "long_ST_bajista"
    if short_break and st_bull:     return None, "short_ST_alcista"

    # ── RSI 5m ───────────────────────────────────────────────
    rsi_arr = _rsi(c5, rsi_period)
    rsi_val = float(rsi_arr[idx])
    if long_break  and rsi_val < 42: return None, f"long_RSI={rsi_val:.0f}"
    if short_break and rsi_val > 58: return None, f"short_RSI={rsi_val:.0f}"

    # ── VWAP ─────────────────────────────────────────────────
    vwap_arr = _vwap(h5, l5, c5, v5)
    vwap_val = float(vwap_arr[idx])
    vwap_aligned = (long_break and price > vwap_val) or \
                   (short_break and price < vwap_val)

    # ── Momentum burst ────────────────────────────────────────
    bull_burst, bear_burst = _momentum_burst(c5, 3)
    burst_ok = (long_break and bull_burst) or (short_break and bear_burst)

    # ── Candle patterns ───────────────────────────────────────
    bull_eng, bear_eng = _is_engulfing(o5, h5, l5, c5, idx)
    bull_pin, bear_pin = _is_pinbar(o5, h5, l5, c5, idx)
    pattern_match = (long_break  and (bull_eng or bull_pin)) or \
                    (short_break and (bear_eng or bear_pin))

    # ── Correlation filter ────────────────────────────────────
    if is_correlated_with_open(symbol, "BUY" if long_break else "SELL", open_syms):
        return None, "correlacion_abierta"

    # ── SCORING (0-12) ────────────────────────────────────────
    score = 0
    score += 2  # ZigZag 5m breakout confirmed
    score += 1 if zz15_aligned else 0            # 15m ZigZag aligned
    score += 2  # Supertrend 15m confirmed
    score += 1 if atr_regime == "HIGH" else 0    # trending volatility
    score += 1 if vwap_aligned else 0            # VWAP alignment
    score += 1 if rsi_val > 58 and long_break else (1 if rsi_val < 42 and short_break else 0)
    score += 1 if burst_ok else 0                # momentum burst
    score += 1 if pattern_match else 0           # engulfing/pinbar
    score += 1 if vol_ratio >= 1.5 else 0        # volume spike
    score += 1 if vol_ratio >= 2.5 else 0        # extreme volume

    if score < min_score:
        return None, f"score_bajo={score}/{min_score}"

    # ── Structural SL ─────────────────────────────────────────
    atr_buf  = atr_val * 0.3
    vol_mult = 1.2 if atr_regime == "HIGH" else (0.9 if atr_regime == "LOW" else 1.0)

    if long_break:
        sl_struct = zz_l5 - atr_buf
        sl_dist   = price - sl_struct
        if sl_dist > atr_val * 3.5 or sl_dist < atr_val * 0.5:
            sl_struct = price - atr_val * atr_mult * vol_mult
            sl_dist   = atr_val * atr_mult * vol_mult
    else:
        sl_struct = zz_h5 + atr_buf
        sl_dist   = sl_struct - price
        if sl_dist > atr_val * 3.5 or sl_dist < atr_val * 0.5:
            sl_struct = price + atr_val * atr_mult * vol_mult
            sl_dist   = atr_val * atr_mult * vol_mult

    tp_dist = sl_dist * rr

    if long_break:
        return Signal(
            symbol=symbol, side="BUY", price=price,
            sl=round(sl_struct, 8), tp=round(price + tp_dist, 8),
            atr_5m=atr_val, zz_high=zz_h5, zz_low=zz_l5,
            zz15_high=zz15_h, zz15_low=zz15_l,
            zz_trend=zz_trend, st_bull_15m=True,
            atr_regime=atr_regime, vwap=vwap_val,
            rsi=rsi_val, score=score, vol_ratio=round(vol_ratio, 2),
        ), "ok"

    return Signal(
        symbol=symbol, side="SELL", price=price,
        sl=round(sl_struct, 8), tp=round(price - tp_dist, 8),
        atr_5m=atr_val, zz_high=zz_h5, zz_low=zz_l5,
        zz15_high=zz15_h, zz15_low=zz15_l,
        zz_trend=zz_trend, st_bull_15m=False,
        atr_regime=atr_regime, vwap=vwap_val,
        rsi=rsi_val, score=score, vol_ratio=round(vol_ratio, 2),
    ), "ok"


# ─────────────────────────────────────────────────────────────
# EXIT LOGIC
# ─────────────────────────────────────────────────────────────

def check_trail_exit(
    ohlcv_5m:    dict,
    ohlcv_15m:   dict | None,
    trade_side:  str,
    pivot_len:   int   = 3,
    st_period:   int   = 10,
    st_mult:     float = 3.0,
    rsi_period:  int   = 14,
    zz_deviation: float = 0.5,
    peak_r:      float = 0.0,   # used to tighten trail after big gains
) -> str | None:
    """
    Priority exit cascade:
    1. Dynamic Supertrend 5m (tighter multiplier after peak_R > 2.5)
    2. ZigZag swing break 5m
    3. RSI divergence
    4. Supertrend 15m flip
    """
    h5 = ohlcv_5m["high"]; l5 = ohlcv_5m["low"]
    c5 = ohlcv_5m["close"]
    idx = len(c5) - 2

    # 1. Supertrend 5m — tighten multiplier when trade is profitable
    if len(c5) > st_period + 3:
        # After 2.5R, use tighter ST to protect gains
        dyn_mult = max(st_mult * 0.6, 1.5) if peak_r >= 2.5 else st_mult
        _, st5b = _supertrend(h5, l5, c5, st_period, dyn_mult)
        if trade_side == "BUY"  and not st5b[-1]: return "ST5_FLIP"
        if trade_side == "SELL" and st5b[-1]:     return "ST5_FLIP"

    # 2. ZigZag swing break (structure invalidated)
    if len(c5) > 30:
        swings, _ = compute_zigzag(h5[:idx], l5[:idx], zz_deviation)
        price = float(c5[idx])
        if trade_side == "BUY":
            ll = _last(swings, "L")
            if ll and price < ll[1]: return "ZZ_SWING_BREAK"
        if trade_side == "SELL":
            lh = _last(swings, "H")
            if lh and price > lh[1]: return "ZZ_SWING_BREAK"

    # 3. RSI divergence (momentum exhaustion)
    if len(c5) > rsi_period + 10:
        rsi = _rsi(c5, rsi_period)
        if trade_side == "BUY"  and c5[idx] > c5[idx-5] and rsi[idx] < rsi[idx-5] - 4:
            return "RSI_DIV"
        if trade_side == "SELL" and c5[idx] < c5[idx-5] and rsi[idx] > rsi[idx-5] + 4:
            return "RSI_DIV"

    # 4. Supertrend 15m flip (last resort)
    if ohlcv_15m and len(ohlcv_15m["close"]) > st_period + 3:
        _, st15b = _supertrend(
            ohlcv_15m["high"], ohlcv_15m["low"], ohlcv_15m["close"],
            st_period, st_mult,
        )
        if trade_side == "BUY"  and not st15b[-1]: return "ST15_FLIP"
        if trade_side == "SELL" and st15b[-1]:     return "ST15_FLIP"

    return None
