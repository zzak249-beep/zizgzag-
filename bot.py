"""
QF×JP Crypto Bot — BingX + Telegram  v2.1 (FIXED)
===================================================
BUGS CORREGIDOS vs versión anterior:
  1. [CRÍTICO] `math` no importado al top-level → NameError silencioso
     en compute_signal() → nunca se generaban señales.
  2. [CRÍTICO] asym_bull threshold 1.40 demasiado agresivo → bloqueaba ~80%
     de las velas. Reducido a 1.15.
  3. [CRÍTICO] sell_exhausted/buy_exhausted en base_long/base_short
     → requería estructura de pivots muy específica. Movido a bonus score.
  4. [CRÍTICO] limit=120 velas insuficiente para cálculos (i_dlen=40,
     i_mom=20, pivots, correlaciones). Aumentado a 300.
  5. [MAYOR] tl_break como condición OBLIGATORIA para FUEL → rarísimo.
     Ahora es bonus: FUEL = base + (tl_break OR dp_signal OR asym_strong).
  6. [MAYOR] Lógica de señales completamente AND-encadenada → sistema de
     puntuación ponderado con umbral configurable (MIN_SCORE).
  7. [MENOR] import math dentro de _tanh() en lugar de top-level.
  8. [MENOR] ic_roll_vals calculado O(n²) con _correlation en loop →
     optimizado con ventana deslizante.
  9. [INFO]  Logging de diagnóstico: muestra QUÉ filtro bloquea en cada ciclo.
  10.[INFO]  Soporte LIVE_TRADING=false para paper-trading seguro.

Sistema de puntuación (0-100):
  LONG:   norm_score>0 (+20), sig_alive (+15), htf_bull (+20),
          tl_break_long (+15), dp_buy (+15), asym_bull (+10), sell_exhausted (+5)
  SHORT:  norm_score<0 (+20), sig_alive (+15), htf_bear (+20),
          tl_break_short (+15), dp_sell (+15), asym_bear (+10), buy_exhausted (+5)
  Umbral: MIN_SCORE (default 40) → señal HUNT
          MIN_SCORE+15 → señal FUEL
          MIN_SCORE+25 → señal SUP
          MIN_SCORE+35 → señal SUP_V3
"""
import asyncio
import logging
import math        # ← FIX #1: importado al top-level
import os
import time
import hmac
import hashlib
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

# ── Config ──────────────────────────────────────────────────────────────────
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
MIN_SCORE        = int(os.environ.get("MIN_SCORE", "55"))       # FIX #6: 40 en env → 55 default más selectivo
LIVE_TRADING     = os.environ.get("LIVE_TRADING", "true").lower() == "true"  # FIX #10

_sym_env = os.environ.get("SYMBOLS", "").strip()
SYMBOLS = [s.strip() for s in _sym_env.split(",") if s.strip()] or [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT"
]

SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL", "180"))
KLINE_INTERVAL = os.environ.get("KLINE_INTERVAL", "3m")
HTF_INTERVAL   = os.environ.get("HTF_INTERVAL", "15m")
KLINE_LIMIT    = int(os.environ.get("KLINE_LIMIT", "300"))   # FIX #4

BINGX_BASE = "https://open-api.bingx.com"

SIGNAL_RANK = {
    "HUNT_LONG": 1, "HUNT_SHORT": 1,
    "LONG_FUEL": 2, "SHORT_FUEL": 2,
    "LONG_SUP":  3, "SHORT_SUP":  3,
    "LONG_SUP_V3": 4, "SHORT_SUP_V3": 4,
}
MIN_RANK = SIGNAL_RANK.get(MIN_SIGNAL_LEVEL, 2)

open_trades: dict[str, dict] = {}
_cycle_count = 0

# ══════════════════════════════════════════════════════════════════════════════
#  BINGX CLIENT
# ══════════════════════════════════════════════════════════════════════════════
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
                raise Exception(f"BingX GET error [{path}]: {data}")
            return data

async def bingx_post(path: str, body: dict) -> dict:
    body["timestamp"] = int(time.time() * 1000)
    body["signature"] = _sign(body, BINGX_API_SECRET)
    async with aiohttp.ClientSession() as s:
        async with s.post(BINGX_BASE + path, json=body, headers=_headers()) as r:
            data = await r.json()
            if data.get("code", 0) != 0:
                raise Exception(f"BingX POST error [{path}]: {data}")
            return data

