"""
Volume-Weighted Order Block Strategy — KIBITO
================================================
Estrategia independiente. Implementación propia inspirada en el
concepto general de "order blocks ponderados por volumen" — no es
traducción de ningún script de terceros. Combina building blocks
genéricos de TA que ya se usan en el resto del fleet (pivotes, ATR
Wilder, ratio de volumen alcista/bajista) de una forma que hoy no
existe en ningún bot.

1. Tendencia: banda ATR alrededor de hl2 (mismo _atr Wilder que el
   resto de estrategias de este bot), flip cuando el precio la cruza.
2. Order block: al formarse un pivote a favor de la tendencia, se
   marca una zona de precio [pivot, pivot ± ATR].
3. Ratio de volumen: dentro de la ventana del pivote, qué fracción
   del volumen vino de velas alcistas vs bajistas.
4. Señal: retest de la zona con la tendencia todavía activa Y el
   ratio de volumen por encima de un mínimo configurable — el
   filtro que hoy no tiene ninguna otra estrategia del bot.
5. SL: extremo opuesto de la zona; TP: R múltiplo configurable.

Nueva — sin backtesting propio, sin validar en producción todavía.
Entra en prioridad 5 (último fallback) precisamente por eso.
"""
import logging
log = logging.getLogger("vol_ob")


def _atr(candles, p=14):
    trs=[max(c["high"]-c["low"],abs(c["high"]-candles[i-1]["close"]),abs(c["low"]-candles[i-1]["close"]))
         for i,c in enumerate(candles) if i>0]
    if not trs: return 0.0
    a=trs[0]
    for t in trs[1:]: a=t/p+a*(1-1/p)
    return a


def _pivot_high(candles, i, strength):
    """True si candles[i] es un máximo local frente a `strength` velas a cada lado."""
    if i - strength < 0 or i + strength >= len(candles):
        return False
    h = candles[i]["high"]
    for j in range(i - strength, i + strength + 1):
        if j == i:
            continue
        if candles[j]["high"] >= h:
            return False
    return True


def _pivot_low(candles, i, strength):
    if i - strength < 0 or i + strength >= len(candles):
        return False
    l = candles[i]["low"]
    for j in range(i - strength, i + strength + 1):
        if j == i:
            continue
        if candles[j]["low"] <= l:
            return False
    return True


def _trend_at(candles, atr_period, mult):
    """
    Flip de tendencia tipo chandelier: banda ATR alrededor de hl2.
    Devuelve trend para la última vela — 1 alcista, -1 bajista.
    """
    n = len(candles)
    trend = 1
    stop = None
    for i in range(atr_period + 1, n):
        window = candles[max(0, i - atr_period - 1): i + 1]
        atr = _atr(window, atr_period)
        if not atr:
            continue
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        upper = hl2 + mult * atr
        lower = hl2 - mult * atr
        close = candles[i]["close"]

        if stop is None:
            stop = lower if trend == 1 else upper

        if trend == 1:
            stop = max(stop, lower)
            if close < stop:
                trend, stop = -1, upper
        else:
            stop = min(stop, upper)
            if close > stop:
                trend, stop = 1, lower

    return trend


def _volume_ratio(candles, center_idx, window):
    """Fracción del volumen que vino de velas alcistas, ventana centrada en center_idx."""
    lo = max(0, center_idx - window)
    hi = min(len(candles), center_idx + window + 1)
    buy_vol = sell_vol = 0.0
    for c in candles[lo:hi]:
        if c["close"] >= c["open"]:
            buy_vol += c["volume"]
        else:
            sell_vol += c["volume"]
    total = buy_vol + sell_vol
    return buy_vol / total if total > 0 else 0.5


def get_signal(candles: list, config) -> dict:
    """
    candles: velas 5m, sorted oldest→newest.
    Returns dict con signal, entry_price, sl_price, tp_price, zone_top,
    zone_bot, buy_ratio, trend, atr.
    """
    R = {
        "signal": None, "entry_price": 0, "sl_price": 0, "tp_price": 0,
        "zone_top": 0, "zone_bot": 0, "buy_ratio": 0.5, "trend": 0, "atr": 0,
    }

    pivot_len  = getattr(config, "VOB_PIVOT_LEN", 7)
    atr_period = getattr(config, "VOB_ATR_LEN", 14)
    atr_mult   = getattr(config, "VOB_ATR_MULT", 3.5)
    min_ratio  = getattr(config, "VOB_MIN_VOL_RATIO", 0.55)
    rr         = getattr(config, "VOB_RR", 2.0)
    direction  = getattr(config, "DIRECTION", "BOTH")

    min_len = atr_period + pivot_len * 2 + 10
    if len(candles) < min_len:
        return R

    atr = _atr(candles[-(atr_period + 1):], atr_period)
    R["atr"] = atr
    if not atr:
        return R

    trend = _trend_at(candles, atr_period, atr_mult)
    R["trend"] = trend

    # Pivote más reciente a favor de la tendencia
    zone = None
    search_from = len(candles) - pivot_len - 2
    for i in range(search_from, pivot_len, -1):
        if trend == 1 and _pivot_low(candles, i, pivot_len):
            buy_ratio = _volume_ratio(candles, i, pivot_len)
            zone = {"top": candles[i]["low"] + atr, "bot": candles[i]["low"],
                    "buy_ratio": buy_ratio}
            break
        if trend == -1 and _pivot_high(candles, i, pivot_len):
            buy_ratio = _volume_ratio(candles, i, pivot_len)
            zone = {"top": candles[i]["high"], "bot": candles[i]["high"] - atr,
                    "buy_ratio": buy_ratio}
            break

    if zone is None:
        return R

    R.update({"zone_top": zone["top"], "zone_bot": zone["bot"],
              "buy_ratio": zone["buy_ratio"]})

    last = candles[-2]["close"]
    prev = candles[-3]["close"] if len(candles) > 2 else last

    if trend == 1 and direction in ("LONG", "BOTH"):
        retest = (prev < zone["top"] <= last) or (zone["bot"] <= last <= zone["top"])
        if retest and zone["buy_ratio"] >= min_ratio:
            sl = zone["bot"] - atr * 0.2
            risk = last - sl
            if risk > 0:
                R.update({"signal": "LONG", "entry_price": last,
                          "sl_price": sl, "tp_price": last + rr * risk})

    elif trend == -1 and direction in ("SHORT", "BOTH"):
        sell_ratio = 1 - zone["buy_ratio"]
        retest = (prev > zone["bot"] >= last) or (zone["bot"] <= last <= zone["top"])
        if retest and sell_ratio >= min_ratio:
            sl = zone["top"] + atr * 0.2
            risk = sl - last
            if risk > 0:
                R.update({"signal": "SHORT", "entry_price": last,
                          "sl_price": sl, "tp_price": last - rr * risk})

    return R


def check_tp_exit(candles: list, side: str, tp_price: float) -> bool:
    if not tp_price or not candles:
        return False
    last = candles[-2]["close"]
    return (side == "LONG" and last >= tp_price * 0.999) or \
           (side == "SHORT" and last <= tp_price * 1.001)
