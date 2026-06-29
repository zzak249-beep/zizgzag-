"""
PDH BOS Retest Strategy — KIBITO
=================================
1. Previous Day High / Low (PDH / PDL) from daily candles
2. 1H candle close breaks PDH → BOS LONG confirmed
   1H candle close breaks PDL → BOS SHORT confirmed
3. On 5m: price retests the broken level → LONG / SHORT entry
4. Exit: EMA8 (5m) break OR ATR trail stop

Designed for BingX Perpetual Futures, hedge mode.
"""

import logging
import time
from typing import Optional

log = logging.getLogger("pdh_bos")

# ── EMA helper ────────────────────────────────────────────────────────────────
def _ema(values: list[float], period: int) -> float:
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

# ── ATR helper ────────────────────────────────────────────────────────────────
def _atr(candles: list[dict], period: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    trs = trs[-period:]
    atr = trs[0]
    k = 1.0 / period
    for t in trs[1:]:
        atr = t * k + atr * (1 - k)
    return atr

# ── Main signal function ───────────────────────────────────────────────────────
def get_signal(client, symbol: str, config) -> dict:
    """
    Returns dict:
      signal:      "LONG" | "SHORT" | None
      entry_price: float
      sl_price:    float
      tp_price:    float
      pdh:         float
      pdl:         float
      bos_level:   float
      ema8:        float
      atr:         float
    """
    result = {
        "signal": None, "entry_price": 0, "sl_price": 0,
        "tp_price": 0, "pdh": 0, "pdl": 0,
        "bos_level": 0, "ema8": 0, "atr": 0,
    }

    try:
        # ── 1. Previous Day High / Low ────────────────────────────────────────
        daily = client.get_klines(symbol, "1d", limit=3)
        if len(daily) < 2:
            return result
        # daily[-1] = today (incomplete), daily[-2] = yesterday (complete)
        pdh = daily[-2]["high"]
        pdl = daily[-2]["low"]
        result["pdh"] = pdh
        result["pdl"] = pdl

        # ── 2. 1H BOS detection ───────────────────────────────────────────────
        h1 = client.get_klines(symbol, "1h", limit=5)
        if len(h1) < 3:
            return result
        # h1[-1] = current forming, h1[-2] = last closed
        h1_last  = h1[-2]  # last confirmed 1H candle
        h1_prev  = h1[-3]  # previous 1H candle

        bos_long  = h1_last["close"] > pdh and h1_prev["close"] <= pdh
        bos_short = h1_last["close"] < pdl and h1_prev["close"] >= pdl

        if not bos_long and not bos_short:
            return result

        bos_level = pdh if bos_long else pdl
        result["bos_level"] = bos_level

        # ── 3. 5m retest entry ────────────────────────────────────────────────
        m5 = client.get_klines(symbol, "5m", limit=60)
        if len(m5) < 15:
            return result

        closes = [c["close"] for c in m5]
        ema8 = _ema(closes, 8)
        atr = _atr(m5[-20:], 14)
        result["ema8"] = ema8
        result["atr"]  = atr

        current_close = m5[-2]["close"]   # last confirmed 5m candle
        current_low   = m5[-2]["low"]
        current_high  = m5[-2]["high"]
        price_now     = m5[-1]["close"]   # current live price

        zone_pct = getattr(config, "PDH_RETEST_ZONE_PCT", 0.0015)  # 0.15%
        zone      = bos_level * zone_pct + atr * 0.3

        if bos_long:
            # Price pulled back to test PDH level (now support) and EMA8 is below
            retesting  = current_low  <= bos_level + zone
            bouncing   = current_close > bos_level
            ema_ok     = current_close > ema8
            if retesting and bouncing and ema_ok:
                sl  = bos_level - atr * getattr(config, "SL_ATR",  1.5)
                tp  = bos_level + atr * getattr(config, "TP1_ATR", 3.0)
                result.update({"signal": "LONG",  "entry_price": price_now,
                               "sl_price": sl,   "tp_price": tp})
        else:
            # Price pulled back to test PDL level (now resistance) and EMA8 above
            retesting  = current_high >= bos_level - zone
            bouncing   = current_close < bos_level
            ema_ok     = current_close < ema8
            if retesting and bouncing and ema_ok:
                sl  = bos_level + atr * getattr(config, "SL_ATR",  1.5)
                tp  = bos_level - atr * getattr(config, "TP1_ATR", 3.0)
                result.update({"signal": "SHORT", "entry_price": price_now,
                               "sl_price": sl,   "tp_price": tp})

    except Exception as e:
        log.warning(f"get_signal {symbol}: {e}")

    return result


def check_ema8_exit(client, symbol: str, position_side: str) -> bool:
    """Exit when last confirmed 5m candle closes on wrong side of EMA8."""
    try:
        m5 = client.get_klines(symbol, "5m", limit=20)
        if len(m5) < 10:
            return False
        closes = [c["close"] for c in m5]
        ema8 = _ema(closes[:-1], 8)   # EMA from confirmed candles
        last_close = m5[-2]["close"]
        if position_side == "LONG"  and last_close < ema8:
            return True
        if position_side == "SHORT" and last_close > ema8:
            return True
    except Exception as e:
        log.warning(f"check_ema8_exit {symbol}: {e}")
    return False
