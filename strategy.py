"""
EMA9 × VWAP strategy — ported from Pine Script v5.
All maths pure Python (no numpy).
"""

from datetime import datetime, timezone


# ────────────────────────────────────────────────────────────
# Indicator helpers
# ────────────────────────────────────────────────────────────

def _ema(prices: list, period: int) -> list:
    """Standard EMA — seeds from first SMA(period)."""
    n = len(prices)
    if n < period:
        return [None] * n
    k = 2.0 / (period + 1)
    out = [None] * (period - 1)
    seed = sum(prices[:period]) / period
    out.append(seed)
    for p in prices[period:]:
        out.append(out[-1] * (1.0 - k) + p * k)
    return out


def _vwap(highs, lows, closes, volumes, timestamps) -> list:
    """
    Session VWAP resetting at UTC midnight.
    typical_price = (H+L+C)/3
    """
    out = []
    cum_tpv = cum_v = 0.0
    prev_day = None
    for h, l, c, v, ts in zip(highs, lows, closes, volumes, timestamps):
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        if day != prev_day:
            cum_tpv = cum_v = 0.0
            prev_day = day
        tp = (h + l + c) / 3.0
        cum_tpv += tp * v
        cum_v   += v
        out.append(cum_tpv / cum_v if cum_v > 0 else c)
    return out


def _atr(highs, lows, closes, period: int) -> list:
    """Wilder ATR — identical to Pine Script ta.atr()."""
    n = len(closes)
    if n < period + 1:
        return [None] * n

    # True ranges (first TR is undefined)
    trs = [None]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)

    # Wilder smoothing
    out = [None] * period
    seed = sum(trs[1 : period + 1]) / period
    out.append(seed)
    for i in range(period + 1, n):
        out.append((out[-1] * (period - 1) + trs[i]) / period)
    return out


# ────────────────────────────────────────────────────────────
# Crossover / crossunder
# ────────────────────────────────────────────────────────────

def _cross_over(a, b) -> bool:
    """a[-1] > b[-1]  AND  a[-2] <= b[-2]."""
    return (
        None not in (a[-1], a[-2], b[-1], b[-2])
        and a[-1] > b[-1]
        and a[-2] <= b[-2]
    )


def _cross_under(a, b) -> bool:
    """a[-1] < b[-1]  AND  a[-2] >= b[-2]."""
    return (
        None not in (a[-1], a[-2], b[-1], b[-2])
        and a[-1] < b[-1]
        and a[-2] >= b[-2]
    )


# ────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────

def get_signal(candles: list, ema_period: int = 9, atr_period: int = 14,
               candles_1h: list = None, direction: str = "BOTH") -> dict:
    """
    candles:    5m candles sorted oldest→newest
    candles_1h: optional 1H candles for HTF trend filter
    direction:  "LONG" | "SHORT" | "BOTH"
    Returns {signal, ema9, vwap, atr, close, trend_1h}
    """
    empty = {"signal": None, "ema9": None, "vwap": None,
             "atr": None, "close": None, "trend_1h": "NONE"}
    if len(candles) < max(ema_period, atr_period) + 5:
        return empty

    ts  = [c["timestamp"] for c in candles]
    hi  = [c["high"]      for c in candles]
    lo  = [c["low"]       for c in candles]
    cl  = [c["close"]     for c in candles]
    vo  = [c["volume"]    for c in candles]

    ema9_s = _ema(cl, ema_period)
    vwap_s = _vwap(hi, lo, cl, vo, ts)
    atr_s  = _atr(hi, lo, cl, atr_period)

    # ── ATR chop filter: no entrar si mercado lateral (ATR muy bajo) ──────────
    atr_now = atr_s[-1]
    price   = cl[-1]
    atr_pct = atr_now / price if price > 0 else 0
    if atr_pct < 0.002:   # ATR < 0.2% del precio = demasiado choppy
        return {**empty, "ema9": ema9_s[-1], "vwap": vwap_s[-1],
                "atr": atr_now, "close": price}

    # ── 1H HTF trend filter ───────────────────────────────────────────────────
    trend_1h = "NONE"
    if candles_1h and len(candles_1h) >= 50:
        cl_1h = [c["close"] for c in candles_1h]
        ema20 = _ema(cl_1h, 20)[-1]
        ema50 = _ema(cl_1h, 50)[-1]
        trend_1h = "UP" if ema20 > ema50 else "DOWN"

    # ── Crossover detection ───────────────────────────────────────────────────
    e2 = ema9_s[-2:]
    v2 = vwap_s[-2:]

    signal = None
    if _cross_over(e2, v2) and direction in ("LONG", "BOTH"):
        # LONG solo si tendencia 1H es UP o no hay filtro
        if trend_1h in ("UP", "NONE"):
            signal = "LONG"
    elif _cross_under(e2, v2) and direction in ("SHORT", "BOTH"):
        # SHORT solo si tendencia 1H es DOWN o no hay filtro
        if trend_1h in ("DOWN", "NONE"):
            signal = "SHORT"

    return {
        "signal":   signal,
        "ema9":     ema9_s[-1],
        "vwap":     vwap_s[-1],
        "atr":      atr_now,
        "close":    price,
        "trend_1h": trend_1h,
    }


def update_trailing_stop(
    side: str,
    price: float,
    atr: float,
    mult: float,
    current_stop,
) -> float:
    """
    ATR trailing stop — mirrors Pine Script logic:
      LONG:  stop = max(stop, price - atr*mult)
      SHORT: stop = min(stop, price + atr*mult)
    """
    if side == "LONG":
        candidate = price - atr * mult
        return candidate if current_stop is None else max(current_stop, candidate)
    else:
        candidate = price + atr * mult
        return candidate if current_stop is None else min(current_stop, candidate)


def trail_stop_hit(side: str, price: float, stop: float) -> bool:
    if stop is None:
        return False
    return price <= stop if side == "LONG" else price >= stop
