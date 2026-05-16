"""indicators.py — Motor completo V50 Ultimate
V49 (Markov + ADX + STC + POC + pivots) +
Funding Rate filter +
Liquidation Map zones
"""
import numpy as np
import pandas as pd
from config import (
    SLOPE_MIN, LOOKBACK_MARKOV, PROB_THRESHOLD,
    ADX_LEN, ADX_TREND, ADX_RANGE,
    PIVOT_LEN, RVOL_MIN, POC_LOOKBACK,
    ATR_MULT_TP, ATR_MULT_SL,
    FUNDING_THRESHOLD, FUNDING_AVOID,
    LIQ_LOOKBACK, LIQ_MULTIPLIER,
)


# ══════════════════════════════════════════════════════════════
#  HELPERS TÉCNICOS
# ══════════════════════════════════════════════════════════════

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def _rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1/n, adjust=False).mean()

def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return _rma(tr, n)

def _adx(df: pd.DataFrame, n: int) -> pd.DataFrame:
    h, l, c = df["high"], df["low"], df["close"]
    up   = h - h.shift(1)
    down = l.shift(1) - l
    pdm  = np.where((up > down) & (up > 0), up, 0.0)
    mdm  = np.where((down > up) & (down > 0), down, 0.0)
    atr  = _rma(pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1), n)
    pdi  = 100 * _rma(pd.Series(pdm, index=df.index), n) / atr
    mdi  = 100 * _rma(pd.Series(mdm, index=df.index), n) / atr
    dx   = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"plus_di": pdi, "minus_di": mdi, "adx": _rma(dx.fillna(0), n)})

def _stc(close: pd.Series, sl=10, f=23, s=50) -> pd.Series:
    macd = _ema(close, f) - _ema(close, s)
    lo   = macd.rolling(sl).min()
    hi   = macd.rolling(sl).max()
    st   = 100 * (macd - lo) / (hi - lo).replace(0, np.nan)
    return _ema(st.fillna(50), 3)

def _vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)

def _pivot_high(df: pd.DataFrame, n: int) -> pd.Series:
    h = df["high"]
    r = pd.Series(np.nan, index=df.index)
    for i in range(n, len(df) - n):
        if h.iloc[i] == h.iloc[i-n:i+n+1].max():
            r.iloc[i] = h.iloc[i]
    return r

def _pivot_low(df: pd.DataFrame, n: int) -> pd.Series:
    l = df["low"]
    r = pd.Series(np.nan, index=df.index)
    for i in range(n, len(df) - n):
        if l.iloc[i] == l.iloc[i-n:i+n+1].min():
            r.iloc[i] = l.iloc[i]
    return r

def _poc(df: pd.DataFrame, n: int) -> float:
    sub = df.tail(n)
    return float(sub.loc[sub["volume"].idxmax(), "close"]) if not sub.empty else float("nan")


# ══════════════════════════════════════════════════════════════
#  LIQUIDATION MAP
# ══════════════════════════════════════════════════════════════

