# -*- coding: utf-8 -*-
"""
Estrategia Maki v4 -- ZigZag + 20MA 4H + RSI + Volumen
Alineada con el indicador Pine Script de TradingView:
  - Entradas en 5m (señal)
  - Filtro MA20 en 4H
  - Crossover/crossunder fiel al indicador TV
  - ATR para TP/SL dinámicos
"""
from typing import Optional

# ── Parámetros ────────────────────────────────────────────────────────────────
PIVOT_BARS   = 5      # velas a cada lado para confirmar pivot (= pivot_len en TV)
ATR_PERIOD   = 14
ATR_TP_MULT  = 2.0    # TP = ATR * 2
ATR_SL_MULT  = 1.0    # SL = ATR * 1
MIN_ATR_PCT  = 0.001  # 0.1% mínimo — más sensible en 5m
MAX_ATR_PCT  = 0.04   # 4% máximo
RSI_PERIOD   = 14
RSI_OB       = 70     # no LONG si sobrecomprado
RSI_OS       = 30     # no SHORT si sobrevendido
VOL_MA_BARS  = 20
VOL_MULT     = 1.1    # volumen ruptura >= 1.1x media (más permisivo en 5m)
MAX_EXT_PCT  = 0.003  # extensión máxima del breakout: 0.3%

TP_PCT_FIXED = 0.006
SL_PCT_FIXED = 0.003


# ── Indicadores ───────────────────────────────────────────────────────────────

def _sma(values: list[float], period: int) -> list[Optional[float]]:
    result: list[Optional[float]] = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i - period + 1: i + 1]) / period)
    return result


def _atr(candles: list[dict], period: int = ATR_PERIOD) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h  = candles[i]["h"]
        l  = candles[i]["l"]
        pc = candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def _rsi(candles: list[dict], period: int = RSI_PERIOD) -> Optional[float]:
    closes = [c["c"] for c in candles]
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def _vol_ok(candles: list[dict], bars: int = VOL_MA_BARS, mult: float = VOL_MULT) -> bool:
    if len(candles) < bars + 2:
        return True
    # Última vela cerrada ([-2]) vs media de las anteriores
    vols = [c["v"] for c in candles[-(bars + 2):-1]]
    avg  = sum(vols[:-1]) / max(len(vols) - 1, 1)
    return vols[-1] >= avg * mult


def _pivot_high(highs: list[float]) -> Optional[float]:
    """
    Replica ta.pivothigh(high, pivot_len, pivot_len) de Pine.
    Busca el pivot más reciente confirmado: PIVOT_BARS velas a cada lado.
    Retorna el valor del último pivot confirmado (puede ser varias velas atrás).
    """
    n = len(highs)
    # Empezamos desde la posición más reciente que puede estar confirmada
    for i in range(n - PIVOT_BARS - 1, PIVOT_BARS - 1, -1):
        h = highs[i]
        if (
            all(h > highs[i - j] for j in range(1, PIVOT_BARS + 1))
            and all(h > highs[i + j] for j in range(1, PIVOT_BARS + 1))
        ):
            return h
    return None


def _pivot_low(lows: list[float]) -> Optional[float]:
    """
    Replica ta.pivotlow(low, pivot_len, pivot_len) de Pine.
    """
    n = len(lows)
    for i in range(n - PIVOT_BARS - 1, PIVOT_BARS - 1, -1):
        lo = lows[i]
        if (
            all(lo < lows[i - j] for j in range(1, PIVOT_BARS + 1))
            and all(lo < lows[i + j] for j in range(1, PIVOT_BARS + 1))
        ):
            return lo
    return None


def _ma20_4h_state(candles_4h: list[dict]) -> tuple[bool, bool, float, float]:
    """
    Retorna (ma_up, price_above_ma, ma_value, last_close).
    Alineado con Pine: ma20_4h_subiendo = ma20_4h > ma20_4h[1]
    """
    closes  = [c["c"] for c in candles_4h]
    ma_vals = _sma(closes, 20)
    valid   = [(i, v) for i, v in enumerate(ma_vals) if v is not None]
    if len(valid) < 2:
        return False, False, 0.0, 0.0

    _, ma_now  = valid[-1]
    _, ma_prev = valid[-2]

    # Slope: misma lógica que Pine (barra actual vs barra anterior)
    ma_up   = ma_now > ma_prev
    ma_down = ma_now < ma_prev

    last_close = closes[-2]   # última vela 4H cerrada
    above_ma   = last_close > ma_now
    below_ma   = last_close < ma_now

    return ma_up, ma_down, above_ma, below_ma, ma_now, last_close


