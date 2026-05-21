"""
QF×JP Crypto Bot — BingX + Telegram
Señales propias desde BingX (sin TradingView)
Railway deployment ready
"""
import asyncio
import logging
import os
import time
import hmac
import hashlib
import statistics
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
log = logging.getLogger("qfjp_bot")

# ── Config
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_API_SECRET = os.environ["BINGX_API_SECRET"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "qfjp_secret_2025")
TRADE_SIZE_USDT  = float(os.environ.get("TRADE_SIZE_USDT", "10"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES", "2"))
SL_PCT           = float(os.environ.get("SL_PCT", "1.5"))
TP_PCT           = float(os.environ.get("TP_PCT", "3.0"))
MIN_SIGNAL_LEVEL = os.environ.get("MIN_SIGNAL_LEVEL", "LONG_FUEL")

# Símbolos a escanear (añade o quita según prefieras)
SYMBOLS = os.environ.get(
    "SYMBOLS",
    "BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT"
).split(",")

# Intervalo de escaneo en segundos (3 minutos = 180s como el Pine)
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "180"))

# Timeframe para velas (3m)
KLINE_INTERVAL = os.environ.get("KLINE_INTERVAL", "3m")
# HTF para tendencia (15m)
HTF_INTERVAL = os.environ.get("HTF_INTERVAL", "15m")

BINGX_BASE = "https://open-api.bingx.com"

SIGNAL_RANK = {
    "HUNT_LONG": 1,  "HUNT_SHORT": 1,
    "LONG_FUEL": 2,  "SHORT_FUEL": 2,
    "LONG_SUP":  3,  "SHORT_SUP":  3,
    "LONG_SUP_V3": 4, "SHORT_SUP_V3": 4,
}
MIN_RANK = SIGNAL_RANK.get(MIN_SIGNAL_LEVEL, 2)

open_trades: dict[str, dict] = {}

# ══════════════════════════════════════════════════
#  BINGX CLIENT
# ══════════════════════════════════════════════════
def _sign(params: dict, secret: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def _headers() -> dict:
    return {"X-BX-APIKEY": BINGX_API_KEY, "Content-Type": "application/json"}

async def bingx_get(path: str, params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params, BINGX_API_SECRET)
    async with aiohttp.ClientSession() as s:
        async with s.get(BINGX_BASE + path, params=params, headers=_headers()) as r:
            data = await r.json()
            if data.get("code", 0) != 0:
                raise Exception(f"BingX GET error: {data}")
            return data

async def bingx_post(path: str, body: dict) -> dict:
    body["timestamp"] = int(time.time() * 1000)
    body["signature"] = _sign(body, BINGX_API_SECRET)
    async with aiohttp.ClientSession() as s:
        async with s.post(BINGX_BASE + path, json=body, headers=_headers()) as r:
            data = await r.json()
            if data.get("code", 0) != 0:
                raise Exception(f"BingX POST error: {data}")
            return data

async def get_klines(symbol: str, interval: str = "3m", limit: int = 100) -> list[dict]:
    """
    Obtiene velas de BingX.
    Devuelve lista de dicts: open, high, low, close, volume (floats).
    """
    data = await bingx_get("/openApi/swap/v3/quote/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
    })
    candles = []
    for c in data.get("data", []):
        candles.append({
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
        })
    return candles  # más antiguo primero

async def get_price(symbol: str) -> float:
    data = await bingx_get("/openApi/swap/v2/quote/price", {"symbol": symbol})
    return float(data["data"]["price"])

async def get_balance() -> float:
    data = await bingx_get("/openApi/swap/v2/user/balance", {})
    for item in data["data"]["balance"]:
        if item["asset"] == "USDT":
            return float(item["availableMargin"])
    return 0.0

async def get_open_position(symbol: str) -> Optional[dict]:
    try:
        data = await bingx_get("/openApi/swap/v2/user/positions", {"symbol": symbol})
        for p in data.get("data", []):
            if float(p.get("positionAmt", 0)) != 0:
                return p
        return None
    except Exception as e:
        log.error(f"Error posición {symbol}: {e}")
        return None

