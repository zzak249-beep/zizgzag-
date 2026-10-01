"""Motor FED + WER v7 portado de Pine (mismo orden de evaluación), con mejoras.

Recibe velas CERRADAS y devuelve las señales de cada barra. Se usa igual en vivo
(se mira solo la última barra) y en backtest (todas). Reproducir la ventana
completa en cada escaneo hace el motor determinista y a prueba de reinicios.
"""
import math
from bisect import bisect_right

NAN = float("nan")
DAY_MS = 86_400_000


def _nan(x):
    return x != x


# ───────────── indicadores (equivalentes a ta.*) ─────────────
def sma(x, n):
    out = [NAN] * len(x)
    s, cnt = 0.0, 0
    for i, v in enumerate(x):
        s += v
        cnt += 1
        if cnt > n:
            s -= x[i - n]
            cnt = n
        if cnt == n:
            out[i] = s / n
    return out


def rma(x, n):
    out = [NAN] * len(x)
    prev = NAN
    for i in range(len(x)):
        if i < n - 1:
            continue
        if _nan(prev):
            prev = sum(x[i - n + 1:i + 1]) / n
        else:
            prev = (prev * (n - 1) + x[i]) / n
        out[i] = prev
    return out


def ema(x, n):
    out = [NAN] * len(x)
    a = 2.0 / (n + 1)
    prev = NAN
    for i in range(len(x)):
        if i < n - 1:
            continue
        prev = sum(x[i - n + 1:i + 1]) / n if _nan(prev) else a * x[i] + (1 - a) * prev
        out[i] = prev
    return out


def atr(h, l, c, n):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    return rma(tr, n)


def rsi(c, n=14):
    g = [0.0] + [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))]
    d = [0.0] + [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))]
    ag, ad = rma(g, n), rma(d, n)
    out = []
    for a, b in zip(ag, ad):
        if _nan(a) or _nan(b):
            out.append(NAN)
        else:
            out.append(100.0 if b == 0 else 100 - 100 / (1 + a / b))
    return out


def daily_open(t, o):
    """Open del día UTC. Solo válido si existe la vela de las 00:00 (si no, NaN)."""
    out, seen = [NAN] * len(t), {}
    for i, ti in enumerate(t):
        d = ti // DAY_MS
        if d not in seen:
            seen[d] = o[i] if ti % DAY_MS == 0 else NAN
        out[i] = seen[d]
    return out


