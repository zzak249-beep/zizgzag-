"""
FibStruct Strategy Engine
Python port of FibStruct Pine Script v1.5.2

Modules: pivot detection · EQH/EQL · liquidity sweeps · BOS/CHoCH
         Fibonacci retracements · confluence scoring · engulfing · signals
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field

NAN = float("nan")


@dataclass
class StrategyParams:
    swing_len:     int   = 10
    atr_filter:    bool  = True
    atr_mult:      float = 0.5
    cooldown:      int   = 5
    eq_tol:        float = 0.1
    conf_tol:      float = 0.3
    strict_engulf: bool  = True
    sweep_boost:   bool  = True


@dataclass
class SignalResult:
    confirmed_buy:  bool  = False
    confirmed_sell: bool  = False
    buy_trigger:    str   = ""
    sell_trigger:   str   = ""
    structure_bias: int   = 0
    conf_score:     float = 0.0
    in_premium:     bool  = False
    in_discount:    bool  = False
    sweep_high:     bool  = False
    sweep_low:      bool  = False
    is_bos:         bool  = False
    is_choch:       bool  = False
    fib_direction:  int   = 0
    fib_swing_high: float = NAN
    fib_swing_low:  float = NAN
    fib_levels:     dict  = field(default_factory=dict)
    sw_high1:       float = NAN
    sw_low1:        float = NAN
    atr:            float = 0.0
    close:          float = 0.0


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


def _pivot_high(series: pd.Series, left: int, right: int) -> pd.Series:
    v = series.values
    n = len(v)
    out = np.full(n, NAN)
    for i in range(left + right, n):
        p, pv = i - right, v[i - right]
        if (all(v[p - j] <= pv for j in range(1, left + 1)) and
                all(v[p + j] <= pv for j in range(1, right + 1))):
            out[i] = pv
    return pd.Series(out, index=series.index)


def _pivot_low(series: pd.Series, left: int, right: int) -> pd.Series:
    v = series.values
    n = len(v)
    out = np.full(n, NAN)
    for i in range(left + right, n):
        p, pv = i - right, v[i - right]
        if (all(v[p - j] >= pv for j in range(1, left + 1)) and
                all(v[p + j] >= pv for j in range(1, right + 1))):
            out[i] = pv
    return pd.Series(out, index=series.index)


def _fib_levels(sh: float, sl: float, direction: int) -> dict:
    r = sh - sl
    if direction == 1:
        return {
            "0.236": sh - r * 0.236, "0.382": sh - r * 0.382,
            "0.500": sh - r * 0.500, "0.618": sh - r * 0.618,
            "0.786": sh - r * 0.786,
            "-0.500": sh + r * 0.500, "-0.618": sh + r * 0.618,
        }
    return {
        "0.236": sl + r * 0.236, "0.382": sl + r * 0.382,
        "0.500": sl + r * 0.500, "0.618": sl + r * 0.618,
        "0.786": sl + r * 0.786,
        "-0.500": sl - r * 0.500, "-0.618": sl - r * 0.618,
    }


# ─────────────────────────────────────────────
# Main computation
# ─────────────────────────────────────────────

def compute(df: pd.DataFrame, params: StrategyParams) -> SignalResult:
    """
    Process all closed bars chronologically.
    Returns the signal for the last bar.

    df columns required: open  high  low  close  volume
    All rows are treated as confirmed (closed) candles.
    Minimum required bars ≈ swing_len*3 + 50 + 10.
    """
    sl = params.swing_len
    n  = len(df)
    if n < max(sl * 3, 50) + sl * 2 + 10:
        return SignalResult()

    atr_s  = _atr(df, 14)
    ph_s   = _pivot_high(df["high"], sl, sl)
    pl_s   = _pivot_low(df["low"],   sl, sl)
    body_s = (df["close"] - df["open"]).abs()
    bema_s = body_s.ewm(span=14, adjust=False).mean()

    warmup = max(sl * 3, 50)

    # ── state ──
    sh1 = sh2 = NAN;  sh1i = sh2i = -1
    sl1 = sl2 = NAN;  sl1i = sl2i = -1
    struct_bias = 0
    brkhi = brkli = -1
    eqh_act = eql_act = False
    eqh_px  = eql_px  = NAN
    eqhi    = eqli    = -1
    swph_i  = swpl_i  = -1
    fsh = fsl = NAN
    fdir = 0
    fh_live = fl_live = False
    bars_sig = 999
    result = SignalResult()

    for i in range(n):
        atr_v = float(atr_s.iloc[i]) if not np.isnan(atr_s.iloc[i]) else 0.0
        hi = float(df["high"].iloc[i]);  lo = float(df["low"].iloc[i])
        cl = float(df["close"].iloc[i]); op = float(df["open"].iloc[i])
        body  = float(body_s.iloc[i])
        b_avg = float(bema_s.iloc[i]) if not np.isnan(bema_s.iloc[i]) else body
        warm  = i >= warmup
        bars_sig += 1

        # ── pivots ──
        rph = float(ph_s.iloc[i]); rpl = float(pl_s.iloc[i])
        new_sh = not np.isnan(rph); new_sl = not np.isnan(rpl)
        amin = atr_v * params.atr_mult if params.atr_filter else 0.0

        if new_sh:
            ok = np.isnan(sl1) or (rph - sl1) >= amin
            if ok:
                sh2, sh2i = sh1, sh1i
                sh1, sh1i = rph, i - sl

        if new_sl:
            ok = np.isnan(sh1) or (sh1 - rpl) >= amin
            if ok:
                sl2, sl2i = sl1, sl1i
                sl1, sl1i = rpl, i - sl

        # ── EQH / EQL ──
        et = atr_v * params.eq_tol if atr_v > 0 else 0.0
        if new_sh and not np.isnan(sh2) and warm and abs(sh1 - sh2) <= et:
            eqh_act = True; eqh_px = (sh1 + sh2) / 2; eqhi = sh2i
        if new_sl and not np.isnan(sl2) and warm and abs(sl1 - sl2) <= et:
            eql_act = True; eql_px = (sl1 + sl2) / 2; eqli = sl2i

        # ── sweeps ──
        swph = swpl = False
        swph_ref = swpl_ref = 0.0
        if warm:
            rh   = eqh_px if (eqh_act and not np.isnan(eqh_px)) else sh1
            rl   = eql_px if (eql_act and not np.isnan(eql_px)) else sl1
            rhi  = eqhi if eqh_act else sh1i
            rli  = eqli if eql_act else sl1i
            can_h = not np.isnan(rh) and (swph_i == -1 or rhi != swph_i)
            can_l = not np.isnan(rl) and (swpl_i == -1 or rli != swpl_i)
            if can_h and hi > rh and cl < rh and op < rh:
                swph = True; swph_ref = rh; swph_i = rhi
                if eqh_act: eqh_act = False
            if can_l and lo < rl and cl > rl and op > rl:
                swpl = True; swpl_ref = rl; swpl_i = rli
                if eql_act: eql_act = False

        # ── BOS / CHoCH ──
        is_bos = is_choch = is_bull = is_bear = False
        if warm:
            bb = not np.isnan(sh1) and cl > sh1 and (brkhi == -1 or sh1i != brkhi)
            rb = not np.isnan(sl1) and cl < sl1 and (brkli == -1 or sl1i != brkli)
            if bb and rb:
                rb = False if struct_bias <= 0 else True; bb = not rb
            if bb:
                is_choch = struct_bias <= 0; is_bos = not is_choch
                if is_choch: swph_i = swpl_i = -1
                struct_bias = 1; is_bull = True; brkhi = sh1i
            if rb:
                is_choch = struct_bias >= 0; is_bos = not is_choch
                if is_choch: swph_i = swpl_i = -1
                struct_bias = -1; is_bear = True; brkli = sl1i

        # ── fib anchors ──
        if is_choch and is_bull: fdir=1; fsh=hi; fsl=sl1; fh_live=True;  fl_live=False
        if is_choch and is_bear: fdir=-1; fsl=lo; fsh=sh1; fl_live=True; fh_live=False
        if is_bos and is_bull:
            fsh=hi
            if not np.isnan(sl1): fsl=sl1
            fh_live=True; fl_live=False
        if is_bos and is_bear:
            fsl=lo
            if not np.isnan(sh1): fsh=sh1
            fl_live=True; fh_live=False
        if not is_bull and not is_bear:
            if fh_live and not np.isnan(fsh) and hi > fsh: fsh = hi
            if fl_live and not np.isnan(fsl) and lo < fsl: fsl = lo
        if new_sh and fh_live and not np.isnan(sh1):  fsh = sh1; fh_live = False
        if new_sl and fl_live and not np.isnan(sl1):  fsl = sl1; fl_live = False
        if new_sh and not fh_live and not np.isnan(sh1) and not np.isnan(fsh) and sh1 != fsh: fsh = sh1
        if new_sl and not fl_live and not np.isnan(sl1) and not np.isnan(fsl) and sl1 != fsl: fsl = sl1

        # ── fib levels ──
        fib_ok = not np.isnan(fsh) and not np.isnan(fsl) and fsh > fsl and fdir != 0
        fibs: dict = _fib_levels(fsh, fsl, fdir) if fib_ok else {}

        # ── premium / discount ──
        f500 = fibs.get("0.500", NAN)
        in_prem = in_disc = False
        if fib_ok and not np.isnan(f500):
            if fdir == 1: in_prem = cl > f500; in_disc = cl <= f500
            else:         in_prem = cl < f500; in_disc = cl >= f500

        # ── confluence ──
        ct = atr_v * params.conf_tol if atr_v > 0 else 0.001
        def near(lv: float) -> bool:
            return not np.isnan(lv) and (
                abs(cl - lv) <= ct or (lo <= lv + ct and hi >= lv - ct))
        cw = sum(w for k, w in [
            ("0.236",1.0),("0.382",1.5),("0.500",2.0),("0.618",2.5),("0.786",1.5)
        ] if k in fibs and near(fibs[k]))
        if not np.isnan(sh1) and near(sh1): cw += 1.0
        if not np.isnan(sl1) and near(sl1): cw += 1.0
        if params.sweep_boost and (swph or swpl): cw += 2.0
        cscore = min(cw * 10.0, 100.0)

        # ── engulfing ──
        bear_eng = bull_eng = False
        if i >= 1:
            po  = float(df["open"].iloc[i-1]); pc  = float(df["close"].iloc[i-1])
            pb  = float(body_s.iloc[i-1])
            pba = float(bema_s.iloc[i-1]) if not np.isnan(bema_s.iloc[i-1]) else pb
            big   = body > pb
            long_ = body > b_avg
            s_prv = pb < pba
            if params.strict_engulf:
                bear_eng = cl<op and long_ and big and pc>po and s_prv and op>pc and cl<po
                bull_eng = cl>op and long_ and big and pc<po and s_prv and op<pc and cl>po
            else:
                bear_eng = cl<op and long_ and big and pc>po and s_prv and cl<=po and op>=pc and (cl<po or op>pc)
                bull_eng = cl>op and long_ and big and pc<po and s_prv and cl>=po and op<=pc and (cl>po or op<pc)
        bear_ctx = bear_eng and warm and (in_prem or cw >= 1.5)
        bull_ctx = bull_eng and warm and (in_disc or cw >= 1.5)

        # ── signals ──
        be  = bull_ctx   and struct_bias ==  1 and cw >= 1.5
        bc  = is_choch   and is_bull
        bsw = swpl       and warm and (in_disc or cw >= 2.0)
        se  = bear_ctx   and struct_bias == -1 and cw >= 1.5
        sc  = is_choch   and is_bear
        ssw = swph       and warm and (in_prem or cw >= 2.0)

        br = be or bc or bsw; sr = se or sc or ssw
        if br and sr: br = sr = False

        cb = br and bars_sig >= params.cooldown and warm
        cs = sr and bars_sig >= params.cooldown and warm

        def _t(choch, swp, eng):
            return "+".join(p for p, f in [("choch",choch),("sweep",swp),("engulf",eng)] if f)
        bt = _t(bc, bsw, be) if cb else ""
        st = _t(sc, ssw, se) if cs else ""
        if cb or cs: bars_sig = 0

        if i == n - 1:
            result = SignalResult(
                confirmed_buy=cb, confirmed_sell=cs,
                buy_trigger=bt, sell_trigger=st,
                structure_bias=struct_bias, conf_score=cscore,
                in_premium=in_prem, in_discount=in_disc,
                sweep_high=swph, sweep_low=swpl,
                is_bos=is_bos, is_choch=is_choch,
                fib_direction=fdir, fib_swing_high=fsh, fib_swing_low=fsl,
                fib_levels=fibs, sw_high1=sh1, sw_low1=sl1,
                atr=atr_v, close=cl,
            )
    return result