async def place_order(symbol: str, side: str, quantity: float,
                      sl_price: float, tp_price: float) -> dict:
    order = await bingx_post("/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type": "MARKET",
        "quantity": str(quantity),
        "stopLossPrice": str(round(sl_price, 4)),
        "takeProfitPrice": str(round(tp_price, 4)),
    })
    log.info(f"Orden: {side} {symbol} qty={quantity} SL={sl_price} TP={tp_price}")
    return order

async def close_position(symbol: str, side: str, quantity: float) -> dict:
    close_side = "SELL" if side == "BUY" else "BUY"
    pos_side   = "LONG" if side == "BUY" else "SHORT"
    return await bingx_post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": close_side, "positionSide": pos_side,
        "type": "MARKET", "quantity": str(quantity), "reduceOnly": "true",
    })

# ══════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════
async def tg_send(msg: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg, "parse_mode": "HTML"
            })
    except Exception as e:
        log.error(f"Telegram error: {e}")

def fmt_signal_msg(signal: str, symbol: str, price: float,
                   sl: float, tp: float, qty: float) -> str:
    emoji = "🟢" if "LONG" in signal else "🔴"
    stars = "★" * SIGNAL_RANK.get(signal, 1)
    return (
        f"{emoji} <b>{signal}</b> {stars}\n"
        f"📊 <b>{symbol}</b> @ <code>{price}</code>\n"
        f"📐 Qty: <code>{qty}</code> | Capital: <code>{round(qty*price,2)} USDT</code>\n"
        f"🛑 SL: <code>{sl}</code>  🎯 TP: <code>{tp}</code>\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )

def fmt_close_msg(symbol: str, pnl_pct: float, reason: str) -> str:
    emoji = "✅" if pnl_pct > 0 else "❌"
    return f"{emoji} <b>CERRADO</b> {symbol}\nPnL: <code>{pnl_pct:+.2f}%</code>\nRazón: {reason}"

# ══════════════════════════════════════════════════
#  INDICADORES (port del Pine Script)
# ══════════════════════════════════════════════════
def _sma(data: list[float], period: int) -> list[float]:
    result = [float("nan")] * len(data)
    for i in range(period - 1, len(data)):
        result[i] = sum(data[i - period + 1: i + 1]) / period
    return result

def _ema(data: list[float], period: int) -> list[float]:
    result = [float("nan")] * len(data)
    k = 2 / (period + 1)
    for i in range(len(data)):
        if i == 0 or (i > 0 and result[i-1] != result[i-1]):  # nan check
            result[i] = data[i]
        else:
            result[i] = data[i] * k + result[i-1] * (1 - k)
    return result

def _stdev(data: list[float], period: int) -> list[float]:
    result = [float("nan")] * len(data)
    for i in range(period - 1, len(data)):
        window = data[i - period + 1: i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        result[i] = variance ** 0.5
    return result

def _atr(highs, lows, closes, period: int) -> list[float]:
    trs = [float("nan")]
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return _sma(trs, period)

def _roc(data: list[float], period: int) -> list[float]:
    result = [float("nan")] * len(data)
    for i in range(period, len(data)):
        if data[i - period] != 0:
            result[i] = (data[i] - data[i - period]) / data[i - period] * 100
    return result

def _obv(closes: list[float], volumes: list[float]) -> list[float]:
    result = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            result.append(result[-1] + volumes[i])
        elif closes[i] < closes[i-1]:
            result.append(result[-1] - volumes[i])
        else:
            result.append(result[-1])
    return result

def _correlation(x: list[float], y: list[float], period: int, idx: int) -> float:
    if idx < period - 1:
        return 0.0
    xs = x[idx - period + 1: idx + 1]
    ys = y[idx - period + 1: idx + 1]
    if len(xs) < 2 or len(ys) < 2:
        return 0.0
    try:
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(xs, ys))
        den = ((sum((xi - mx)**2 for xi in xs) * sum((yi - my)**2 for yi in ys)) ** 0.5)
        return num / den if den != 0 else 0.0
    except Exception:
        return 0.0

def _zscore(data: list[float], period: int, idx: int) -> float:
    if idx < period - 1:
        return 0.0
    window = data[idx - period + 1: idx + 1]
    m = sum(window) / len(window)
    s = (sum((x - m)**2 for x in window) / len(window)) ** 0.5
    return (data[idx] - m) / s if s > 0 else 0.0

def _tanh(x: float) -> float:
    import math
    v = max(min(2.0 * x, 20.0), -20.0)
    e2x = math.exp(v)
    return (e2x - 1.0) / (e2x + 1.0)

def _winsor(z: float, cap: float = 2.5) -> float:
    return max(min(z, cap), -cap)

def _highest(data: list[float], period: int, idx: int) -> float:
    start = max(0, idx - period + 1)
    window = [x for x in data[start:idx+1] if x == x]  # skip nan
    return max(window) if window else float("nan")

def _lowest(data: list[float], period: int, idx: int) -> float:
    start = max(0, idx - period + 1)
    window = [x for x in data[start:idx+1] if x == x]
    return min(window) if window else float("nan")

def _pivot_high(highs: list[float], left: int, right: int, idx: int) -> Optional[float]:
    """Pivot high: el valor en idx-right es el máximo en ±left/right."""
    pivot_idx = idx - right
    if pivot_idx < left:
        return None
    candidate = highs[pivot_idx]
    for i in range(pivot_idx - left, pivot_idx + right + 1):
        if i != pivot_idx and highs[i] >= candidate:
            return None
    return candidate

def _pivot_low(lows: list[float], left: int, right: int, idx: int) -> Optional[float]:
    pivot_idx = idx - right
    if pivot_idx < left:
        return None
    candidate = lows[pivot_idx]
    for i in range(pivot_idx - left, pivot_idx + right + 1):
        if i != pivot_idx and lows[i] <= candidate:
            return None
    return candidate

# ─────────────────────────────────────────────────
#  MOTOR DE SEÑALES (lógica completa del Pine V3)
# ─────────────────────────────────────────────────
def compute_signal(candles: list[dict], htf_candles: list[dict]) -> Optional[str]:
    """
    Reproduce la lógica QF×JP V3 en Python.
    Devuelve el nombre de la señal o None.
    """
    if len(candles) < 80 or len(htf_candles) < 25:
        return None

    # Arrays base
    o = [c["open"]   for c in candles]
    h = [c["high"]   for c in candles]
    l = [c["low"]    for c in candles]
    c = [c["close"]  for c in candles]
    v = [c["volume"] for c in candles]
    n = len(c)
    i = n - 1  # índice actual (última vela completa)

    # ── Parámetros (mismos defaults que el Pine)
    i_mom = 20; i_rev = 8; i_vol = 14; i_atr_p = 10
    i_w1 = 0.40; i_w2 = 0.30; i_w3 = 0.30
    i_dlen = 40; i_dthr = 0.50
    i_dpm = 2.5; i_dpb = 20; i_spl = 5
    i_bpt = 0.18
    i_asl = 10; i_arr = 1.40; i_abr = 1.40
    i_tlb = 30; i_tll = 5; i_tlr = 3; i_tlm = 0.15
    i_pll = 5;  i_plr = 3; i_phl = 5; i_phr = 3
    i_hlc = 2;  i_hhc = 2; i_hlw = 40
    i_spoof_vol = 2.0; i_spoof_rev = 0.25; i_spoof_bars = 3
    i_blk_mult = 2.5; i_blk_bars = 5
    i_lev = 10.0; i_liq_look = 50
    i_cvd_len = 20; i_cvd_th = 1.5
    i_sh_wick = 0.60; i_sh_vol = 1.5
    i_smo = 3

    # ── ATR
    atr_arr  = _atr(h, l, c, i_atr_p)
    atr_long_arr = _atr(h, l, c, 20)
    atr  = atr_arr[i]  if atr_arr[i] == atr_arr[i]  else 0.0001
    atr_long = atr_long_arr[i] if atr_long_arr[i] == atr_long_arr[i] else 0.0001

    # ── Spread / exec
    hi_lo_r   = [math.log(h[j] / l[j]) if l[j] > 0 else 0.0 for j in range(n)]
    spread_arr = _sma(hi_lo_r, i_spl)
    spread = spread_arr[i] * c[i] if spread_arr[i] == spread_arr[i] else 0.0
    bp_drain = (spread / c[i]) * 100 if c[i] > 0 else 999
    exec_ok = bp_drain < i_bpt

    # ── OBV
    obv_arr    = _obv(c, v)
    obv_ma_arr = _ema(obv_arr, i_vol)
    obv_std_arr = _stdev(obv_arr, i_vol)
    f_vol_val  = (obv_arr[i] - obv_ma_arr[i]) / obv_std_arr[i] if obv_std_arr[i] > 0 else 0.0

    # ── Crowding
    roc_arr    = _roc(c, i_mom)
    roc_simple = [r / 100 if r == r else 0.0 for r in roc_arr]  # normalise
    sma2_arr   = _sma(roc_simple, i_mom * 2)
    std2_arr   = _stdev(roc_simple, i_mom * 2)
    simple_z   = _winsor((roc_simple[i] - sma2_arr[i]) / std2_arr[i] if std2_arr[i] > 0 else 0.0)

    obv_ma2_arr  = _ema(obv_arr, i_vol)
    obv_std2_arr = _stdev(obv_arr, i_vol)
    f_vol_pre_arr = [(obv_arr[j] - obv_ma2_arr[j]) / obv_std2_arr[j] if obv_std2_arr[j] > 0 else 0.0 for j in range(n)]

    simple_z_list = [_winsor(_zscore(roc_simple, i_mom * 2, j)) for j in range(n)]
    pre_crowd_r = _correlation(simple_z_list, f_vol_pre_arr, i_mom * 3, i)
    is_pre_crowd = abs(pre_crowd_r) >= 0.75
    crowd_count = 0
    for j in range(max(0, i - 15), i + 1):
        cr = _correlation(simple_z_list, f_vol_pre_arr, i_mom * 3, j)
        if abs(cr) >= 0.75:
            crowd_count += 1
        else:
            crowd_count = 0
    crowd_persistent = crowd_count >= 15

    w1_dyn = max(i_w1 - 0.15, 0.10) if crowd_persistent else i_w1
    w3_dyn = min(i_w3 + 0.15, 0.60) if crowd_persistent else i_w3

    # ── L2 Factores
    roc_raw_val = (c[i] - c[i - i_mom]) / c[i - i_mom] if c[i - i_mom] != 0 else 0.0
    sma_c  = _sma(c, i_mom)
    std_c  = _stdev(c, i_mom)
    vol_norm = std_c[i] / sma_c[i] if sma_c[i] > 0 and std_c[i] == std_c[i] else 0.0
    f_mom_val = roc_raw_val / vol_norm if vol_norm != 0 else 0.0

    basis_arr = _sma(c, i_rev)
    bstd_arr  = _stdev(c, i_rev)
    f_rev_val = -(c[i] - basis_arr[i]) / bstd_arr[i] if bstd_arr[i] > 0 else 0.0

    raw_score_val = w1_dyn * f_mom_val + i_w2 * f_rev_val + w3_dyn * f_vol_val

    # For EMA of raw_score we'd need history; approximate with current value
    raw_scores = [w1_dyn * ((c[j] - c[j-i_mom])/c[j-i_mom] if j >= i_mom and c[j-i_mom] != 0 else 0.0)
                  + i_w2 * (-(c[j] - basis_arr[j])/bstd_arr[j] if bstd_arr[j] > 0 else 0.0)
                  + w3_dyn * f_vol_pre_arr[j]
                  for j in range(n)]
    comp_scores = _ema(raw_scores, i_smo)
    sc_std_arr  = _stdev(comp_scores, i_dlen)
    norm_score = _tanh(comp_scores[i] / sc_std_arr[i]) if sc_std_arr[i] > 0 else 0.0

    # ── L3 Decaimiento
    fwd_rets = [((c[j] - c[j-1]) / c[j-1]) if j > 0 and c[j-1] != 0 else 0.0 for j in range(n)]
    norm_scores_shifted = [comp_scores[j-1] / sc_std_arr[j-1] if j > 0 and sc_std_arr[j-1] > 0 else 0.0 for j in range(n)]
    ic_num_val = _correlation(norm_scores_shifted, fwd_rets, i_dlen, i)
    ic_roll_vals = [abs(_correlation(norm_scores_shifted, fwd_rets, i_dlen, j)) for j in range(n)]
    ic_roll_ema = _ema(ic_roll_vals, i_smo)
    ic_peak = _highest(ic_roll_ema, i_dlen, i)
    decay_r = ic_roll_ema[i] / ic_peak if ic_peak and ic_peak > 0 else 0.5
    sig_alive = decay_r >= i_dthr

    # ── L4 Dark Pool
    vol_base_arr = _sma(v, i_dpb)
    vol_base = vol_base_arr[i] if vol_base_arr[i] == vol_base_arr[i] else 0.0
    vol_spike  = v[i] > vol_base * i_dpm
    rng_narrow = (h[i] - l[i]) < atr * 0.6
    dp_buy     = vol_spike and rng_narrow and c[i] > o[i]
    dp_sell    = vol_spike and rng_narrow and c[i] < o[i]

    # ── L6 Asimetría
    up_rng = [(h[j] - l[j]) if c[j] > o[j] else 0.0 for j in range(n)]
    dn_rng = [(h[j] - l[j]) if c[j] < o[j] else 0.0 for j in range(n)]
    avg_up = _sma(up_rng, i_asl)
    avg_dn = _sma(dn_rng, i_asl)
    rng_rb = avg_up[i] / avg_dn[i] if avg_dn[i] > 0 else 1.0
    rng_rb_bear = avg_dn[i] / avg_up[i] if avg_up[i] > 0 else 1.0
    asym_bull = rng_rb >= i_arr
    asym_bear = rng_rb_bear >= i_abr

    # ── HTF tendencia (15m)
    htf_c = [x["close"] for x in htf_candles]
    htf_ema9  = _ema(htf_c, 9)
    htf_ema21 = _ema(htf_c, 21)
    htf_i = len(htf_c) - 1
    htf_bull = htf_ema9[htf_i] > htf_ema21[htf_i]
    htf_bear = htf_ema9[htf_i] < htf_ema21[htf_i]

    # ── L7 Trendline (simplificado: busca últimos 2 pivots)
    tl_break_long  = False
    tl_break_short = False
    ph_vals = []
    pl_vals = []
    for j in range(max(0, i - i_tlb - i_tll - 5), i + 1):
        ph = _pivot_high(h, i_tll, i_tlr, j)
        pl = _pivot_low(l,  i_pll, i_plr, j)
        if ph is not None:
            ph_vals.append((j - i_tlr, ph))
        if pl is not None:
            pl_vals.append((j - i_plr, pl))

    if len(ph_vals) >= 2:
        (pb2, pv2), (pb1, pv1) = ph_vals[-2], ph_vals[-1]
        if pv2 > pv1 and (i - pb2) <= i_tlb:
            slope = (pv1 - pv2) / max(pb1 - pb2, 1)
            tl_dn_now = pv1 + slope * (i - pb1)
            tl_dn_prev = pv1 + slope * (i - 1 - pb1)
            tl_break_long = c[i] > tl_dn_now + atr * i_tlm and c[i-1] <= tl_dn_prev + atr * i_tlm

    if len(pl_vals) >= 2:
        (pb2, pv2), (pb1, pv1) = pl_vals[-2], pl_vals[-1]
        if pv2 < pv1 and (i - pb2) <= i_tlb:
            slope = (pv1 - pv2) / max(pb1 - pb2, 1)
            tl_up_now = pv1 + slope * (i - pb1)
            tl_up_prev = pv1 + slope * (i - 1 - pb1)
            tl_break_short = c[i] < tl_up_now - atr * i_tlm and c[i-1] >= tl_up_prev - atr * i_tlm

    # ── L8 Swing (Higher Lows / Lower Highs)
    recent_pl = [pv for (pb, pv) in pl_vals if (i - pb) <= i_hlw]
    recent_ph = [pv for (pb, pv) in ph_vals if (i - pb) <= i_hlw]
    hl_count = sum(1 for j in range(1, len(recent_pl)) if recent_pl[j] > recent_pl[j-1])
    lh_count = sum(1 for j in range(1, len(recent_ph)) if recent_ph[j] < recent_ph[j-1])
    sell_exhausted = hl_count >= i_hlc
    buy_exhausted  = lh_count >= i_hhc

    # ── Anti-spoof
    bar_range = h[i] - l[i]
    spoof_vol_ok = v[i-1] > vol_base * i_spoof_vol if i > 0 else False
    spoof_ret_ok = abs(c[i] - c[i-2]) < atr * i_spoof_rev if i > 1 else False
    bull_trap = (c[i-1] > o[i-1] and (h[i-1]-l[i-1]) > atr*1.5 and c[i] < o[i] and v[i-1] > vol_base*1.8) if i > 0 else False
    bear_trap = (c[i-1] < o[i-1] and (h[i-1]-l[i-1]) > atr*1.5 and c[i] > o[i] and v[i-1] > vol_base*1.8) if i > 0 else False
    in_spoof_zone = (spoof_vol_ok and spoof_ret_ok) or bull_trap or bear_trap

    # ── Blackout (ATR spike)
    atr_spike = atr > atr_long * i_blk_mult
    in_blackout = atr_spike

    # ── Sesión
    current_hour = datetime.now(timezone.utc).hour
    sess_eu     = 7 <= current_hour < 15
    sess_ny     = 13 <= current_hour < 21
    sess_crypto = current_hour >= 21 or current_hour < 1
    in_time_window = sess_eu or sess_ny or sess_crypto

    # ── Liquidaciones
    highest_h = _highest(h, i_liq_look, i)
    lowest_l  = _lowest(l,  i_liq_look, i)
    liq_long_zone  = highest_h * (1.0 - 1.0 / i_lev) if highest_h == highest_h else 0
    liq_short_zone = lowest_l  * (1.0 + 1.0 / i_lev) if lowest_l == lowest_l else 0
    near_liq_long  = abs(c[i] - liq_long_zone)  < atr * 0.5
    near_liq_short = abs(c[i] - liq_short_zone) < atr * 0.5

    # ── CVD
    bar_pos_arr = [(c[j] - l[j]) / (h[j] - l[j]) if (h[j] - l[j]) > 0 else 0.5 for j in range(n)]
    bar_delta_arr = [v[j] * (2.0 * bar_pos_arr[j] - 1.0) for j in range(n)]
    cvd_arr = _sma(bar_delta_arr, i_cvd_len)
    price_roc_arr = _roc(c, i_cvd_len)
    cvd_roc_arr   = _roc(cvd_arr, i_cvd_len)
    price_roc_val = price_roc_arr[i] if price_roc_arr[i] == price_roc_arr[i] else 0.0
    cvd_roc_val   = cvd_roc_arr[i]   if cvd_roc_arr[i]   == cvd_roc_arr[i]   else 0.0
    cvd_div_bull  = price_roc_val < 0 and cvd_roc_val >  i_cvd_th
    cvd_div_bear  = price_roc_val > 0 and cvd_roc_val < -i_cvd_th
    cvd_z         = _winsor(_zscore(cvd_arr, i_cvd_len * 2, i))

    # ── Stop Hunt
    low_wick  = min(o[i], c[i]) - l[i]
    high_wick = h[i] - max(o[i], c[i])
    stop_hunt_dn = low_wick  > bar_range * i_sh_wick and v[i] > vol_base * i_sh_vol and c[i] > o[i]
    stop_hunt_up = high_wick > bar_range * i_sh_wick and v[i] > vol_base * i_sh_vol and c[i] < o[i]

    # ══ SEÑALES (idéntica lógica al Pine)
    base_long  = norm_score > 0.15 and sig_alive and exec_ok and htf_bull and asym_bull and sell_exhausted
    base_short = norm_score < -0.15 and sig_alive and exec_ok and htf_bear and asym_bear and buy_exhausted
    filters_ok = not in_spoof_zone and not in_blackout and in_time_window

    long_std   = base_long  and filters_ok
    short_std  = base_short and filters_ok
    long_fuel  = long_std  and tl_break_long
    short_fuel = short_std and tl_break_short
    long_sup   = long_fuel  and dp_buy
    short_sup  = short_fuel and dp_sell
    long_sup_v3  = long_sup  and cvd_z > 0 and not near_liq_short
    short_sup_v3 = short_sup and cvd_z < 0 and not near_liq_long
    sh_long_entry  = stop_hunt_dn and htf_bull and sig_alive and not in_blackout and in_time_window and not in_spoof_zone
    sh_short_entry = stop_hunt_up and htf_bear and sig_alive and not in_blackout and in_time_window and not in_spoof_zone

    # Retorna la señal de mayor rango
    if long_sup_v3:    return "LONG_SUP_V3"
    if short_sup_v3:   return "SHORT_SUP_V3"
    if long_sup:       return "LONG_SUP"
    if short_sup:      return "SHORT_SUP"
    if long_fuel:      return "LONG_FUEL"
    if short_fuel:     return "SHORT_FUEL"
    if sh_long_entry:  return "HUNT_LONG"
    if sh_short_entry: return "HUNT_SHORT"
    return None

import math  # necesario para compute_signal

# ══════════════════════════════════════════════════
#  TRADING LOGIC
# ══════════════════════════════════════════════════
def calc_quantity(price: float, usdt: float) -> float:
    return max(round(usdt / price, 4), 0.001)

async def handle_signal(signal: str, symbol: str) -> None:
    if SIGNAL_RANK.get(signal, 0) < MIN_RANK:
        log.info(f"Nivel insuficiente: {signal}")
        return
    if len(open_trades) >= MAX_OPEN_TRADES:
        log.info(f"Límite de trades ({MAX_OPEN_TRADES}) alcanzado")
        return

    is_long  = "LONG"  in signal
    is_short = "SHORT" in signal

    if symbol in open_trades:
        existing = open_trades[symbol]
        if (is_long and existing["side"] == "BUY") or (is_short and existing["side"] == "SELL"):
            log.info(f"Ya hay posición en {symbol}")
            return

    try:
        price   = await get_price(symbol)
        balance = await get_balance()
        usdt_to_use = min(TRADE_SIZE_USDT, balance * 0.20)
        if usdt_to_use < 5:
            await tg_send(f"⚠️ Balance insuficiente: <code>{balance:.2f} USDT</code>")
            return

        qty  = calc_quantity(price, usdt_to_use)
        side = "BUY" if is_long else "SELL"
        sl = round(price * (1 - SL_PCT/100) if is_long else price * (1 + SL_PCT/100), 4)
        tp = round(price * (1 + TP_PCT/100) if is_long else price * (1 - TP_PCT/100), 4)

        order    = await place_order(symbol, side, qty, sl, tp)
        order_id = order.get("data", {}).get("orderId", "—")
        open_trades[symbol] = {
            "side": side, "entry": price, "qty": qty,
            "sl": sl, "tp": tp, "signal": signal,
            "order_id": order_id, "time": time.time(),
        }
        await tg_send(fmt_signal_msg(signal, symbol, price, sl, tp, qty))
        log.info(f"Trade: {side} {symbol} @ {price}")
    except Exception as e:
        log.error(f"Error trade {signal} {symbol}: {e}")
        await tg_send(f"❌ <b>ERROR</b> {signal} {symbol}\n<code>{e}</code>")

# ══════════════════════════════════════════════════
#  SCANNER — escanea todos los símbolos cada SCAN_INTERVAL
# ══════════════════════════════════════════════════
async def scanner_loop() -> None:
    await asyncio.sleep(10)  # espera arranque
    await tg_send(
        f"🔍 <b>Scanner activo</b>\n"
        f"Símbolos: <code>{', '.join(SYMBOLS)}</code>\n"
        f"Intervalo: <code>{KLINE_INTERVAL}</code> | cada <code>{SCAN_INTERVAL}s</code>\n"
        f"Min señal: <code>{MIN_SIGNAL_LEVEL}</code>"
    )
    while True:
        for symbol in SYMBOLS:
            try:
                candles     = await get_klines(symbol, KLINE_INTERVAL, limit=120)
                htf_candles = await get_klines(symbol, HTF_INTERVAL,   limit=40)
                signal = compute_signal(candles, htf_candles)
                if signal:
                    log.info(f"Señal detectada: {signal} en {symbol}")
                    await handle_signal(signal, symbol)
                await asyncio.sleep(0.5)  # pausa entre símbolos para no saturar la API
            except Exception as e:
                log.error(f"Scanner error {symbol}: {e}")
                await asyncio.sleep(2)
        await asyncio.sleep(SCAN_INTERVAL)

# ══════════════════════════════════════════════════
#  MONITOR DE POSICIONES
# ══════════════════════════════════════════════════
async def position_monitor() -> None:
    while True:
        await asyncio.sleep(30)
        for symbol, trade in list(open_trades.items()):
            try:
                live_pos = await get_open_position(symbol)
                if live_pos is None:
                    price   = await get_price(symbol)
                    entry   = trade["entry"]
                    side    = trade["side"]
                    pnl_pct = ((price - entry)/entry*100) if side == "BUY" else ((entry - price)/entry*100)
                    await tg_send(fmt_close_msg(symbol, pnl_pct, "SL/TP ejecutado en servidor"))
                    open_trades.pop(symbol, None)
                    continue

                price   = await get_price(symbol)
                entry   = trade["entry"]
                side    = trade["side"]
                pnl_pct = ((price - entry)/entry*100) if side == "BUY" else ((entry - price)/entry*100)
                reason: Optional[str] = None

                if side == "BUY":
                    if price <= trade["sl"]: reason = "SL alcanzado"
                    elif price >= trade["tp"]: reason = "TP alcanzado"
                else:
                    if price >= trade["sl"]: reason = "SL alcanzado"
                    elif price <= trade["tp"]: reason = "TP alcanzado"

                if time.time() - trade["time"] > 10_800:
                    reason = "Timeout 3h"

                if reason:
                    await close_position(symbol, side, trade["qty"])
                    await tg_send(fmt_close_msg(symbol, pnl_pct, reason))
                    open_trades.pop(symbol, None)
            except Exception as e:
                log.error(f"Monitor error {symbol}: {e}")

# ══════════════════════════════════════════════════
#  FASTAPI
# ══════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(scanner_loop())
    asyncio.create_task(position_monitor())
    await tg_send("🤖 <b>QF×JP Bot iniciado</b>\nModo: <b>Scanner BingX directo</b> (sin TradingView)")
    log.info("Bot iniciado correctamente")
    yield
    log.info("Bot detenido")

app = FastAPI(title="QF×JP Bot", lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook manual opcional (TradingView, pruebas, etc.)."""
    secret = request.headers.get("X-Webhook-Secret", "")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    log.info(f"Webhook recibido: {data}")
    signal = data.get("signal", "").strip()
    symbol = data.get("symbol", "").strip().upper().replace("USDT", "-USDT")
    if signal and symbol:
        asyncio.create_task(handle_signal(signal, symbol))
    return JSONResponse({"status": "ok"})

@app.get("/health")
async def health():
    return {
        "status": "running",
        "mode": "bingx_scanner",
        "symbols": SYMBOLS,
        "scan_interval_s": SCAN_INTERVAL,
        "open_trades": len(open_trades),
        "trades": list(open_trades.keys()),
    }

@app.get("/trades")
async def trades():
    return {"open_trades": open_trades}

@app.get("/scan/{symbol}")
async def scan_now(symbol: str):
    """Fuerza un escaneo inmediato del símbolo."""
    sym = symbol.upper()
    if "-" not in sym:
        sym = sym.replace("USDT", "") + "-USDT"
    try:
        candles     = await get_klines(sym, KLINE_INTERVAL, limit=120)
        htf_candles = await get_klines(sym, HTF_INTERVAL,   limit=40)
        signal = compute_signal(candles, htf_candles)
        return {"symbol": sym, "signal": signal or "none"}
    except Exception as e:
        return {"symbol": sym, "error": str(e)}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
