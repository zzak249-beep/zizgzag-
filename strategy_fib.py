"""
Fibonacci Golden Pocket Strategy — KIBITO
==========================================
1. Detecta swing high/low reciente (N velas)
2. Calcula niveles Fibonacci 0.5 y 0.618 (golden pocket)
3. LONG:  precio retrocede al 50-61.8% en tendencia alcista + confirmación
4. SHORT: precio retrocede al 50-61.8% en tendencia bajista + confirmación
5. Target: nivel 0.236 (extensión del movimiento)
"""
import logging

log = logging.getLogger("fib")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> float:
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def _atr(candles: list[dict], period: int = 14) -> float:
    trs = [max(c["high"] - c["low"],
               abs(c["high"] - candles[i-1]["close"]),
               abs(c["low"]  - candles[i-1]["close"]))
           for i, c in enumerate(candles) if i > 0]
    if not trs:
        return 0.0
    a = trs[0]
    for t in trs[1:]:
        a = t / period + a * (1 - 1 / period)
    return a

def _swing_high(candles: list[dict], lookback: int) -> float:
    return max(c["high"] for c in candles[-lookback:])

def _swing_low(candles: list[dict], lookback: int) -> float:
    return min(c["low"] for c in candles[-lookback:])

def _vol_avg(candles: list[dict], period: int = 20) -> float:
    vols = [c["volume"] for c in candles[-period:]]
    return sum(vols) / len(vols) if vols else 0


# ── Main signal ───────────────────────────────────────────────────────────────

def get_signal(candles_5m: list[dict],
               candles_1h: list[dict],
               config) -> dict:
    """
    Returns dict:
      signal:      "LONG" | "SHORT" | None
      entry_price: float
      sl_price:    float
      tp_price:    float  (nivel 0.236)
      swing_high:  float
      swing_low:   float
      fib_50:      float
      fib_618:     float
      fib_236:     float
      rsi:         float
      atr:         float
      trend:       "UP" | "DOWN" | "NONE"
    """
    result = {
        "signal": None, "entry_price": 0, "sl_price": 0, "tp_price": 0,
        "swing_high": 0, "swing_low": 0, "fib_50": 0, "fib_618": 0,
        "fib_236": 0, "rsi": 50, "atr": 0, "trend": "NONE",
    }

    lb = getattr(config, "FIB_LOOKBACK", 50)
    if len(candles_5m) < lb + 5 or len(candles_1h) < 20:
        return result

    # ── 1H HTF trend (EMA20 vs EMA50) ─────────────────────────────────────────
    h1_closes = [c["close"] for c in candles_1h]
    ema20_1h  = _ema(h1_closes[-20:], 20)
    ema50_1h  = _ema(h1_closes[-50:], 50) if len(h1_closes) >= 50 else h1_closes[-1]
    trend = "UP" if ema20_1h > ema50_1h else "DOWN"
    result["trend"] = trend

    # ── 5m indicators ─────────────────────────────────────────────────────────
    closes_5m = [c["close"] for c in candles_5m]
    rsi = _rsi(closes_5m, 14)
    atr = _atr(candles_5m, 14)
    result["rsi"] = rsi
    result["atr"] = atr

    current_close  = candles_5m[-2]["close"]   # última vela cerrada
    current_open   = candles_5m[-2]["open"]
    current_vol    = candles_5m[-2]["volume"]
    vol_avg        = _vol_avg(candles_5m, 20)

    # ── 2. Swing points ───────────────────────────────────────────────────────
    # Para LONG: buscamos un swing HIGH reciente seguido de retroceso
    # Para SHORT: buscamos un swing LOW reciente seguido de rebote
    sh = _swing_high(candles_5m[:-5], lb)   # excluye las últimas 5 para que sea "previo"
    sl = _swing_low(candles_5m[:-5],  lb)
    result["swing_high"] = sh
    result["swing_low"]  = sl

    move = sh - sl
    if move < atr * 2:   # movimiento mínimo para que tenga sentido el retroceso
        return result

    # ── 3. Fibonacci levels ───────────────────────────────────────────────────
    fib_236 = sh - move * 0.236
    fib_382 = sh - move * 0.382
    fib_50  = sh - move * 0.500
    fib_618 = sh - move * 0.618
    fib_786 = sh - move * 0.786

    result.update({"fib_50": fib_50, "fib_618": fib_618,
                   "fib_236": fib_236})

    zone_pct = getattr(config, "FIB_ZONE_PCT", 0.001)   # 0.1% tolerancia
    zone = move * 0.01 + atr * 0.3                        # zona dinámica

    # ── 4a. LONG — precio en golden pocket, tendencia alcista 1H ──────────────
    rsi_long_min = getattr(config, "FIB_RSI_LONG_MIN", 35)
    rsi_long_max = getattr(config, "FIB_RSI_LONG_MAX", 60)
    vol_ok = current_vol > vol_avg * 0.8

    long_in_pocket  = fib_618 - zone <= current_close <= fib_50 + zone
    long_candle     = current_close > current_open     # vela alcista
    long_rsi_ok     = rsi_long_min <= rsi <= rsi_long_max
    long_trend_ok   = trend == "UP"
    # Override: RSI extremadamente sobrevendido ignora filtro de tendencia
    extreme_oversold = rsi <= getattr(config, "FIB_EXTREME_RSI_LONG", 28)
    long_trend_final = long_trend_ok or extreme_oversold

    if long_in_pocket and long_candle and long_rsi_ok and long_trend_final and vol_ok:
        sl_price = fib_786 - atr * 0.5      # SL bajo el 0.786
        tp_price = sh                         # TP: máximo anterior (0%)
        result.update({
            "signal": "LONG",
            "entry_price": current_close,
            "sl_price": sl_price,
            "tp_price": tp_price,
        })
        return result

    # ── 4b. SHORT — precio rebota al golden pocket, tendencia bajista 1H ──────
    # Invertimos los niveles: swing LOW a swing HIGH
    fib_618_inv = sl + move * 0.618
    fib_50_inv  = sl + move * 0.500
    fib_786_inv = sl + move * 0.786

    rsi_short_min = getattr(config, "FIB_RSI_SHORT_MIN", 40)
    rsi_short_max = getattr(config, "FIB_RSI_SHORT_MAX", 65)

    short_in_pocket  = fib_50_inv - zone <= current_close <= fib_618_inv + zone
    short_candle     = current_close < current_open    # vela bajista
    short_rsi_ok     = rsi_short_min <= rsi <= rsi_short_max
    short_trend_ok   = trend == "DOWN"
    extreme_overbought = rsi >= getattr(config, "FIB_EXTREME_RSI_SHORT", 72)
    short_trend_final  = short_trend_ok or extreme_overbought

    if short_in_pocket and short_candle and short_rsi_ok and short_trend_final and vol_ok:
        sl_price = fib_786_inv + atr * 0.5   # SL sobre el 0.786 (invertido)
        tp_price = sl                          # TP: mínimo anterior (0% invertido)
        result.update({
            "signal": "SHORT",
            "entry_price": current_close,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "fib_50": fib_50_inv,
            "fib_618": fib_618_inv,
            "fib_236": sl + move * 0.236,
        })

    return result


def check_tp_exit(candles: list[dict], side: str,
                  tp_price: float) -> bool:
    """Precio alcanzó el target Fibonacci (nivel 0.236 o máximo)."""
    if not tp_price or not candles:
        return False
    last = candles[-2]["close"]
    if side == "LONG"  and last >= tp_price * 0.998:
        return True
    if side == "SHORT" and last <= tp_price * 1.002:
        return True
    return False