def calc_liq_zones(df: pd.DataFrame) -> dict:
    """
    Estima zonas donde hay acumulación de stops/liquidaciones.
    Usa los pivots históricos ponderados por volumen como proxy
    de dónde hay órdenes apalancadas atrapadas.
    Devuelve zonas long_liq (bajo precio) y short_liq (sobre precio).
    """
    sub  = df.tail(LIQ_LOOKBACK).copy()
    atr  = _atr(sub, 14).iloc[-1]
    ph   = _pivot_high(sub, 4).dropna()
    pl   = _pivot_low(sub, 4).dropna()

    price = float(df["close"].iloc[-1])

    long_liq_zones  = []
    short_liq_zones = []

    for idx, val in pl.items():
        zone_lo = val - atr * LIQ_MULTIPLIER
        zone_hi = val + atr * 0.3
        vol_weight = float(sub.loc[idx, "volume"]) if idx in sub.index else 1.0
        if zone_hi < price:
            long_liq_zones.append({"price": val, "zone_lo": zone_lo,
                                    "zone_hi": zone_hi, "weight": vol_weight})

    for idx, val in ph.items():
        zone_lo = val - atr * 0.3
        zone_hi = val + atr * LIQ_MULTIPLIER
        vol_weight = float(sub.loc[idx, "volume"]) if idx in sub.index else 1.0
        if zone_lo > price:
            short_liq_zones.append({"price": val, "zone_lo": zone_lo,
                                     "zone_hi": zone_hi, "weight": vol_weight})

    nearest_long  = sorted(long_liq_zones,  key=lambda x: abs(price - x["price"]))[:3]
    nearest_short = sorted(short_liq_zones, key=lambda x: abs(price - x["price"]))[:3]

    in_long_zone  = any(z["zone_lo"] <= price <= z["zone_hi"] for z in long_liq_zones)
    in_short_zone = any(z["zone_lo"] <= price <= z["zone_hi"] for z in short_liq_zones)

    return {
        "long_zones":    nearest_long,
        "short_zones":   nearest_short,
        "in_long_zone":  in_long_zone,
        "in_short_zone": in_short_zone,
        "zone_count":    len(long_liq_zones) + len(short_liq_zones),
    }


# ══════════════════════════════════════════════════════════════
#  MOTOR MARKOV
# ══════════════════════════════════════════════════════════════

class MarkovEngine:
    def __init__(self, lookback: int = LOOKBACK_MARKOV):
        self.lookback = lookback
        self.matrix   = np.zeros(9, dtype=float)

    def _state(self, slope: float, thr: float) -> int:
        if slope > thr:  return 0
        if slope < -thr: return 1
        return 2

    def update(self, slopes: pd.Series, thr: float) -> tuple[float, float]:
        self.matrix[:] = 0.0
        window = slopes.dropna().values[-self.lookback:]
        if len(window) < 2:
            return 0.0, 0.0
        states = np.array([self._state(s, thr) for s in window])
        for i in range(1, len(states)):
            self.matrix[states[i-1]*3 + states[i]] += 1.0
        cs   = states[-1]
        base = cs * 3
        tot  = self.matrix[base:base+3].sum()
        if tot == 0:
            return 0.0, 0.0
        return (self.matrix[base]/tot)*100, (self.matrix[base+1]/tot)*100


# ══════════════════════════════════════════════════════════════
#  PIPELINE PRINCIPAL
# ══════════════════════════════════════════════════════════════