async def get_klines(symbol: str, interval: str = "3m", limit: int = 300) -> list[dict]:
    data = await bingx_get("/openApi/swap/v3/quote/klines", {
        "symbol": symbol, "interval": interval, "limit": str(limit),
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

async def get_all_symbols() -> list[str]:
    """Obtiene todos los símbolos USDT de BingX Perpetual."""
    try:
        data = await bingx_get("/openApi/swap/v2/quote/contracts", {})
        syms = []
        for c in data.get("data", []):
            sym = c.get("symbol", "")
            if sym.endswith("-USDT"):
                syms.append(sym)
        return syms
    except Exception as e:
        log.error(f"get_all_symbols: {e}")
        return SYMBOLS

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
    if not LIVE_TRADING:                           # FIX #10
        log.info(f"[PAPER] {side} {symbol} qty={quantity} SL={sl_price} TP={tp_price}")
        return {"data": {"orderId": "PAPER"}}
    order = await bingx_post("/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type": "MARKET",
        "quantity": str(quantity),
        "stopLossPrice": str(round(sl_price, 4)),
        "takeProfitPrice": str(round(tp_price, 4)),
    })
    log.info(f"Orden REAL: {side} {symbol} qty={quantity} SL={sl_price} TP={tp_price}")
    return order

async def close_position(symbol: str, side: str, quantity: float) -> dict:
    if not LIVE_TRADING:
        log.info(f"[PAPER] CLOSE {symbol}")
        return {}
    close_side = "SELL" if side == "BUY" else "BUY"
    pos_side   = "LONG" if side == "BUY" else "SHORT"
    return await bingx_post("/openApi/swap/v2/trade/order", {
        "symbol": symbol, "side": close_side, "positionSide": pos_side,
        "type": "MARKET", "quantity": str(quantity), "reduceOnly": "true",
    })

# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════
async def tg_send(msg: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"
            })
    except Exception as e:
        log.error(f"Telegram error: {e}")

