"""
bot/indicators.py
Implementación pura en NumPy/Pandas de todos los indicadores utilizados por
el Sniper Bot V49 y el Kotegawa Dip Reversal.

No depende de librerías externas de TA para máxima portabilidad y control.
"""
import numpy as np
import pandas as pd
from typing import Tuple, Optional


# ──────────────────────────────────────────────
# UTILIDADES INTERNAS
# ──────────────────────────────────────────────

def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing (RMA), equivalente a Pine Script ta.rma()."""
    result = pd.Series(index=series.index, dtype=float)
    result.iloc[period - 1] = series.iloc[:period].mean()
    alpha = 1.0 / period
    for i in range(period, len(series)):
        result.iloc[i] = result.iloc[i - 1] * (1 - alpha) + series.iloc[i] * alpha
    return result


# ──────────────────────────────────────────────
# MEDIAS MÓVILES
# ──────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


# ──────────────────────────────────────────────
# ATR
# ──────────────────────────────────────────────

def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return _wilder_smooth(tr, period)


# ──────────────────────────────────────────────
# ADX / DMI
# ──────────────────────────────────────────────

def adx_dmi(high: pd.Series, low: pd.Series, close: pd.Series,
            period: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Returns: (plus_di, minus_di, adx)
    """
    up   = high.diff()
    down = -low.diff()

    plus_dm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0),
                         index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0),
                         index=high.index)

    tr_series = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)

    atr_w     = _wilder_smooth(tr_series, period)
    plus_di   = 100 * _wilder_smooth(plus_dm,  period) / atr_w.replace(0, np.nan)
    minus_di  = 100 * _wilder_smooth(minus_dm, period) / atr_w.replace(0, np.nan)

    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _wilder_smooth(dx.fillna(0), period)
    return plus_di, minus_di, adx


# ──────────────────────────────────────────────
# VWAP (sesión — se resetea cada 24 h de datos)
# ──────────────────────────────────────────────

def vwap(high: pd.Series, low: pd.Series, close: pd.Series,
         volume: pd.Series) -> pd.Series:
    typical = (high + low + close) / 3
    cum_vol = volume.cumsum()
    cum_pv  = (typical * volume).cumsum()
    return cum_pv / cum_vol.replace(0, np.nan)


# ──────────────────────────────────────────────
# RVOL (Relative Volume)
# ──────────────────────────────────────────────

def rvol(volume: pd.Series, period: int = 50) -> pd.Series:
    vol_ma = sma(volume, period)
    return volume / vol_ma.replace(0, np.nan)


# ──────────────────────────────────────────────
# POC (Point of Control — precio con mayor volumen)
# ──────────────────────────────────────────────

def poc(close: pd.Series, volume: pd.Series, lookback: int) -> pd.Series:
    """
    Para cada barra, el precio de cierre con mayor volumen
    en la ventana lookback anterior.
    """
    result = pd.Series(index=close.index, dtype=float)
    for i in range(lookback, len(close)):
        window_vol = volume.iloc[i - lookback: i].values
        window_cls = close.iloc[i - lookback: i].values
        idx_max    = np.argmax(window_vol)
        result.iloc[i] = window_cls[idx_max]
    return result


# ──────────────────────────────────────────────
# MAGIC SLOPE
# ──────────────────────────────────────────────

def magic_slope(close: pd.Series, ema_period: int = 7,
                atr_period: int = 7) -> pd.Series:
    e   = ema(close, ema_period)
    a   = atr(close, close, close, atr_period)   # solo para normalizar
    # Usamos high/low reales cuando disponibles (pasados como close aquí
    # pero se sobreescribirá en strategy.py con la versión correcta)
    slope = ((e - e.shift(1)) / a.clip(lower=1e-10)) * 100
    return slope


def magic_slope_full(high: pd.Series, low: pd.Series, close: pd.Series,
                     ema_period: int = 7, atr_period: int = 7) -> pd.Series:
    e = ema(close, ema_period)
    a = atr(high, low, close, atr_period)
    return ((e - e.shift(1)) / a.clip(lower=1e-10)) * 100


# ──────────────────────────────────────────────
# PIVOT HIGHS / LOWS
# ──────────────────────────────────────────────

def pivot_high(high: pd.Series, length: int) -> pd.Series:
    """Devuelve el valor si es pivot high, NaN si no."""
    result = pd.Series(np.nan, index=high.index)
    for i in range(length, len(high) - length):
        window = high.iloc[i - length: i + length + 1]
        if high.iloc[i] == window.max():
            result.iloc[i] = high.iloc[i]
    return result


def pivot_low(low: pd.Series, length: int) -> pd.Series:
    result = pd.Series(np.nan, index=low.index)
    for i in range(length, len(low) - length):
        window = low.iloc[i - length: i + length + 1]
        if low.iloc[i] == window.min():
            result.iloc[i] = low.iloc[i]
    return result


# ──────────────────────────────────────────────
# STC — Schaff Trend Cycle
# ──────────────────────────────────────────────

def stc(close: pd.Series, stc_len: int = 10,
        fast: int = 23, slow: int = 50) -> pd.Series:
    macd_line = ema(close, fast) - ema(close, slow)

    def _stoch_of(series: pd.Series, period: int) -> pd.Series:
        low_  = series.rolling(period).min()
        high_ = series.rolling(period).max()
        return 100 * (series - low_) / (high_ - low_).replace(0, np.nan)

    stoch1 = _stoch_of(macd_line, stc_len)
    d1     = ema(stoch1.fillna(50), 3)
    stoch2 = _stoch_of(d1, stc_len)
    d2     = ema(stoch2.fillna(50), 3)
    return d2


# ──────────────────────────────────────────────
# RSI
# ──────────────────────────────────────────────

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = _wilder_smooth(gain, period)
    avg_loss = _wilder_smooth(loss, period)
    rs   = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ──────────────────────────────────────────────
# BOLLINGER BANDS
# ──────────────────────────────────────────────

def bollinger_bands(close: pd.Series, period: int = 20,
                    mult: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    basis = sma(close, period)
    dev   = close.rolling(period).std()
    upper = basis + mult * dev
    lower = basis - mult * dev
    return upper, basis, lower


# ──────────────────────────────────────────────
# ADAPTIVE SLOPE THRESHOLD  (V50.1)
# ──────────────────────────────────────────────

def adaptive_slope_threshold(adx: pd.Series, slope_base: float,
                              adx_trend: int, adx_range: int) -> pd.Series:
    """
    Devuelve el umbral de slope adaptado al régimen de mercado:
    - Rango     → más exigente (× 1.30)
    - Tendencia → más permisivo (× 0.85)
    - Transición → base
    """
    result = pd.Series(slope_base, index=adx.index)
    result = result.where(adx >= adx_range,  slope_base * 1.30)  # ranging
    result = result.where(adx <= adx_trend,  slope_base * 0.85)  # trending
    return result
