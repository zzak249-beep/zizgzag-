"""
QF×JP v3.5 PREDATOR — Strategy Engine
Full Python port of the Pine Script indicator.
Main trigger: Trendline Breakout (TL RUPTURA) + composite score.
"""
import math
import logging
from typing import Optional
from datetime import datetime, timezone

log = logging.getLogger("qfjp.engine")

# ── Math helpers ──────────────────────────────────────────────────────────────

def tanh(x: float) -> float:
    x = max(-20.0, min(20.0, x))
    e2x = math.exp(2.0 * x)
    return (e2x - 1.0) / (e2x + 1.0)

def sma(arr: list, n: int) -> float:
    d = arr[-n:] if len(arr) >= n else arr
    return sum(d) / len(d) if d else 0.0

def ema(arr: list, n: int) -> float:
    if not arr: return 0.0
    k = 2.0 / (n + 1)
    val = arr[0]
    for v in arr[1:]:
        val = v * k + val * (1 - k)
    return val

def stdev(arr: list, n: int) -> float:
    d = arr[-n:] if len(arr) >= n else arr
    if len(d) < 2: return 0.0
    m = sum(d) / len(d)
    return math.sqrt(sum((x - m) ** 2 for x in d) / len(d))

def highest(arr: list, n: int) -> float:
    d = arr[-n:] if len(arr) >= n else arr
    return max(d) if d else 0.0

def lowest(arr: list, n: int) -> float:
    d = arr[-n:] if len(arr) >= n else arr
    return min(d) if d else float("inf")

def roc(closes: list, n: int) -> float:
    if len(closes) <= n: return 0.0
    base = closes[-n - 1]
    return (closes[-1] - base) / base if base != 0 else 0.0

def correlation(xs: list, ys: list, n: int) -> float:
    a, b = xs[-n:], ys[-n:]
    if len(a) < 4: return 0.0
    mx, my = sum(a) / len(a), sum(b) / len(b)
    num = sum((a[i] - mx) * (b[i] - my) for i in range(len(a)))
    dx  = math.sqrt(sum((x - mx) ** 2 for x in a))
    dy  = math.sqrt(sum((y - my) ** 2 for y in b))
    return num / (dx * dy) if dx * dy != 0 else 0.0

def atr_calc(candles: list, n: int) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return ema(trs, n) if trs else 0.0

def rsi_calc(closes: list, n: int) -> float:
    if len(closes) < n + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = ema(gains, n); al = ema(losses, n)
    if al == 0: return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1 + rs)