def compute(df: pd.DataFrame, markov: MarkovEngine,
            funding_rate: float = 0.0) -> dict:
    """
    Recibe OHLCV + funding_rate actual.
    Devuelve dict completo con señales, indicadores y zonas.
    """
    df = df.copy()

    # ── Base ──────────────────────────────────────────────────
    ema7   = _ema(df["close"], 7)
    atr7   = _atr(df, 7)
    atr14  = _atr(df, 14)
    slope  = ((ema7 - ema7.shift(1)) / atr7.clip(lower=1e-9)) * 100

    # ── ADX adaptativo ────────────────────────────────────────
    adx_df     = _adx(df, ADX_LEN)
    adx_val    = float(adx_df["adx"].iloc[-1])
    is_trend   = adx_val > ADX_TREND
    is_range   = adx_val < ADX_RANGE
    adapt_thr  = SLOPE_MIN * (1.3 if is_range else 0.85 if is_trend else 1.0)

    # ── Markov ────────────────────────────────────────────────
    pb, pr = markov.update(slope, adapt_thr)

    # ── Filtros ───────────────────────────────────────────────
    vwap_s   = _vwap(df)
    vol_sma  = df["volume"].rolling(50).mean()
    rvol     = (df["volume"] / vol_sma.replace(0, np.nan)).fillna(0)
    is_dense = float(rvol.iloc[-1]) >= RVOL_MIN

    poc      = _poc(df, POC_LOOKBACK)
    stc      = float(_stc(df["close"]).iloc[-1])

    ph_s     = _pivot_high(df, PIVOT_LEN)
    pl_s     = _pivot_low(df, PIVOT_LEN)
    peak     = float(ph_s.dropna().iloc[-1]) if ph_s.dropna().shape[0] else float("nan")
    valley   = float(pl_s.dropna().iloc[-1]) if pl_s.dropna().shape[0] else float("nan")

    last     = df.iloc[-1]
    price    = float(last["close"])
    vwap_now = float(vwap_s.iloc[-1])
    slope_now= float(slope.iloc[-1])
    atr_now  = float(atr14.iloc[-1])
    rvol_now = float(rvol.iloc[-1])
    threshold= PROB_THRESHOLD - 5.0 if is_dense else PROB_THRESHOLD

    # ── Liquidation map ───────────────────────────────────────
    liq = calc_liq_zones(df)

    # ── Funding rate filter ───────────────────────────────────
    funding_extreme = abs(funding_rate) >= FUNDING_THRESHOLD
    funding_bull    = funding_rate < -FUNDING_THRESHOLD  # negativo = shorts pagan = bullish
    funding_bear    = funding_rate >  FUNDING_THRESHOLD  # positivo = longs pagan = bearish

    funding_ok_long  = not (FUNDING_AVOID and funding_bear and funding_extreme)
    funding_ok_short = not (FUNDING_AVOID and funding_bull and funding_extreme)

    # ── Señales V49 base ──────────────────────────────────────
    long_base = (
        not np.isnan(valley)         and
        float(last["low"])  < valley and
        price < vwap_now             and
        slope_now > adapt_thr        and
        is_dense                     and
        pb > threshold               and
        stc < 75
    )
    short_base = (
        not np.isnan(peak)            and
        float(last["high"]) > peak   and
        price > vwap_now              and
        slope_now < -adapt_thr        and
        is_dense                      and
        pr > threshold                and
        stc > 25
    )

    # ── Boost por liquidation map ─────────────────────────────
    # Si el precio está en zona de liquidación long → más prob de rebote alcista
    liq_boost_long  = liq["in_long_zone"]
    liq_boost_short = liq["in_short_zone"]

    # ── Señales finales con todos los filtros ─────────────────
    long_sig  = long_base  and funding_ok_long  and (not liq_boost_short)
    short_sig = short_base and funding_ok_short and (not liq_boost_long)

    # ── Score de confianza (0–100) ────────────────────────────
    def _score(base, liq_boost, fund_ok, prob):
        if not base: return 0
        s = min(prob, 80)
        if liq_boost:  s += 10
        if fund_ok:    s += 10
        return round(min(s, 100), 1)

    long_score  = _score(long_base,  liq_boost_long,  funding_ok_long,  pb)
    short_score = _score(short_base, liq_boost_short, funding_ok_short, pr)

    return {
        "long":           long_sig,
        "short":          short_sig,
        "long_score":     long_score,
        "short_score":    short_score,
        "close":          price,
        "atr14":          atr_now,
        "vwap":           vwap_now,
        "poc":            poc,
        "peak":           peak,
        "valley":         valley,
        "slope":          slope_now,
        "adaptive_slope": adapt_thr,
        "adx":            adx_val,
        "is_trending":    is_trend,
        "is_ranging":     is_range,
        "prob_bull":      pb,
        "prob_bear":      pr,
        "rvol":           rvol_now,
        "is_dense":       is_dense,
        "stc":            stc,
        "threshold":      threshold,
        "funding_rate":   funding_rate,
        "funding_extreme":funding_extreme,
        "funding_ok_long":funding_ok_long,
        "funding_ok_short":funding_ok_short,
        "liq":            liq,
        "liq_boost_long": liq_boost_long,
        "liq_boost_short":liq_boost_short,
    }