# ── Señal principal ───────────────────────────────────────────────────────────

def signal(candles_5m: list[dict], candles_4h: list[dict]) -> Optional[str]:
    """
    Replica fiel del indicador Pine de TradingView con timeframe 5m.

    LONG:
      1. MA20 4H subiendo (ma_now > ma_prev)
      2. Precio 4H por encima de MA20
      3. RSI 5m < RSI_OB (no sobrecomprado)
      4. Crossover del precio sobre el último pivot high:
         → prev_close <= peak  y  curr_close > peak
      5. Extensión de ruptura < MAX_EXT_PCT
      6. Volumen de ruptura >= VOL_MULT * media
      7. ATR dentro de rango de volatilidad

    SHORT: simétrico.
    """
    min_bars = PIVOT_BARS * 2 + 5
    if len(candles_5m) < min_bars or len(candles_4h) < 22:
        return None

    # ── 4H MA state ───────────────────────────────────────────────────────────
    state = _ma20_4h_state(candles_4h)
    ma_up, ma_down, above_ma, below_ma, ma_val, close_4h = state

    if not ma_up and not ma_down:
        return None

    # ── ATR 5m ────────────────────────────────────────────────────────────────
    atr   = _atr(candles_5m)
    price = candles_5m[-2]["c"]
    if atr is not None:
        atr_pct = atr / price
        if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
            return None

    # ── RSI 5m ────────────────────────────────────────────────────────────────
    rsi = _rsi(candles_5m)
    can_long  = ma_up  and above_ma
    can_short = ma_down and below_ma

    if rsi is not None:
        if rsi >= RSI_OB:
            can_long = False
        if rsi <= RSI_OS:
            can_short = False

    if not can_long and not can_short:
        return None

    # ── Pivotes en 5m ─────────────────────────────────────────────────────────
    highs  = [c["h"] for c in candles_5m]
    lows   = [c["l"] for c in candles_5m]
    peak   = _pivot_high(highs)
    valley = _pivot_low(lows)

    if peak is None or valley is None:
        return None

    # Usamos las dos últimas velas cerradas para detectar el crossover
    # (en 5m la vela [-1] está en formación, [-2] es la última cerrada, [-3] es la anterior)
    prev_c = candles_5m[-3]["c"]   # vela cerrada anterior
    curr_c = candles_5m[-2]["c"]   # última vela cerrada

    # ── LONG: crossover alcista sobre pivot high ───────────────────────────────
    if can_long and prev_c <= peak < curr_c:
        ext = (curr_c - peak) / peak
        if ext <= MAX_EXT_PCT and _vol_ok(candles_5m):
            return "LONG"

    # ── SHORT: crossunder bajista bajo pivot low ───────────────────────────────
    if can_short and prev_c >= valley > curr_c:
        ext = (valley - curr_c) / valley
        if ext <= MAX_EXT_PCT and _vol_ok(candles_5m):
            return "SHORT"

    return None


# ── TP / SL basados en ATR ────────────────────────────────────────────────────

def tp_sl(entry: float, side: str, candles: list[dict] | None = None) -> tuple[float, float]:
    atr = _atr(candles) if candles else None
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
    if side == "LONG":
        reward = tp - entry
        risk   = entry - sl
    else:
        reward = entry - tp
        risk   = sl - entry
    return reward / risk if risk > 0 else 0.0


def get_rsi_value(candles: list[dict]) -> Optional[float]:
    return _rsi(candles)


def get_atr_pct(candles: list[dict]) -> Optional[float]:
    atr   = _atr(candles)
    price = candles[-2]["c"] if len(candles) >= 2 else None
    if atr and price:
        return atr / price * 100
    return None