def adx_calc(candles: list, n: int) -> tuple[float, float, float]:
    """Returns (DI+, DI-, ADX)"""
    if len(candles) < n + 2: return 0.0, 0.0, 0.0
    dm_plus, dm_minus, trs = [], [], []
    for i in range(1, len(candles)):
        h, l   = candles[i]["high"],   candles[i]["low"]
        ph, pl = candles[i-1]["high"], candles[i-1]["low"]
        pc     = candles[i-1]["close"]
        up, dn = h - ph, pl - l
        dm_plus.append(up   if up > dn and up > 0 else 0.0)
        dm_minus.append(dn  if dn > up and dn > 0 else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    tr_sm  = ema(trs, n)
    dmp_sm = ema(dm_plus, n)
    dmn_sm = ema(dm_minus, n)
    di_p   = 100 * dmp_sm / tr_sm if tr_sm else 0.0
    di_n   = 100 * dmn_sm / tr_sm if tr_sm else 0.0
    dx     = 100 * abs(di_p - di_n) / (di_p + di_n) if (di_p + di_n) else 0.0
    # Smooth DX into ADX
    adx_val = ema([dx] * n, n)   # simplified; good enough for signal decisions
    return di_p, di_n, adx_val

def mfi_calc(candles: list, n: int) -> float:
    pos, neg = 0.0, 0.0
    for i in range(1, min(n + 1, len(candles))):
        tp_now  = (candles[-i]["high"]   + candles[-i]["low"]   + candles[-i]["close"])   / 3
        tp_prev = (candles[-i-1]["high"] + candles[-i-1]["low"] + candles[-i-1]["close"]) / 3
        flow = tp_now * candles[-i]["volume"]
        if tp_now > tp_prev: pos += flow
        elif tp_now < tp_prev: neg += flow
    if neg == 0: return 100.0 if pos > 0 else 50.0
    mf_ratio = pos / neg
    return 100.0 - 100.0 / (1 + mf_ratio)

def squeeze_fire(candles: list, sq_len: int, bb_mult: float, kc_mult: float) -> tuple[bool, bool]:
    """Returns (sq_bull_fire, sq_bear_fire)"""
    closes = [c["close"] for c in candles]
    basis  = sma(closes, sq_len)
    dev    = stdev(closes, sq_len)
    bb_hi, bb_lo = basis + bb_mult * dev, basis - bb_mult * dev
    atr    = atr_calc(candles, sq_len)
    ema_c  = ema(closes, sq_len)
    kc_hi, kc_lo = ema_c + kc_mult * atr, ema_c - kc_mult * atr
    sq_on  = bb_hi < kc_hi and bb_lo > kc_lo
    sq_on_prev = True   # simplified: assume prev was in squeeze
    sq_fire_now = not sq_on and sq_on_prev
    if not sq_fire_now: return False, False
    mid = (highest([c["high"] for c in candles], sq_len) +
           lowest([c["low"]   for c in candles], sq_len)) / 2
    val = closes[-1] - (mid + basis) / 2
    return val > 0, val < 0

# ── Trendline breakout (core trigger) ────────────────────────────────────────

def find_pivot_highs(candles: list, left: int, right: int) -> list[tuple[int, float]]:
    """Returns list of (bar_index, price) for confirmed pivot highs."""
    pivots = []
    for i in range(left, len(candles) - right):
        price = candles[i]["high"]
        if (all(candles[i]["high"] >= candles[i - j]["high"] for j in range(1, left + 1)) and
                all(candles[i]["high"] >= candles[i + j]["high"] for j in range(1, right + 1))):
            pivots.append((i, price))
    return pivots

def find_pivot_lows(candles: list, left: int, right: int) -> list[tuple[int, float]]:
    pivots = []
    for i in range(left, len(candles) - right):
        price = candles[i]["low"]
        if (all(candles[i]["low"] <= candles[i - j]["low"] for j in range(1, left + 1)) and
                all(candles[i]["low"] <= candles[i + j]["low"] for j in range(1, right + 1))):
            pivots.append((i, price))
    return pivots

def trendline_break(candles: list, left: int, right: int, lookback: int, buf_atr: float, atr: float) -> tuple[bool, bool]:
    """
    Returns (tl_break_long, tl_break_short).
    tl_break_long  = price broke above a descending trendline (join two lower highs)
    tl_break_short = price broke below an ascending trendline (join two higher lows)
    """
    n      = len(candles)
    buf    = atr * buf_atr
    close  = candles[-1]["close"]
    close1 = candles[-2]["close"] if n >= 2 else close

    # ── Descending TL (bearish TL → bullish breakout when broken) ────────
    ph_list = find_pivot_highs(candles[:-right], left, right)
    tl_break_long = False
    if len(ph_list) >= 2:
        ph1_idx, ph1_p = ph_list[-1]
        # Find earlier pivot that's HIGHER (forms descending TL)
        for ph2_idx, ph2_p in reversed(ph_list[:-1]):
            if ph2_p > ph1_p and (n - 1 - ph2_idx) <= lookback:
                slope   = (ph1_p - ph2_p) / max(ph1_idx - ph2_idx, 1)
                tl_now  = ph1_p + slope * (n - 1 - ph1_idx)
                tl_prev = ph1_p + slope * (n - 2 - ph1_idx)
                if close > tl_now + buf and close1 <= tl_prev + buf:
                    tl_break_long = True
                break

    # ── Ascending TL (bullish TL → bearish breakout when broken) ─────────
    pl_list = find_pivot_lows(candles[:-right], left, right)
    tl_break_short = False
    if len(pl_list) >= 2:
        pl1_idx, pl1_p = pl_list[-1]
        for pl2_idx, pl2_p in reversed(pl_list[:-1]):
            if pl2_p < pl1_p and (n - 1 - pl2_idx) <= lookback:
                slope   = (pl1_p - pl2_p) / max(pl1_idx - pl2_idx, 1)
                tl_now  = pl1_p + slope * (n - 1 - pl1_idx)
                tl_prev = pl1_p + slope * (n - 2 - pl1_idx)
                if close < tl_now - buf and close1 >= tl_prev - buf:
                    tl_break_short = True
                break

    return tl_break_long, tl_break_short


# ── CVD delta ────────────────────────────────────────────────────────────────

def cvd_score_calc(candles: list, cvd_len: int, roll: int) -> tuple[float, bool]:
    deltas = []
    for c in candles:
        hl = c["high"] - c["low"]
        bv = ((c["close"] - c["low"]) / hl * c["volume"]) if hl > 0 else c["volume"] * 0.5
        sv = ((c["high"] - c["close"]) / hl * c["volume"]) if hl > 0 else c["volume"] * 0.5
        deltas.append(bv - sv)
    window = deltas[-roll:]
    cvd    = sum(window)
    cvd_e  = ema(window, cvd_len)
    cvd_s  = stdev(window, cvd_len * 2) or 1.0
    z      = (cvd - cvd_e) / cvd_s
    score  = max(0.0, min(1.0, (tanh(z) + 1) / 2))
    rising = cvd > cvd_e
    return score, rising


# ── VDI ──────────────────────────────────────────────────────────────────────

def vdi_calc(candles: list, vdi_len: int, thr: float) -> tuple[float, bool, bool]:
    deltas = []
    for c in candles:
        hl = c["high"] - c["low"]
        bv = ((c["close"] - c["low"]) / hl * c["volume"]) if hl > 0 else c["volume"] * 0.5
        sv = ((c["high"] - c["close"]) / hl * c["volume"]) if hl > 0 else c["volume"] * 0.5
        deltas.append(bv - sv)
    vdi_sum = sum(deltas[-vdi_len:])
    history = [sum(deltas[max(0, i - vdi_len):i]) for i in range(vdi_len, len(deltas))]
    if not history: return 0.0, False, False
    avg = sum(history) / len(history)
    std = stdev(history, len(history)) or 1.0
    z   = (vdi_sum - avg) / std
    norm = max(0.0, min(1.0, (tanh(z) + 1) / 2))
    return z, z > thr, z < -thr


# ── Composite score ───────────────────────────────────────────────────────────

def composite_score(
    norm_score: float, cvd_s: float, mom_n: float, decay_r: float,
    htf_long: float, htf_short: float,
    struc_long: float, struc_short: float,
    vp_long: float, vp_short: float,
    sent_long: float, sent_short: float,
    vdi_n: float, conv_long: int, conv_short: int,
) -> tuple[int, int]:
    W = dict(score=0.22, cvd=0.20, mom=0.15, decay=0.08,
             htf=0.14, struc=0.08, vp=0.05, sent=0.04, vdi=0.04)

    ns_n  = (tanh(norm_score) + 1) / 2
    ns_ns = (tanh(-norm_score) + 1) / 2
    cvd_n = cvd_s
    cvd_s2 = 1.0 - cvd_n
    mom_l = max(0.0, min(1.0, (tanh(mom_n * 2) + 1) / 2))
    mom_s = max(0.0, min(1.0, (tanh(-mom_n * 2) + 1) / 2))

    raw_l = (W["score"] * ns_n + W["cvd"] * cvd_n + W["mom"] * mom_l +
             W["decay"] * decay_r + W["htf"] * htf_long +
             W["struc"] * min(1.0, struc_long) + W["vp"] * vp_long +
             W["sent"] * sent_long + W["vdi"] * vdi_n)
    raw_s = (W["score"] * ns_ns + W["cvd"] * cvd_s2 + W["mom"] * mom_s +
             W["decay"] * decay_r + W["htf"] * htf_short +
             W["struc"] * min(1.0, struc_short) + W["vp"] * vp_short +
             W["sent"] * sent_short + W["vdi"] * (1.0 - vdi_n))

    base_l = round(raw_l * 100)
    base_s = round(raw_s * 100)

    boost_l = min(100, base_l + round(conv_long * 0.5))
    boost_s = min(100, base_s + round(conv_short * 0.5))

    return min(100, boost_l), min(100, boost_s)


# ── Session ──────────────────────────────────────────────────────────────────

def current_session() -> str:
    h = datetime.now(timezone.utc).hour
    in_asia   = 0 <= h < 8
    in_london = 7 <= h < 16
    in_ny     = 13 <= h < 22
    if in_london and in_ny: return "OVL"
    if in_ny:      return "NY"
    if in_london:  return "LDN"
    if in_asia:    return "ASIA"
    return "OFF"


# ── Main engine ───────────────────────────────────────────────────────────────

class StrategyEngine:
    def __init__(self, settings):
        self.s = settings

    def evaluate(self, candles: list, htf_candles: dict | None = None) -> dict:
        """
        candles: list of dicts {ts, open, high, low, close, volume}, newest last, min 100 bars.
        htf_candles: optional dict {"15m": [...], "1h": [...], "4h": [...]}
        Returns signal dict with all computed values.
        """
        s  = self.s
        cs = [c["close"] for c in candles]
        hs = [c["high"]  for c in candles]
        ls = [c["low"]   for c in candles]
        vs = [c["volume"] for c in candles]

        # ── ATR ────────────────────────────────────────────────────────────
        atr      = atr_calc(candles, s.ATR_LEN)
        atr_avg20 = sma([atr_calc(candles[max(0,i-s.ATR_LEN-10):i+1], s.ATR_LEN)
                         for i in range(max(0, len(candles)-30), len(candles))], 20)

        # ── ADX / regime ───────────────────────────────────────────────────
        di_p, di_n, adx = adx_calc(candles, s.ADX_LEN)
        trend_strong = adx >= s.ADX_TREND
        trend_up     = di_p > di_n and trend_strong
        trend_dn     = di_n > di_p and trend_strong
        is_lateral   = adx < s.ADX_LAT
        reg_label    = "TEND↑" if trend_up else ("TEND↓" if trend_dn else ("LATERAL" if is_lateral else "NEUTRAL"))

        # ── L2 factors ─────────────────────────────────────────────────────
        roc_raw   = roc(cs, s.MOM_LEN)
        vol_n     = stdev(cs, s.MOM_LEN) / (sma(cs, s.MOM_LEN) or 1)
        f_mom     = roc_raw / vol_n if vol_n else 0.0

        basis     = sma(cs, s.REV_LEN)
        b_std     = stdev(cs, s.REV_LEN)
        f_rev     = -(cs[-1] - basis) / b_std if b_std else 0.0

        obv = 0.0
        for i in range(1, len(candles)):
            obv += vs[i] if cs[i] >= cs[i-1] else -vs[i]
        obv_ma  = sma([obv] * s.VOL_LEN, s.VOL_LEN)  # simplified
        obv_std = stdev(vs, s.VOL_LEN) or 1.0
        f_vol   = (obv - obv_ma) / obv_std

        adx_f   = min(1.0, adx / (s.ADX_TREND * 2.0))
        w_m     = 0.40 + adx_f * 0.40 * 0.40
        w_r     = max(0.30 * 0.30, 0.30 - adx_f * 0.30 * 0.50)
        w_v     = 0.30
        wt      = w_m + w_r + w_v
        raw_s   = (w_m * f_mom + w_r * f_rev + w_v * f_vol) / wt

        comp_s  = ema([raw_s] * s.MOM_LEN, 3)   # smoothed
        sc_std  = stdev([raw_s] * s.MOM_LEN, s.MOM_LEN) or 1.0
        norm_s  = tanh(comp_s / sc_std)

        # ── Decay ─────────────────────────────────────────────────────────
        fwd_ret   = (cs[-1] - cs[-2]) / cs[-2] if cs[-2] else 0.0
        ic_roll   = abs(correlation(cs[-s.MOM_LEN:], [fwd_ret] * s.MOM_LEN, min(s.MOM_LEN, len(cs))))
        decay_r   = min(1.0, max(0.0, ic_roll / 0.5))
        sig_alive = decay_r >= 0.40

        # ── CVD ────────────────────────────────────────────────────────────
        cvd_s, cvd_rising = cvd_score_calc(candles, s.CVD_LEN, min(s.CVD_ROLL, len(candles)))
        cvd_bull_div = cs[-1] < cs[-s.CVD_LEN] and cvd_s > 0.5
        cvd_bear_div = cs[-1] > cs[-s.CVD_LEN] and cvd_s < 0.5

        # ── VDI ────────────────────────────────────────────────────────────
        vdi_z, vdi_bull, vdi_bear = vdi_calc(candles, s.VDI_LEN, s.VDI_THR)
        vdi_norm = max(0.0, min(1.0, (tanh(vdi_z) + 1) / 2))

        # ── MFI ────────────────────────────────────────────────────────────
        mfi_val     = mfi_calc(candles, s.MFI_LEN)
        mfi_ob      = mfi_val > s.MFI_OB
        mfi_os      = mfi_val < s.MFI_OS
        mfi_bull_div = cs[-1] < cs[-s.MFI_LEN] and mfi_val > mfi_calc(candles[:-s.MFI_LEN], s.MFI_LEN) and mfi_val < 50
        mfi_bear_div = cs[-1] > cs[-s.MFI_LEN] and mfi_val < mfi_calc(candles[:-s.MFI_LEN], s.MFI_LEN) and mfi_val > 50

        # ── RSI ────────────────────────────────────────────────────────────
        rsi = rsi_calc(cs, s.RSI_LEN)
        rsi_bull_div = cs[-1] < cs[-s.RSI_DIV] and rsi > rsi_calc(cs[:-s.RSI_DIV], s.RSI_LEN) and rsi < 50
        rsi_bear_div = cs[-1] > cs[-s.RSI_DIV] and rsi < rsi_calc(cs[:-s.RSI_DIV], s.RSI_LEN) and rsi > 50

        # ── Squeeze ─────────────────────────────────────────────────────────
        sq_bull, sq_bear = squeeze_fire(candles, s.SQ_LEN, s.SQ_BBM, s.SQ_KCM)

        # ── Swing highs / lows ──────────────────────────────────────────────
        ph_list = find_pivot_highs(candles, 5, 3)
        pl_list = find_pivot_lows(candles,  5, 3)
        last_sh = ph_list[-1][1] if ph_list else None
        last_sl = pl_list[-1][1] if pl_list else None

        # ── Structure (CHoCH / BoS) ────────────────────────────────────────
        mkt_bull    = cs[-1] > last_sh if last_sh else True
        choch_bull  = last_sh and cs[-1] > last_sh and cs[-2] <= last_sh and not mkt_bull
        choch_bear  = last_sl and cs[-1] < last_sl and cs[-2] >= last_sl and mkt_bull
        bos_bull    = last_sh and cs[-1] > last_sh and cs[-2] <= last_sh and mkt_bull
        bos_bear    = last_sl and cs[-1] < last_sl and cs[-2] >= last_sl and not mkt_bull

        # ── Liquidity sweeps ────────────────────────────────────────────────
        liq_bull_sweep = last_sl and candles[-1]["low"] < last_sl and cs[-1] > last_sl
        liq_bear_sweep = last_sh and candles[-1]["high"] > last_sh and cs[-1] < last_sh

        # ── Sell / buy exhaustion ───────────────────────────────────────────
        hl_count = sum(1 for i in range(1, len(pl_list)) if pl_list[i][1] > pl_list[i-1][1])
        lh_count = sum(1 for i in range(1, len(ph_list)) if ph_list[i][1] < ph_list[i-1][1])
        sell_exhausted = hl_count >= 2
        buy_exhausted  = lh_count >= 2

        # ── HTF alignment ────────────────────────────────────────────────────
        htf_l, htf_s = 0, 0
        if htf_candles:
            for tf, clist in htf_candles.items():
                if len(clist) >= 22:
                    tf_cs = [c["close"] for c in clist]
                    f9  = ema(tf_cs, 9)
                    f21 = ema(tf_cs, 21)
                    if f9 > f21: htf_l += 1
                    else:        htf_s += 1
        else:
            # Fallback: use 15/50 EMA on current candles
            ema15 = ema(cs, 15); ema50 = ema(cs, 50)
            if ema15 > ema50: htf_l = 2
            else:              htf_s = 2

        htf_norm_l = min(1.0, htf_l / 3.0)
        htf_norm_s = min(1.0, htf_s / 3.0)

        # ── Dark pool ───────────────────────────────────────────────────────
        vol_base  = sma(vs, 20)
        dp_buy    = vs[-1] > vol_base * 2.5 and (hs[-1] - ls[-1]) < atr * 0.6 and cs[-1] > candles[-1]["open"]
        dp_sell   = vs[-1] > vol_base * 2.5 and (hs[-1] - ls[-1]) < atr * 0.6 and cs[-1] < candles[-1]["open"]

        # ── Volume filter ───────────────────────────────────────────────────
        vol_ok = atr > atr_avg20 * 0.70 if atr_avg20 > 0 else True

        # ── VWAP ───────────────────────────────────────────────────────────
        typical = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles[-50:]]
        vols50  = [c["volume"] for c in candles[-50:]]
        vwap    = sum(t * v for t, v in zip(typical, vols50)) / max(sum(vols50), 0.001)
        above_vwap = cs[-1] > vwap

        # ── OI synthetic ───────────────────────────────────────────────────
        buy_vol  = [v if c > o else 0.0 for c, o, v in zip(cs, [c["open"] for c in candles], vs)]
        sell_vol = [v if c < o else 0.0 for c, o, v in zip(cs, [c["open"] for c in candles], vs)]
        oi_buy_e  = ema(buy_vol,  20)
        oi_sell_e = ema(sell_vol, 20)
        oi_total  = oi_buy_e + oi_sell_e
        oi_ratio  = oi_buy_e / oi_total if oi_total > 0 else 0.5
        oi_conf_long  = cs[-1] > cs[-20] and oi_ratio > 0.55
        oi_conf_short = cs[-1] < cs[-20] and oi_ratio < 0.45

        # ── FVG synthetic ───────────────────────────────────────────────────
        in_bull_fvg = len(candles) >= 3 and ls[-1] > hs[-3] and (ls[-1] - hs[-3]) > atr * 0.3
        in_bear_fvg = len(candles) >= 3 and hs[-1] < ls[-3] and (ls[-3] - hs[-1]) > atr * 0.3

        # ── Order block synthetic ───────────────────────────────────────────
        strong_bull = (cs[-1] - candles[-1]["open"]) > atr * 1.5 and cs[-1] > cs[-2]
        in_bull_ob  = strong_bull and cs[-2] < candles[-2]["open"]
        strong_bear = (candles[-1]["open"] - cs[-1]) > atr * 1.5 and cs[-1] < cs[-2]
        in_bear_ob  = strong_bear and cs[-2] > candles[-2]["open"]

        # ── Asimetría momentum ─────────────────────────────────────────────
        up_rng = [h - l if c > o else 0.0 for h, l, c, o in
                  zip(hs[-10:], ls[-10:], cs[-10:], [c["open"] for c in candles[-10:]])]
        dn_rng = [h - l if c < o else 0.0 for h, l, c, o in
                  zip(hs[-10:], ls[-10:], cs[-10:], [c["open"] for c in candles[-10:]])]
        avg_up = sma(up_rng, 10); avg_dn = sma(dn_rng, 10)
        asym_bull = (avg_up / avg_dn >= 1.20) if avg_dn > 0 else False
        asym_bear = (avg_dn / avg_up >= 1.20) if avg_up > 0 else False

        # ── Exec OK (spread proxy) ──────────────────────────────────────────
        spread_pct = (hs[-1] - ls[-1]) / cs[-1] * 100
        exec_ok    = spread_pct < 0.18

        # ── Session ─────────────────────────────────────────────────────────
        session = current_session()
        ses_active = session in ("NY", "LDN", "OVL", "ASIA")

        # ── TRENDLINE BREAKOUT (main trigger) ──────────────────────────────
        tl_break_long, tl_break_short = trendline_break(
            candles, s.TL_PIVOT_L, s.TL_PIVOT_R, s.TL_LOOKBACK, s.TL_BUFFER, atr
        )

        # ── VP score proxy ─────────────────────────────────────────────────
        poc   = (max(hs[-100:]) + min(ls[-100:])) / 2  # simplified
        above_poc = cs[-1] > poc
        vp_l = 0.7 if above_poc else 0.4
        vp_s = 0.7 if not above_poc else 0.4

        # ── Sentiment (RSI-based) ───────────────────────────────────────────
        rsi_ext_long  = rsi < 30
        rsi_ext_short = rsi > 70
        sent_l = 0.8 if rsi_ext_long  else (0.5 if not rsi_ext_short else 0.2)
        sent_s = 0.8 if rsi_ext_short else (0.5 if not rsi_ext_long  else 0.2)

        # ── Convicción ─────────────────────────────────────────────────────
        conv_l = (
            (1 if norm_s   > 0.10            else 0) +
            (1 if sig_alive                   else 0) +
            (1 if exec_ok                     else 0) +
            (1 if htf_l   >= 2                else 0) +
            (1 if asym_bull                   else 0) +
            (1 if sell_exhausted              else 0) +
            (1 if tl_break_long or liq_bull_sweep or choch_bull else 0) +
            (1 if dp_buy                      else 0) +
            (1 if cvd_rising                  else 0) +
            (1 if sq_bull or in_bull_fvg or in_bull_ob else 0) +
            (1 if oi_conf_long                else 0) +
            (1 if above_vwap                  else 0) +
            (1 if vdi_bull                    else 0) +
            (1 if mfi_os or mfi_bull_div      else 0) +
            (1 if rsi_bull_div                else 0) +
            (1 if cvd_bull_div                else 0)
        )
        conv_s = (
            (1 if norm_s   < -0.10           else 0) +
            (1 if sig_alive                   else 0) +
            (1 if exec_ok                     else 0) +
            (1 if htf_s   >= 2                else 0) +
            (1 if asym_bear                   else 0) +
            (1 if buy_exhausted               else 0) +
            (1 if tl_break_short or liq_bear_sweep or choch_bear else 0) +
            (1 if dp_sell                     else 0) +
            (1 if not cvd_rising              else 0) +
            (1 if sq_bear or in_bear_fvg or in_bear_ob else 0) +
            (1 if oi_conf_short               else 0) +
            (1 if not above_vwap              else 0) +
            (1 if vdi_bear                    else 0) +
            (1 if mfi_ob or mfi_bear_div      else 0) +
            (1 if rsi_bear_div                else 0) +
            (1 if cvd_bear_div                else 0)
        )

        # ── Struc norm ─────────────────────────────────────────────────────
        str_l = (0.5 if mkt_bull else 0.0) + (0.3 if choch_bull or bos_bull else 0.0) + (0.2 if liq_bull_sweep else 0.0)
        str_s = (0.5 if not mkt_bull else 0.0) + (0.3 if choch_bear or bos_bear else 0.0) + (0.2 if liq_bear_sweep else 0.0)

        # ── Composite scores ────────────────────────────────────────────────
        sc_l, sc_s = composite_score(
            norm_s, cvd_s, f_mom, decay_r,
            htf_norm_l, htf_norm_s,
            str_l, str_s, vp_l, vp_s, sent_l, sent_s,
            vdi_norm, conv_l, conv_s,
        )

        # ── Tier classification ─────────────────────────────────────────────
        fuel_cat_l  = (tl_break_long or sq_bull or (in_bull_fvg and cvd_rising) or
                       liq_bull_sweep or choch_bull or bos_bull or vdi_bull)
        fuel_cat_s  = (tl_break_short or sq_bear or (in_bear_fvg and not cvd_rising) or
                       liq_bear_sweep or choch_bear or bos_bear or vdi_bear)

        base_l = sc_l >= s.SC_THR_STD and exec_ok and ses_active and sig_alive and vol_ok and sell_exhausted
        base_s = sc_s >= s.SC_THR_STD and exec_ok and ses_active and sig_alive and vol_ok and buy_exhausted

        long_std   = base_l and htf_l >= s.HTF_MIN
        short_std  = base_s and htf_s >= s.HTF_MIN
        long_fuel  = long_std  and sc_l >= s.SC_THR_FUEL and fuel_cat_l
        short_fuel = short_std and sc_s >= s.SC_THR_FUEL and fuel_cat_s
        long_sup   = long_fuel  and sc_l >= s.SC_THR_SUP and (dp_buy or cvd_bull_div or rsi_bull_div or mfi_bull_div)
        short_sup  = short_fuel and sc_s >= s.SC_THR_SUP and (dp_sell or cvd_bear_div or rsi_bear_div or mfi_bear_div)
        pre_long   = sc_l >= s.SC_THR_PRE and sc_l < s.SC_THR_STD and cvd_s >= 0.55 and vdi_bull
        pre_short  = sc_s >= s.SC_THR_PRE and sc_s < s.SC_THR_STD and cvd_s <= 0.45 and vdi_bear

        # Determine best signal
        if long_sup:       side, tier = "LONG",  "SUP"
        elif short_sup:    side, tier = "SHORT", "SUP"
        elif long_fuel:    side, tier = "LONG",  "FUEL"
        elif short_fuel:   side, tier = "SHORT", "FUEL"
        elif long_std:     side, tier = "LONG",  "STD"
        elif short_std:    side, tier = "SHORT", "STD"
        elif pre_long:     side, tier = "LONG",  "PRE"
        elif pre_short:    side, tier = "SHORT", "PRE"
        else:              side, tier = "NONE",  "NONE"

        # TL break must match the side
        tl_match = (
            (side == "LONG"  and tl_break_long)  or
            (side == "SHORT" and tl_break_short) or
            not s.REQUIRE_TL_BREAK
        )
        if not tl_match:
            side, tier = "NONE", "NONE"

        return {
            "side":         side,
            "tier":         tier,
            "score_long":   sc_l,
            "score_short":  sc_s,
            "atr":          atr,
            "adx":          adx,
            "reg_label":    reg_label,
            "cvd_score":    cvd_s,
            "cvd_rising":   cvd_rising,
            "cvd_div":      cvd_bull_div if side == "LONG" else cvd_bear_div,
            "vdi":          vdi_bull if side == "LONG" else vdi_bear,
            "obp":          False,   # full OB premium needs more history
            "sweep":        liq_bull_sweep if side == "LONG" else liq_bear_sweep,
            "choch":        choch_bull if side == "LONG" else choch_bear,
            "tl_break_long":  tl_break_long,
            "tl_break_short": tl_break_short,
            "mfi":          mfi_val,
            "rsi":          rsi,
            "session":      session,
            "conv_long":    conv_l,
            "conv_short":   conv_s,
            "htf_long":     htf_l,
            "htf_short":    htf_s,
            "sl_price":     None,   # filled by order manager
            "tp1_price":    None,
            "tp2_price":    None,
            "rr1":          None,
        }