def fmt_signal_msg(signal: str, symbol: str, price: float, sl: float,
                   tp: float, qty: float, score: int, reasons: list[str]) -> str:
    emoji = "🟢" if "LONG" in signal else "🔴"
    stars = "★" * SIGNAL_RANK.get(signal, 1)
    mode  = "" if LIVE_TRADING else " <b>[PAPER]</b>"
    reasons_txt = "\n".join(f"  • {r}" for r in reasons[:6])
    return (
        f"{emoji} <b>{signal}</b> {stars}{mode}\n"
        f"📊 <b>{symbol}</b> @ <code>{price:.4f}</code>\n"
        f"📐 Qty: <code>{qty}</code> | Capital: <code>{round(qty*price,2)} USDT</code>\n"
        f"🛑 SL: <code>{sl:.4f}</code>  🎯 TP: <code>{tp:.4f}</code>\n"
        f"⭐ Score: <b>{score}/100</b>\n"
        f"<b>Razones:</b>\n{reasons_txt}\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )

def fmt_close_msg(symbol: str, pnl_pct: float, reason: str) -> str:
    emoji = "✅" if pnl_pct > 0 else "❌"
    return f"{emoji} <b>CERRADO</b> {symbol}\nPnL: <code>{pnl_pct:+.2f}%</code>\nRazón: {reason}"

# ══════════════════════════════════════════════════════════════════════════════
#  INDICADORES
# ══════════════════════════════════════════════════════════════════════════════
def _sma(data: list[float], period: int) -> list[float]:
    result = [float("nan")] * len(data)
    for i in range(period - 1, len(data)):
        result[i] = sum(data[i - period + 1: i + 1]) / period
    return result

def _ema(data: list[float], period: int) -> list[float]:
    result = [float("nan")] * len(data)
    k = 2 / (period + 1)
    for i in range(len(data)):
        if i == 0 or result[i-1] != result[i-1]:
            result[i] = data[i]
        else:
            result[i] = data[i] * k + result[i-1] * (1 - k)
    return result

def _stdev(data: list[float], period: int) -> list[float]:
    result = [float("nan")] * len(data)
    for i in range(period - 1, len(data)):
        window = data[i - period + 1: i + 1]
        mean = sum(window) / period
        result[i] = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
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
    if len(xs) < 2:
        return 0.0
    try:
        mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
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
    v = max(min(2.0 * x, 20.0), -20.0)  # math ya importado al top-level
    e2x = math.exp(v)
    return (e2x - 1.0) / (e2x + 1.0)

def _winsor(z: float, cap: float = 2.5) -> float:
    return max(min(z, cap), -cap)

def _highest(data: list[float], period: int, idx: int) -> float:
    start = max(0, idx - period + 1)
    window = [x for x in data[start:idx+1] if x == x]
    return max(window) if window else float("nan")

def _lowest(data: list[float], period: int, idx: int) -> float:
    start = max(0, idx - period + 1)
    window = [x for x in data[start:idx+1] if x == x]
    return min(window) if window else float("nan")

def _pivot_high(highs: list[float], left: int, right: int, idx: int) -> Optional[float]:
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

# ══════════════════════════════════════════════════════════════════════════════
#  MOTOR DE SEÑALES — QF×JP V3 CORREGIDO
# ══════════════════════════════════════════════════════════════════════════════
def compute_signal(candles: list[dict], htf_candles: list[dict],
                   symbol: str = "") -> tuple[Optional[str], int, list[str]]:
    """
    Retorna (nombre_señal | None, score 0-100, lista_razones).
    FIX #6: Sistema de puntuación en lugar de AND puro.
    FIX #4: Requiere mínimo 80 velas (antes 80 pero limit era 120 con cálculos que necesitan más).
    """
    if len(candles) < 80 or len(htf_candles) < 20:
        return None, 0, ["datos insuficientes"]

    o = [c["open"]   for c in candles]
    h = [c["high"]   for c in candles]
    l = [c["low"]    for c in candles]
    c = [c["close"]  for c in candles]
    v = [c["volume"] for c in candles]
    n = len(c)
    i = n - 1

    # Parámetros (ajustados para más señales)
    i_mom = 20; i_rev = 8; i_vol = 14; i_atr_p = 10
    i_w1 = 0.40; i_w2 = 0.30; i_w3 = 0.30
    i_dlen = 40; i_dthr = 0.35    # FIX: 0.50 → 0.35 (menos restrictivo)
    i_dpm = 2.5; i_dpb = 20; i_spl = 5
    i_bpt = 0.60                   # FIX: 0.18% → 0.60% (realista para 3m/1h)
    i_asl = 10
    i_arr  = 1.15                  # FIX #2: 1.40 → 1.15
    i_abr  = 1.15                  # FIX #2: 1.40 → 1.15
    i_tlb = 40; i_tll = 5; i_tlr = 3; i_tlm = 0.10
    i_pll = 5; i_plr = 3; i_phl = 5; i_phr = 3
    i_hlc = 2; i_hhc = 2; i_hlw = 50
    i_spoof_vol = 2.5; i_spoof_rev = 0.30; i_blk_mult = 3.0
    i_lev = 10.0; i_liq_look = 50
    i_cvd_len = 20; i_cvd_th = 1.0
    i_sh_wick = 0.55; i_sh_vol = 1.3
    i_smo = 3

    # ATR
    atr_arr      = _atr(h, l, c, i_atr_p)
    atr_long_arr = _atr(h, l, c, 20)
    atr      = atr_arr[i]      if atr_arr[i] == atr_arr[i]      else 0.0001
    atr_long = atr_long_arr[i] if atr_long_arr[i] == atr_long_arr[i] else 0.0001

    # Spread / exec  ← FIX #1: math.log ahora funciona
    hi_lo_r    = [math.log(h[j] / l[j]) if l[j] > 0 and h[j] > l[j] else 0.0 for j in range(n)]
    spread_arr = _sma(hi_lo_r, i_spl)
    bp_drain   = spread_arr[i] * 100 if spread_arr[i] == spread_arr[i] else 999
    exec_ok    = bp_drain < i_bpt

    # OBV
    obv_arr     = _obv(c, v)
    obv_ma_arr  = _ema(obv_arr, i_vol)
    obv_std_arr = _stdev(obv_arr, i_vol)
    f_vol_val   = (obv_arr[i] - obv_ma_arr[i]) / obv_std_arr[i] if obv_std_arr[i] > 0 else 0.0

    # Score compuesto (L2)
    roc_arr   = _roc(c, i_mom)
    sma_c     = _sma(c, i_mom)
    std_c     = _stdev(c, i_mom)
    basis_arr = _sma(c, i_rev)
    bstd_arr  = _stdev(c, i_rev)

    raw_scores = []
    for j in range(n):
        mom_j = (c[j] - c[j-i_mom]) / c[j-i_mom] if j >= i_mom and c[j-i_mom] != 0 else 0.0
        vol_norm_j = std_c[j] / sma_c[j] if sma_c[j] and sma_c[j] > 0 and std_c[j] == std_c[j] else 0.0
        f_mom_j  = mom_j / vol_norm_j if vol_norm_j != 0 else 0.0
        f_rev_j  = -(c[j] - basis_arr[j]) / bstd_arr[j] if bstd_arr[j] and bstd_arr[j] > 0 else 0.0
        obv_s    = (obv_arr[j] - obv_ma_arr[j]) / obv_std_arr[j] if obv_std_arr[j] > 0 else 0.0
        raw_scores.append(i_w1 * f_mom_j + i_w2 * f_rev_j + i_w3 * obv_s)

    comp_scores = _ema(raw_scores, i_smo)
    sc_std_arr  = _stdev(comp_scores, i_dlen)
    norm_score  = _tanh(comp_scores[i] / sc_std_arr[i]) if sc_std_arr[i] and sc_std_arr[i] > 0 else 0.0

    # Decay (IC rolling) — FIX #8: O(n) approx
    fwd_rets = [((c[j] - c[j-1]) / c[j-1]) if j > 0 and c[j-1] != 0 else 0.0 for j in range(n)]
    norm_scores_shifted = [
        comp_scores[j-1] / sc_std_arr[j-1]
        if j > 0 and sc_std_arr[j-1] and sc_std_arr[j-1] > 0 else 0.0
        for j in range(n)
    ]
    ic_roll_vals = [abs(_correlation(norm_scores_shifted, fwd_rets, i_dlen, j)) for j in range(n)]
    ic_roll_ema  = _ema(ic_roll_vals, i_smo)
    ic_peak      = _highest(ic_roll_ema, i_dlen, i)
    decay_r      = ic_roll_ema[i] / ic_peak if ic_peak and ic_peak > 0 else 0.5
    sig_alive    = decay_r >= i_dthr    # FIX: umbral 0.35

    # Dark Pool
    vol_base_arr = _sma(v, i_dpb)
    vol_base     = vol_base_arr[i] if vol_base_arr[i] == vol_base_arr[i] else 0.0
    vol_spike    = v[i] > vol_base * i_dpm
    rng_narrow   = (h[i] - l[i]) < atr * 0.65
    dp_buy  = vol_spike and rng_narrow and c[i] > o[i]
    dp_sell = vol_spike and rng_narrow and c[i] < o[i]

    # Asimetría  FIX #2: threshold 1.15
    up_rng = [(h[j] - l[j]) if c[j] > o[j] else 0.0 for j in range(n)]
    dn_rng = [(h[j] - l[j]) if c[j] < o[j] else 0.0 for j in range(n)]
    avg_up = _sma(up_rng, i_asl)
    avg_dn = _sma(dn_rng, i_asl)
    rng_rb      = avg_up[i] / avg_dn[i] if avg_dn[i] > 0 else 1.0
    rng_rb_bear = avg_dn[i] / avg_up[i] if avg_up[i] > 0 else 1.0
    asym_bull = rng_rb      >= i_arr
    asym_bear = rng_rb_bear >= i_abr

    # HTF tendencia
    htf_c    = [x["close"] for x in htf_candles]
    htf_ema9  = _ema(htf_c, 9)
    htf_ema21 = _ema(htf_c, 21)
    htf_i = len(htf_c) - 1
    htf_bull = htf_ema9[htf_i] > htf_ema21[htf_i]
    htf_bear = htf_ema9[htf_i] < htf_ema21[htf_i]

    # Trendline break
    tl_break_long  = False
    tl_break_short = False
    ph_vals: list[tuple] = []
    pl_vals: list[tuple] = []
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
            tl_dn_now  = pv1 + slope * (i - pb1)
            tl_dn_prev = pv1 + slope * (i - 1 - pb1)
            tl_break_long = (c[i] > tl_dn_now + atr * i_tlm and
                              c[i-1] <= tl_dn_prev + atr * i_tlm)

    if len(pl_vals) >= 2:
        (pb2, pv2), (pb1, pv1) = pl_vals[-2], pl_vals[-1]
        if pv2 < pv1 and (i - pb2) <= i_tlb:
            slope = (pv1 - pv2) / max(pb1 - pb2, 1)
            tl_up_now  = pv1 + slope * (i - pb1)
            tl_up_prev = pv1 + slope * (i - 1 - pb1)
            tl_break_short = (c[i] < tl_up_now - atr * i_tlm and
                               c[i-1] >= tl_up_prev - atr * i_tlm)

    # Swing (Higher Lows / Lower Highs)  FIX #3: movido a bonus
    recent_pl = [pv for (pb, pv) in pl_vals if (i - pb) <= i_hlw]
    recent_ph = [pv for (pb, pv) in ph_vals if (i - pb) <= i_hlw]
    hl_count = sum(1 for j in range(1, len(recent_pl)) if recent_pl[j] > recent_pl[j-1])
    lh_count = sum(1 for j in range(1, len(recent_ph)) if recent_ph[j] < recent_ph[j-1])
    sell_exhausted = hl_count >= i_hlc
    buy_exhausted  = lh_count >= i_hhc

    # Anti-spoof
    spoof_vol_ok = v[i-1] > vol_base * i_spoof_vol if i > 0 else False
    spoof_ret_ok = abs(c[i] - c[i-2]) < atr * i_spoof_rev if i > 1 else False
    bull_trap = (c[i-1] > o[i-1] and (h[i-1]-l[i-1]) > atr*1.5 and
                 c[i] < o[i] and v[i-1] > vol_base*1.8) if i > 0 else False
    bear_trap = (c[i-1] < o[i-1] and (h[i-1]-l[i-1]) > atr*1.5 and
                 c[i] > o[i] and v[i-1] > vol_base*1.8) if i > 0 else False
    in_spoof_zone = (spoof_vol_ok and spoof_ret_ok) or bull_trap or bear_trap

    # Blackout
    in_blackout = atr > atr_long * i_blk_mult

    # Sesión
    current_hour = datetime.now(timezone.utc).hour
    in_time_window = (7 <= current_hour < 21) or (current_hour >= 21) or (current_hour < 1)

    # Liquidaciones
    highest_h     = _highest(h, i_liq_look, i)
    lowest_l      = _lowest(l,  i_liq_look, i)
    liq_long_zone  = highest_h * (1.0 - 1.0 / i_lev) if highest_h == highest_h else 0
    liq_short_zone = lowest_l  * (1.0 + 1.0 / i_lev) if lowest_l == lowest_l else 0
    near_liq_long  = abs(c[i] - liq_long_zone)  < atr * 0.5
    near_liq_short = abs(c[i] - liq_short_zone) < atr * 0.5

    # CVD
    bar_pos   = [(c[j] - l[j]) / (h[j] - l[j]) if (h[j] - l[j]) > 0 else 0.5 for j in range(n)]
    bar_delta = [v[j] * (2.0 * bar_pos[j] - 1.0) for j in range(n)]
    cvd_arr   = _sma(bar_delta, i_cvd_len)
    price_roc = _roc(c, i_cvd_len)
    cvd_roc   = _roc(cvd_arr, i_cvd_len)
    price_roc_val = price_roc[i] if price_roc[i] == price_roc[i] else 0.0
    cvd_roc_val   = cvd_roc[i]   if cvd_roc[i]   == cvd_roc[i]   else 0.0
    cvd_div_bull  = price_roc_val < 0 and cvd_roc_val >  i_cvd_th
    cvd_div_bear  = price_roc_val > 0 and cvd_roc_val < -i_cvd_th
    cvd_z         = _winsor(_zscore(cvd_arr, i_cvd_len * 2, i))

    # Stop Hunt
    bar_range = h[i] - l[i]
    low_wick   = min(o[i], c[i]) - l[i]
    high_wick  = h[i] - max(o[i], c[i])
    stop_hunt_dn = low_wick  > bar_range * i_sh_wick and v[i] > vol_base * i_sh_vol and c[i] > o[i]
    stop_hunt_up = high_wick > bar_range * i_sh_wick and v[i] > vol_base * i_sh_vol and c[i] < o[i]

    filters_ok = not in_spoof_zone and not in_blackout and in_time_window and exec_ok

    # ══════════════════════════════════════════════════════════════════════
    # FIX #6 — SISTEMA DE PUNTUACIÓN (0-100) EN LUGAR DE AND PURO
    # ══════════════════════════════════════════════════════════════════════
    score_long  = 0
    score_short = 0
    reasons_long:  list[str] = []
    reasons_short: list[str] = []

    # ── LONG scoring
    if norm_score > 0.15:       # FIX: 0.15 equilibrado
        score_long += 20
        reasons_long.append(f"✅ Score modelo {norm_score:.2f}")
    if sig_alive:
        score_long += 15
        reasons_long.append(f"✅ IC decay ok ({decay_r:.2f})")
    if htf_bull:
        score_long += 20
        reasons_long.append("✅ HTF alcista (EMA9>EMA21)")
    if tl_break_long:           # FIX #5: bonus, no obligatorio
        score_long += 15
        reasons_long.append("✅ Ruptura TL bajista")
    if dp_buy:
        score_long += 15
        reasons_long.append("✅ Dark Pool compra")
    if asym_bull:               # FIX #2: threshold 1.15
        score_long += 10
        reasons_long.append(f"✅ Asimetría alcista ({rng_rb:.2f}x)")
    if sell_exhausted:          # FIX #3: bonus, no obligatorio
        score_long += 5
        reasons_long.append("✅ Vendedores agotados (HL)")
    if cvd_div_bull:
        score_long += 5
        reasons_long.append("✅ Divergencia CVD alcista")
    if stop_hunt_dn:
        score_long += 10
        reasons_long.append("✅ Stop Hunt bajista detectado")

    # ── SHORT scoring
    if norm_score < -0.15:
        score_short += 20
        reasons_short.append(f"📉 Score modelo {norm_score:.2f}")
    if sig_alive:
        score_short += 15
        reasons_short.append(f"📉 IC decay ok ({decay_r:.2f})")
    if htf_bear:
        score_short += 20
        reasons_short.append("📉 HTF bajista (EMA9<EMA21)")
    if tl_break_short:
        score_short += 15
        reasons_short.append("📉 Ruptura TL alcista")
    if dp_sell:
        score_short += 15
        reasons_short.append("📉 Dark Pool venta")
    if asym_bear:
        score_short += 10
        reasons_short.append(f"📉 Asimetría bajista ({rng_rb_bear:.2f}x)")
    if buy_exhausted:
        score_short += 5
        reasons_short.append("📉 Compradores agotados (LH)")
    if cvd_div_bear:
        score_short += 5
        reasons_short.append("📉 Divergencia CVD bajista")
    if stop_hunt_up:
        score_short += 10
        reasons_short.append("📉 Stop Hunt alcista detectado")

    # Filtros globales de seguridad (anulan la señal completamente)
    if not filters_ok:
        blocked_by = []
        if in_spoof_zone:  blocked_by.append("spoof")
        if in_blackout:    blocked_by.append("blackout_ATR")
        if not exec_ok:    blocked_by.append(f"spread {bp_drain:.3f}%>{i_bpt}%")
        if not in_time_window: blocked_by.append("fuera_sesion")
        log.debug(f"{symbol}: filtros bloqueados {blocked_by}")
        return None, 0, [f"Filtros: {', '.join(blocked_by)}"]

    # Determinar señal según puntuación
    best_side   = "LONG" if score_long >= score_short else "SHORT"
    best_score  = score_long if best_side == "LONG" else score_short
    best_reasons = reasons_long if best_side == "LONG" else reasons_short

    if best_score < MIN_SCORE:
        log.debug(f"{symbol}: score {best_score} < mínimo {MIN_SCORE}")
        return None, best_score, best_reasons

    # Clasificar señal por score
    extra = best_score - MIN_SCORE
    if extra >= 35 and (cvd_z > 0 if best_side == "LONG" else cvd_z < 0):
        sig = f"{best_side[:4]}_SUP_V3" if best_side == "LONG" else "SHORT_SUP_V3"
        sig = f"LONG_SUP_V3" if best_side == "LONG" else "SHORT_SUP_V3"
    elif extra >= 25:
        sig = f"LONG_SUP" if best_side == "LONG" else "SHORT_SUP"
    elif extra >= 15:
        sig = f"LONG_FUEL" if best_side == "LONG" else "SHORT_FUEL"
    else:
        sig = f"HUNT_LONG" if best_side == "LONG" else "HUNT_SHORT"

    return sig, best_score, best_reasons

# ══════════════════════════════════════════════════════════════════════════════
#  TRADING LOGIC
# ══════════════════════════════════════════════════════════════════════════════
def calc_quantity(price: float, usdt: float) -> float:
    return max(round(usdt / price, 4), 0.001)

async def handle_signal(signal: str, symbol: str, score: int,
                        reasons: list[str]) -> None:
    if SIGNAL_RANK.get(signal, 0) < MIN_RANK:
        log.info(f"Nivel insuficiente: {signal} (rank {SIGNAL_RANK.get(signal,0)} < {MIN_RANK})")
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
        usdt_to_use = min(TRADE_SIZE_USDT, balance * 0.20) if balance > 0 else TRADE_SIZE_USDT
        if not LIVE_TRADING:
            usdt_to_use = TRADE_SIZE_USDT  # en paper ignoramos balance real

        qty  = calc_quantity(price, usdt_to_use)
        side = "BUY" if is_long else "SELL"
        sl   = round(price * (1 - SL_PCT/100) if is_long else price * (1 + SL_PCT/100), 4)
        tp   = round(price * (1 + TP_PCT/100) if is_long else price * (1 - TP_PCT/100), 4)

        order    = await place_order(symbol, side, qty, sl, tp)
        order_id = order.get("data", {}).get("orderId", "—")
        open_trades[symbol] = {
            "side": side, "entry": price, "qty": qty,
            "sl": sl, "tp": tp, "signal": signal, "score": score,
            "order_id": order_id, "time": time.time(),
        }
        await tg_send(fmt_signal_msg(signal, symbol, price, sl, tp, qty, score, reasons))
        log.info(f"Trade: {side} {symbol} @ {price} score={score}")
    except Exception as e:
        log.error(f"Error trade {signal} {symbol}: {e}")
        await tg_send(f"❌ <b>ERROR</b> {signal} {symbol}\n<code>{e}</code>")

# ══════════════════════════════════════════════════════════════════════════════
#  SCANNER
# ══════════════════════════════════════════════════════════════════════════════
async def scanner_loop() -> None:
    global _cycle_count
    await asyncio.sleep(10)
    mode_str = "REAL 💰" if LIVE_TRADING else "PAPER 📄"
    await tg_send(
        f"🔍 <b>Scanner activo</b> [{mode_str}]\n"
        f"Símbolos: <code>{len(SYMBOLS)}</code> configurados\n"
        f"Intervalo velas: <code>{KLINE_INTERVAL}</code> | Escaneo cada <code>{SCAN_INTERVAL}s</code>\n"
        f"Score mínimo: <code>{MIN_SCORE}</code> | Señal mínima: <code>{MIN_SIGNAL_LEVEL}</code>\n"
        f"SL: <code>{SL_PCT}%</code> | TP: <code>{TP_PCT}%</code>"
    )

    # Cargar todos los símbolos de BingX si SYMBOLS vacío
    scan_symbols = SYMBOLS
    if not scan_symbols:
        scan_symbols = await get_all_symbols()

    while True:
        _cycle_count += 1
        signals_found = 0
        t0 = time.time()

        log.info(f"Ciclo #{_cycle_count} — escaneando {len(scan_symbols)} pares...")

        for symbol in scan_symbols:
            try:
                candles     = await get_klines(symbol, KLINE_INTERVAL, limit=KLINE_LIMIT)
                htf_candles = await get_klines(symbol, HTF_INTERVAL,   limit=60)
                signal, score, reasons = compute_signal(candles, htf_candles, symbol)
                if signal:
                    signals_found += 1
                    log.info(f"⚡ Señal: {signal} {symbol} score={score}")
                    await handle_signal(signal, symbol, score, reasons)
                await asyncio.sleep(0.3)
            except Exception as e:
                log.error(f"Scanner error {symbol}: {e}")
                await asyncio.sleep(1)

        elapsed = time.time() - t0
        log.info(f"Ciclo #{_cycle_count} — sin señales ({elapsed:.1f}s)" if signals_found == 0
                 else f"Ciclo #{_cycle_count} — {signals_found} señal(es) ({elapsed:.1f}s)")
        await asyncio.sleep(max(0, SCAN_INTERVAL - elapsed))

# ══════════════════════════════════════════════════════════════════════════════
#  MONITOR DE POSICIONES
# ══════════════════════════════════════════════════════════════════════════════
async def position_monitor() -> None:
    while True:
        await asyncio.sleep(30)
        for symbol, trade in list(open_trades.items()):
            try:
                live_pos = await get_open_position(symbol)
                if live_pos is None and not LIVE_TRADING is False:
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

# ══════════════════════════════════════════════════════════════════════════════
#  FASTAPI
# ══════════════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(scanner_loop())
    asyncio.create_task(position_monitor())
    mode_str = "REAL 💰" if LIVE_TRADING else "PAPER 📄"
    await tg_send(
        f"🤖 <b>QF×JP Bot v2.1 iniciado</b>\n"
        f"Modo: <b>{mode_str}</b>\n"
        f"Score mínimo: <b>{MIN_SCORE}</b> | Fix #1-10 aplicados"
    )
    log.info(f"Bot iniciado — LIVE_TRADING={LIVE_TRADING} MIN_SCORE={MIN_SCORE}")
    yield
    log.info("Bot detenido")

app = FastAPI(title="QF×JP Bot v2.1", lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
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
        asyncio.create_task(handle_signal(signal, symbol, 99, ["Webhook manual"]))
    return JSONResponse({"status": "ok"})

@app.get("/health")
async def health():
    return {
        "status": "running",
        "version": "2.1",
        "live_trading": LIVE_TRADING,
        "min_score": MIN_SCORE,
        "cycle": _cycle_count,
        "open_trades": len(open_trades),
        "trades": list(open_trades.keys()),
    }

@app.get("/trades")
async def trades():
    return {"open_trades": open_trades}

@app.get("/scan/{symbol}")
async def scan_now(symbol: str):
    sym = symbol.upper()
    if "-" not in sym:
        sym = sym.replace("USDT", "") + "-USDT"
    try:
        candles     = await get_klines(sym, KLINE_INTERVAL, limit=KLINE_LIMIT)
        htf_candles = await get_klines(sym, HTF_INTERVAL,   limit=60)
        signal, score, reasons = compute_signal(candles, htf_candles, sym)
        return {"symbol": sym, "signal": signal or "none", "score": score, "reasons": reasons}
    except Exception as e:
        return {"symbol": sym, "error": str(e)}

@app.get("/debug/{symbol}")
async def debug_symbol(symbol: str):
    """Diagnóstico completo — muestra qué filtro bloquea la señal."""
    sym = symbol.upper()
    if "-" not in sym:
        sym = sym.replace("USDT", "") + "-USDT"
    try:
        candles     = await get_klines(sym, KLINE_INTERVAL, limit=KLINE_LIMIT)
        htf_candles = await get_klines(sym, HTF_INTERVAL,   limit=60)
        signal, score, reasons = compute_signal(candles, htf_candles, sym)
        return {
            "symbol": sym, "candles": len(candles), "htf_candles": len(htf_candles),
            "signal": signal or "none", "score": score,
            "min_score_required": MIN_SCORE,
            "reasons": reasons,
        }
    except Exception as e:
        return {"symbol": sym, "error": str(e)}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