def btc_context(btc):
    """btc = dict(t,o,c) → (close_por_tiempo, open_diario_por_día)."""
    do = daily_open(btc["t"], btc["o"])
    close_by_t = dict(zip(btc["t"], btc["c"]))
    dopen_by_day = {}
    for ti, v in zip(btc["t"], do):
        dopen_by_day.setdefault(ti // DAY_MS, v)
    return close_by_t, dopen_by_day


def htf_context(htf, cfg):
    """HTF sin repintado: en cada vela HTF se guarda el valor de la HTF anterior ([1])."""
    e = ema(htf["c"], cfg.CTX_EMA)
    hi = [max(htf["h"][max(0, j - cfg.PD_LEN + 1):j + 1]) if j >= cfg.PD_LEN - 1 else NAN for j in range(len(htf["c"]))]
    lo = [min(htf["l"][max(0, j - cfg.PD_LEN + 1):j + 1]) if j >= cfg.PD_LEN - 1 else NAN for j in range(len(htf["c"]))]
    sh = lambda a: [NAN] + a[:-1]
    return {"t": htf["t"], "ema": sh(e), "hi": sh(hi), "lo": sh(lo)}


def _session_ok(t_ms, sess):
    a, b = sess.split("-")
    ma = int(a[:2]) * 60 + int(a[2:])
    mb = int(b[:2]) * 60 + int(b[2:])
    m = (t_ms // 60_000) % 1440
    return ma <= m < mb if ma <= mb else (m >= ma or m < mb)


# ───────────── motor ─────────────
def run(b, btc_ctx, cfg, htf=None):
    """b = dict(t,o,h,l,c,v) velas cerradas ascendentes. Devuelve lista de señales."""
    t, o, h, l, c, v = b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]
    n = len(c)
    btc_close, btc_dopen = btc_ctx
    L = cfg.EXT_LOOKBACK

    dop = daily_open(t, o)
    A = atr(h, l, c, cfg.ATR_LEN)
    vs = sma(v, 20)
    body = [max(abs(c[i] - o[i]), c[i] * 1e-5) for i in range(n)]  # ≈ mintick
    wer = [((h[i] - max(o[i], c[i])) + (min(o[i], c[i]) - l[i])) / body[i] for i in range(n)]
    wsma = sma(wer, cfg.WER_LOOKBACK)
    rsis = rsi(c) if cfg.USE_RSI_FILTER else None

    # VWAP diario anclado (para la prima)
    vwap = [NAN] * n
    cpv = cv = 0.0
    for i in range(n):
        if i == 0 or t[i] // DAY_MS != t[i - 1] // DAY_MS:
            cpv = cv = 0.0
        tp = (h[i] + l[i] + c[i]) / 3
        cpv += tp * v[i]
        cv += v[i]
        vwap[i] = cpv / cv if cv > 0 else NAN

    s_lvl = s_size = s_rvol = s_atrsz = s_base = NAN
    s_bar = None
    waiting = False
    top_ext = False
    last_sig = -1000
    pend, pend_bar = 0, None
    sigs = []

    for i in range(max(L, 21, cfg.ATR_LEN), n):
        a = A[i]
        if _nan(a) or _nan(dop[i]) or _nan(vs[i]) or vs[i] == 0:
            continue
        bc = btc_close.get(t[i], NAN)
        bo = btc_dopen.get(t[i] // DAY_MS, NAN)
        if _nan(bc) or _nan(bo):
            continue
        coin_pct = (c[i] - dop[i]) / dop[i] * 100
        btc_pct = (bc - bo) / bo * 100
        rs = coin_pct - btc_pct
        is_top = coin_pct >= cfg.MIN_DAILY_MOVE and rs >= cfg.RS_THRESHOLD
        is_bot = coin_pct <= -cfg.MIN_DAILY_MOVE and rs <= -cfg.RS_THRESHOLD
        rv = v[i] / vs[i]
        werok = (not _nan(wsma[i])) and wer[i] >= cfg.WER_THRESHOLD and (wer[i] - wsma[i]) > cfg.WER_ACCEL_MIN

        hh = max(h[i - L:i])
        ll = min(l[i - L:i])
        new_hi = h[i] > hh and is_top and (h[i] - hh) >= a * cfg.MIN_EXT_ATR and rv >= cfg.VOL_MULT
        new_lo = l[i] < ll and is_bot and (ll - l[i]) >= a * cfg.MIN_EXT_ATR and rv >= cfg.VOL_MULT

        if new_hi:
            s_lvl, s_size, s_bar, s_rvol, s_atrsz, s_base = h[i], h[i] - hh, i, rv, (h[i] - hh) / a, hh
            waiting, top_ext, pend = True, True, 0
        if new_lo:
            s_lvl, s_size, s_bar, s_rvol, s_atrsz, s_base = l[i], ll - l[i], i, rv, (ll - l[i]) / a, ll
            waiting, top_ext, pend = True, False, 0

        bars_since = i - s_bar if s_bar is not None else 10 ** 9
        failed = False
        if waiting and 1 <= bars_since <= cfg.FAIL_BARS:
            if top_ext:
                retr = (s_lvl - l[i]) / s_size * 100 if s_size > 0 else 0
                inside = (not cfg.REQUIRE_CLOSE_INSIDE) or c[i] < s_base
                if h[i] < s_lvl and retr >= cfg.MIN_RETRACE_PCT and inside:
                    failed, waiting = True, False
            else:
                retr = (h[i] - s_lvl) / s_size * 100 if s_size > 0 else 0
                inside = (not cfg.REQUIRE_CLOSE_INSIDE) or c[i] > s_base
                if l[i] > s_lvl and retr >= cfg.MIN_RETRACE_PCT and inside:
                    failed, waiting = True, False
        if waiting and bars_since > cfg.FAIL_BARS:
            waiting = False

        if failed and not werok and cfg.WER_WINDOW > 0:
            pend, pend_bar = (1 if top_ext else -1), i
        pend_bars = i - pend_bar if pend_bar is not None else 10 ** 9
        if pend != 0 and pend_bars > cfg.WER_WINDOW:
            pend = 0
        pend_top = pend == 1 and 1 <= pend_bars <= cfg.WER_WINDOW and is_top and h[i] < s_lvl and werok
        pend_bot = pend == -1 and 1 <= pend_bars <= cfg.WER_WINDOW and is_bot and l[i] > s_lvl and werok

        can = (i - last_sig) > cfg.COOLDOWN_BARS
        fade_top = can and ((failed and top_ext and is_top and werok) or pend_top)
        fade_bot = can and ((failed and not top_ext and is_bot and werok) or pend_bot)
        if not (fade_top or fade_bot):
            continue
        last_sig, pend = i, 0
        short = fade_top

        # ── score / delta ──
        buy_v = v[i] * (c[i] - l[i]) / (h[i] - l[i]) if h[i] > l[i] else v[i] * 0.5
        dlt = (2 * buy_v - v[i]) / v[i] * 100 if v[i] > 0 else NAN
        dl_fav = -dlt if top_ext else dlt
        retr_now = ((s_lvl - l[i]) if top_ext else (h[i] - s_lvl)) / s_size * 100 if s_size > 0 else NAN
        sc = (min(s_rvol / 3, 1) * 20
              + (0 if _nan(retr_now) else min(max((retr_now - cfg.MIN_RETRACE_PCT) / max(100 - cfg.MIN_RETRACE_PCT, 1), 0), 1) * 15)
              + min(wer[i] / 4, 1) * 20
              + (7 if _nan(dl_fav) else min(max(dl_fav / 60, 0), 1) * 15)
              + min(abs(rs) / (cfg.RS_THRESHOLD * 2.5), 1) * 15
              + min(s_atrsz / (cfg.MIN_EXT_ATR * 3), 1) * 15)
        prem = (c[i] - vwap[i]) / vwap[i] * 100 if not _nan(vwap[i]) else NAN
        roc = (c[i] - c[i - cfg.ROC_LEN]) / c[i - cfg.ROC_LEN] * 100 if i >= cfg.ROC_LEN else NAN

        # ── filtros ──
        why = []
        if short and not cfg.ALLOW_SHORT: why.append("lado")
        if not short and not cfg.ALLOW_LONG: why.append("lado")
        if v[i] * c[i] < cfg.MIN_BAR_VOL_USDT: why.append("liquidez")
        if cfg.MAX_ATR_PCT > 0 and a / c[i] * 100 > cfg.MAX_ATR_PCT: why.append("ATR%")
        if cfg.MIN_RISK_PCT > 0 and a * cfg.SL_ATR < c[i] * cfg.MIN_RISK_PCT / 100: why.append("riesgo<min")
        if cfg.MIN_SCORE and sc < cfg.MIN_SCORE: why.append(f"score {sc:.0f}")
        if cfg.MIN_DELTA and not _nan(dl_fav) and dl_fav < cfg.MIN_DELTA: why.append("delta")
        if cfg.MIN_FAIL_RVOL and rv < cfg.MIN_FAIL_RVOL: why.append("rvol fallo")
        if cfg.USE_SESSION and not _session_ok(t[i], cfg.SESSION_UTC): why.append("sesión")
        if cfg.USE_RSI_FILTER:
            win = rsis[max(0, i - cfg.FAIL_BARS - 1):i + 1]
            if short and not max(win) >= cfg.RSI_HI: why.append("RSI")
            if not short and not min(win) <= cfg.RSI_LO: why.append("RSI")
        if cfg.USE_PREMIUM:
            if _nan(prem) or (short and prem < cfg.PREMIUM_PCT) or (not short and prem > -cfg.PREMIUM_PCT):
                why.append("prima")
        if cfg.USE_ROC:
            if _nan(roc) or (short and roc >= 0) or (not short and roc <= 0):
                why.append("ROC")
        if cfg.need_htf:
            hx = None
            if htf:
                j = bisect_right(htf["t"], t[i]) - 1
                if j >= 0:
                    hx = (htf["ema"][j], htf["hi"][j], htf["lo"][j])
            if hx is None:
                why.append("HTF n/d")
            else:
                he, phi, plo = hx
                if cfg.MIN_EXT_EMA_ATR > 0:
                    dist = (c[i] - he) / a if not _nan(he) else NAN
                    if _nan(dist) or (short and dist < cfg.MIN_EXT_EMA_ATR) or (not short and dist > -cfg.MIN_EXT_EMA_ATR):
                        why.append("EMA HTF")
                if cfg.USE_PD:
                    pos = (c[i] - plo) / (phi - plo) * 100 if (not _nan(phi) and phi > plo) else NAN
                    if _nan(pos) or (short and pos < cfg.PD_MIN_PCT) or (not short and pos > 100 - cfg.PD_MIN_PCT):
                        why.append("P/D")

        sigs.append({
            "i": i, "t": t[i], "side": "SHORT" if short else "LONG", "ok": not why, "why": why,
            "entry": c[i], "atr": a, "score": round(sc, 1), "coin_pct": coin_pct, "rs": rs,
            "wer": wer[i], "retrace": retr_now, "ext_rvol": s_rvol, "ext_level": s_lvl,
            "delta": dl_fav, "premium": prem, "roc": roc,
        })
    return sigs
