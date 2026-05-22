"""
QF×JP Crypto Bot — BingX + Telegram  ·  FIXED v2
Fixes aplicados:
  A) exec_ok: umbral 0.18% → 1.5% (crypto 3m es volátil)
  B) sell/buy_exhausted: i_hlc/i_hhc reducido a 1, velas ampliadas a 200
  C) get_klines: maneja tanto arrays como dicts de BingX
  D) spoof_alert_bars: simulado con ventana fija (sin estado)
  E) in_time_window: incluye todas las horas (asia on)
  F) Decay % logueado y configurable (MIN_DECAY_PCT)
  G) Lógica de señal más permisiva con logging detallado por qué no entra
"""
import asyncio
import logging
import math
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

# FIX B: umbral de drenaje de ejecución subido a 1.5% (crypto volátil)
EXEC_BP_THRESHOLD = float(os.environ.get("EXEC_BP_THRESHOLD", "1.5"))

# FIX F: umbral mínimo de decay (% de vida de la señal, 0-1)
MIN_DECAY_PCT = float(os.environ.get("MIN_DECAY_PCT", "0.40"))

# Parseo correcto de SYMBOLS (evita tokens malformados como "XRP-USDTKLINE_INTERVAL=3m")
_raw_symbols = os.environ.get("SYMBOLS", "").strip()
if _raw_symbols:
    SYMBOLS_OVERRIDE = ",".join(
        s.strip() for s in _raw_symbols.split(",")
        if s.strip() and "-USDT" in s.strip() and "=" not in s.strip()
    )
else:
    SYMBOLS_OVERRIDE = ""

MIN_VOLUME_USDT = float(os.environ.get("MIN_VOLUME_USDT", "500000"))
SCAN_INTERVAL   = int(os.environ.get("SCAN_INTERVAL", "180"))
SYMBOLS: list[str] = []
KLINE_INTERVAL  = os.environ.get("KLINE_INTERVAL", "3m")
HTF_INTERVAL    = os.environ.get("HTF_INTERVAL", "15m")

# FIX B: más velas para tener suficientes pivots
KLINE_LIMIT     = int(os.environ.get("KLINE_LIMIT", "200"))

BINGX_BASE = "https://open-api.bingx.com"

SIGNAL_RANK = {
    "HUNT_LONG":   1, "HUNT_SHORT":   1,
    "LONG_FUEL":   2, "SHORT_FUEL":   2,
    "LONG_SUP":    3, "SHORT_SUP":    3,
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

async def get_all_symbols(min_volume: float = 500_000) -> list[str]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                BINGX_BASE + "/openApi/swap/v2/quote/contracts",
                headers={"Content-Type": "application/json"}
            ) as r:
                data = await r.json()

        contracts = data.get("data", [])
        usdt_pairs = [
            c["symbol"] for c in contracts
            if c.get("symbol", "").endswith("-USDT") and c.get("status", 1) == 1
        ]
        log.info(f"Contratos USDT activos: {len(usdt_pairs)}")

        async with aiohttp.ClientSession() as s:
            async with s.get(
                BINGX_BASE + "/openApi/swap/v2/quote/ticker",
                headers={"Content-Type": "application/json"}
            ) as r:
                ticker_data = await r.json()

        tickers = {
            t["symbol"]: float(t.get("quoteVolume", 0))
            for t in ticker_data.get("data", [])
            if t.get("symbol", "").endswith("-USDT")
        }

        symbols_with_vol = [
            (sym, tickers.get(sym, 0))
            for sym in usdt_pairs
            if tickers.get(sym, 0) >= min_volume
        ]
        symbols_with_vol.sort(key=lambda x: x[1], reverse=True)
        result = [s for s, _ in symbols_with_vol]
        log.info(f"Pares con vol≥{min_volume/1e6:.2f}M: {len(result)}")
        return result

    except Exception as e:
        log.error(f"Error obteniendo símbolos: {e}")
        return ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
                "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT"]


# FIX C: maneja tanto arrays como dicts en la respuesta de BingX klines
async def get_klines(symbol: str, interval: str, limit: int = 200) -> list[dict]:
    data = await bingx_get("/openApi/swap/v3/quote/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
    })
    candles = []
    for c in data.get("data", []):
        try:
            if isinstance(c, dict):
                # Formato dict: {"open": ..., "high": ..., ...}
                candles.append({
                    "open":   float(c.get("open",  c.get("o", 0))),
                    "high":   float(c.get("high",  c.get("h", 0))),
                    "low":    float(c.get("low",   c.get("l", 0))),
                    "close":  float(c.get("close", c.get("c", 0))),
                    "volume": float(c.get("volume",c.get("v", 0))),
                })
            else:
                # Formato array: [timestamp, open, high, low, close, volume, ...]
                candles.append({
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                })
        except (IndexError, KeyError, ValueError):
            continue
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
        "stopLossPrice":   str(round(sl_price, 4)),
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
                   sl: float, tp: float, qty: float, decay_pct: float) -> str:
    emoji = "🟢" if "LONG" in signal else "🔴"
    stars = "★" * SIGNAL_RANK.get(signal, 1)
    return (
        f"{emoji} <b>{signal}</b> {stars}\n"
        f"📊 <b>{symbol}</b> @ <code>{price}</code>\n"
        f"📐 Qty: <code>{qty}</code> | Capital: <code>{round(qty*price,2)} USDT</code>\n"
        f"🛑 SL: <code>{sl}</code>  🎯 TP: <code>{tp}</code>\n"
        f"📉 Decay: <code>{decay_pct:.0f}%</code>\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )

def fmt_close_msg(symbol: str, pnl_pct: float, reason: str) -> str:
    emoji = "✅" if pnl_pct > 0 else "❌"
    return (f"{emoji} <b>CERRADO</b> {symbol}\n"
            f"PnL: <code>{pnl_pct:+.2f}%</code>\nRazón: {reason}")

# ══════════════════════════════════════════════════
#  INDICADORES
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
                 abs(lows[i]  - closes[i-1]))
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
        mx = sum(xs) / len(xs);  my = sum(ys) / len(ys)
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(xs, ys))
        den = (sum((xi-mx)**2 for xi in xs) * sum((yi-my)**2 for yi in ys)) ** 0.5
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
    v = max(min(2.0 * x, 20.0), -20.0)
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
    if pivot_idx < left or pivot_idx >= len(highs):
        return None
    candidate = highs[pivot_idx]
    for i in range(pivot_idx - left, pivot_idx + right + 1):
        if 0 <= i < len(highs) and i != pivot_idx and highs[i] >= candidate:
            return None
    return candidate

def _pivot_low(lows: list[float], left: int, right: int, idx: int) -> Optional[float]:
    pivot_idx = idx - right
    if pivot_idx < left or pivot_idx >= len(lows):
        return None
    candidate = lows[pivot_idx]
    for i in range(pivot_idx - left, pivot_idx + right + 1):
        if 0 <= i < len(lows) and i != pivot_idx and lows[i] <= candidate:
            return None
    return candidate

# ══════════════════════════════════════════════════
#  MOTOR DE SEÑALES — QF×JP V3 FIXED
# ══════════════════════════════════════════════════
def compute_signal(candles: list[dict], htf_candles: list[dict],
                   debug: bool = False) -> tuple[Optional[str], float, dict]:
    """
    Retorna (signal_name | None, decay_pct 0-100, debug_dict).
    """
    dbg: dict = {}

    if len(candles) < 80 or len(htf_candles) < 25:
        return None, 0.0, {"reason": f"insuf_velas ltf={len(candles)} htf={len(htf_candles)}"}

    o = [c["open"]   for c in candles]
    h = [c["high"]   for c in candles]
    l = [c["low"]    for c in candles]
    c = [c["close"]  for c in candles]
    v = [c["volume"] for c in candles]
    n = len(c)
    i = n - 1

    # ── Parámetros
    i_mom = 20; i_rev = 8; i_vol = 14; i_atr_p = 10
    i_w1 = 0.40; i_w2 = 0.30; i_w3 = 0.30
    i_dlen = 40; i_dthr = MIN_DECAY_PCT          # FIX F: configurable
    i_dpm = 2.5; i_dpb = 20; i_spl = 5
    i_bpt = EXEC_BP_THRESHOLD                    # FIX A: 1.5% en vez de 0.18%
    i_asl = 10; i_arr = 1.40; i_abr = 1.40
    i_tlb = 30; i_tll = 5; i_tlr = 3; i_tlm = 0.15
    i_pll = 5;  i_plr = 3
    i_hlc = 1; i_hhc = 1                        # FIX B: 2→1 (más permisivo)
    i_hlw = 40
    i_spoof_vol = 2.0; i_spoof_rev = 0.25
    i_blk_mult = 2.5
    i_lev = 10.0; i_liq_look = 50
    i_cvd_len = 20; i_cvd_th = 1.5
    i_sh_wick = 0.60; i_sh_vol = 1.5
    i_smo = 3

    # ── ATR
    atr_arr      = _atr(h, l, c, i_atr_p)
    atr_long_arr = _atr(h, l, c, 20)
    atr      = atr_arr[i]      if atr_arr[i] == atr_arr[i]      else 0.0001
    atr_long = atr_long_arr[i] if atr_long_arr[i] == atr_long_arr[i] else 0.0001

    # ── Spread / exec  (FIX A)
    hi_lo_r    = [math.log(h[j] / l[j]) if l[j] > 0 else 0.0 for j in range(n)]
    spread_arr = _sma(hi_lo_r, i_spl)
    bp_drain   = spread_arr[i] * 100 if spread_arr[i] == spread_arr[i] else 0.0
    exec_ok    = bp_drain < i_bpt
    dbg["bp_drain"] = round(bp_drain, 4)
    dbg["exec_ok"]  = exec_ok

    # ── OBV
    obv_arr      = _obv(c, v)
    obv_ma_arr   = _ema(obv_arr, i_vol)
    obv_std_arr  = _stdev(obv_arr, i_vol)
    f_vol_pre_arr = [
        (obv_arr[j] - obv_ma_arr[j]) / obv_std_arr[j]
        if obv_std_arr[j] > 0 else 0.0
        for j in range(n)
    ]

    # ── Crowding
    roc_arr      = _roc(c, i_mom)
    roc_simple   = [r / 100 if r == r else 0.0 for r in roc_arr]
    simple_z_list = [_winsor(_zscore(roc_simple, i_mom * 2, j)) for j in range(n)]

    crowd_count = 0
    for j in range(max(0, i - 15), i + 1):
        cr = _correlation(simple_z_list, f_vol_pre_arr, i_mom * 3, j)
        crowd_count = crowd_count + 1 if abs(cr) >= 0.75 else 0
    crowd_persistent = crowd_count >= 15
    w1_dyn = max(i_w1 - 0.15, 0.10) if crowd_persistent else i_w1
    w3_dyn = min(i_w3 + 0.15, 0.60) if crowd_persistent else i_w3

    # ── Scores
    sma_c    = _sma(c, i_mom)
    std_c    = _stdev(c, i_mom)
    basis_arr = _sma(c, i_rev)
    bstd_arr  = _stdev(c, i_rev)

    raw_scores = []
    for j in range(n):
        mom_j = (c[j] - c[j - i_mom]) / c[j - i_mom] if j >= i_mom and c[j - i_mom] != 0 else 0.0
        vn_j  = std_c[j] / sma_c[j] if sma_c[j] > 0 and std_c[j] == std_c[j] else 0.0
        fm_j  = mom_j / vn_j if vn_j != 0 else 0.0
        fr_j  = -(c[j] - basis_arr[j]) / bstd_arr[j] if bstd_arr[j] > 0 else 0.0
        raw_scores.append(w1_dyn * fm_j + 0.30 * fr_j + w3_dyn * f_vol_pre_arr[j])

    comp_scores = _ema(raw_scores, i_smo)
    sc_std_arr  = _stdev(comp_scores, i_dlen)
    norm_score  = _tanh(comp_scores[i] / sc_std_arr[i]) if sc_std_arr[i] > 0 else 0.0
    dbg["norm_score"] = round(norm_score, 4)

    # ── Decay (FIX F: porcentaje exacto del Pine)
    fwd_rets = [
        (c[j] - c[j-1]) / c[j-1] if j > 0 and c[j-1] != 0 else 0.0
        for j in range(n)
    ]
    norm_scores_shifted = [
        comp_scores[j-1] / sc_std_arr[j-1]
        if j > 0 and sc_std_arr[j-1] > 0 else 0.0
        for j in range(n)
    ]
    ic_roll_vals = [
        abs(_correlation(norm_scores_shifted, fwd_rets, i_dlen, j))
        for j in range(n)
    ]
    ic_roll_ema = _ema(ic_roll_vals, i_smo)
    ic_peak     = _highest(ic_roll_ema, i_dlen, i)
    decay_r     = ic_roll_ema[i] / ic_peak if ic_peak and ic_peak > 0 else 0.5
    sig_alive   = decay_r >= i_dthr
    decay_pct   = decay_r * 100.0
    dbg["decay_pct"] = round(decay_pct, 1)
    dbg["sig_alive"] = sig_alive

    # ── Dark Pool
    vol_base_arr = _sma(v, i_dpb)
    vol_base = vol_base_arr[i] if vol_base_arr[i] == vol_base_arr[i] else 0.0
    vol_spike  = v[i] > vol_base * i_dpm
    rng_narrow = (h[i] - l[i]) < atr * 0.6
    dp_buy  = vol_spike and rng_narrow and c[i] > o[i]
    dp_sell = vol_spike and rng_narrow and c[i] < o[i]

    # ── Asimetría
    up_rng = [(h[j] - l[j]) if c[j] > o[j] else 0.0 for j in range(n)]
    dn_rng = [(h[j] - l[j]) if c[j] < o[j] else 0.0 for j in range(n)]
    avg_up = _sma(up_rng, i_asl)
    avg_dn = _sma(dn_rng, i_asl)
    rng_rb      = avg_up[i] / avg_dn[i] if avg_dn[i] > 0 else 1.0
    rng_rb_bear = avg_dn[i] / avg_up[i] if avg_up[i] > 0 else 1.0
    asym_bull = rng_rb >= i_arr
    asym_bear = rng_rb_bear >= i_abr
    dbg["asym_bull"] = asym_bull
    dbg["asym_bear"] = asym_bear

    # ── HTF tendencia
    htf_c    = [x["close"] for x in htf_candles]
    htf_ema9  = _ema(htf_c, 9)
    htf_ema21 = _ema(htf_c, 21)
    htf_i     = len(htf_c) - 1
    htf_bull  = htf_ema9[htf_i] > htf_ema21[htf_i]
    htf_bear  = htf_ema9[htf_i] < htf_ema21[htf_i]
    dbg["htf_bull"] = htf_bull
    dbg["htf_bear"] = htf_bear

    # ── Trendline
    tl_break_long  = False
    tl_break_short = False
    ph_vals: list[tuple[int, float]] = []
    pl_vals: list[tuple[int, float]] = []
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
            slope      = (pv1 - pv2) / max(pb1 - pb2, 1)
            tl_dn_now  = pv1 + slope * (i - pb1)
            tl_dn_prev = pv1 + slope * (i - 1 - pb1)
            tl_break_long = (c[i] > tl_dn_now + atr * i_tlm and
                             c[i-1] <= tl_dn_prev + atr * i_tlm)

    if len(pl_vals) >= 2:
        (pb2, pv2), (pb1, pv1) = pl_vals[-2], pl_vals[-1]
        if pv2 < pv1 and (i - pb2) <= i_tlb:
            slope      = (pv1 - pv2) / max(pb1 - pb2, 1)
            tl_up_now  = pv1 + slope * (i - pb1)
            tl_up_prev = pv1 + slope * (i - 1 - pb1)
            tl_break_short = (c[i] < tl_up_now - atr * i_tlm and
                              c[i-1] >= tl_up_prev - atr * i_tlm)

    dbg["tl_break_long"]  = tl_break_long
    dbg["tl_break_short"] = tl_break_short

    # ── Swing (FIX B: i_hlc/i_hhc=1)
    recent_pl = [pv for (pb, pv) in pl_vals if (i - pb) <= i_hlw]
    recent_ph = [pv for (pb, pv) in ph_vals if (i - pb) <= i_hlw]
    hl_count = sum(1 for j in range(1, len(recent_pl)) if recent_pl[j] > recent_pl[j-1])
    lh_count = sum(1 for j in range(1, len(recent_ph)) if recent_ph[j] < recent_ph[j-1])
    sell_exhausted = hl_count >= i_hlc
    buy_exhausted  = lh_count >= i_hhc
    dbg["sell_exhausted"] = sell_exhausted
    dbg["buy_exhausted"]  = buy_exhausted
    dbg["hl_count"]       = hl_count
    dbg["lh_count"]       = lh_count

    # ── Anti-spoof (FIX D: ventana fija sin estado persistente)
    bull_trap = (i > 0 and c[i-1] > o[i-1] and (h[i-1]-l[i-1]) > atr*1.5
                 and c[i] < o[i] and v[i-1] > vol_base * 1.8)
    bear_trap = (i > 0 and c[i-1] < o[i-1] and (h[i-1]-l[i-1]) > atr*1.5
                 and c[i] > o[i] and v[i-1] > vol_base * 1.8)
    spoof_vol_ok = i > 0 and v[i-1] > vol_base * i_spoof_vol
    spoof_ret_ok = i > 1 and abs(c[i] - c[i-2]) < atr * i_spoof_rev
    in_spoof_zone = (spoof_vol_ok and spoof_ret_ok) or bull_trap or bear_trap

    # ── Blackout
    in_blackout = atr > atr_long * i_blk_mult
    dbg["in_blackout"] = in_blackout

    # ── Sesión (FIX E: incluye todas las horas — asia siempre activa)
    current_hour = datetime.now(timezone.utc).hour
    sess_eu     = 7  <= current_hour < 15
    sess_ny     = 13 <= current_hour < 21
    sess_crypto = current_hour >= 21 or current_hour < 1
    sess_asia   = current_hour < 8
    in_time_window = True   # FIX E: no bloquear por horario en scanner 24/7
    dbg["hour_utc"] = current_hour

    # ── Liquidaciones
    highest_h      = _highest(h, i_liq_look, i)
    lowest_l       = _lowest(l,  i_liq_look, i)
    liq_long_zone  = highest_h * (1.0 - 1.0 / i_lev) if highest_h == highest_h else 0
    liq_short_zone = lowest_l  * (1.0 + 1.0 / i_lev) if lowest_l  == lowest_l  else 0
    near_liq_long  = abs(c[i] - liq_long_zone)  < atr * 0.5
    near_liq_short = abs(c[i] - liq_short_zone) < atr * 0.5

    # ── CVD
    bar_pos_arr   = [(c[j]-l[j])/(h[j]-l[j]) if (h[j]-l[j]) > 0 else 0.5 for j in range(n)]
    bar_delta_arr = [v[j] * (2.0 * bar_pos_arr[j] - 1.0) for j in range(n)]
    cvd_arr       = _sma(bar_delta_arr, i_cvd_len)
    price_roc_arr = _roc(c, i_cvd_len)
    cvd_roc_arr   = _roc(cvd_arr, i_cvd_len)
    price_roc_val = price_roc_arr[i] if price_roc_arr[i] == price_roc_arr[i] else 0.0
    cvd_roc_val   = cvd_roc_arr[i]   if cvd_roc_arr[i]   == cvd_roc_arr[i]   else 0.0
    cvd_z         = _winsor(_zscore(cvd_arr, i_cvd_len * 2, i))

    # ── Stop Hunt
    bar_range    = h[i] - l[i]
    low_wick     = min(o[i], c[i]) - l[i]
    high_wick    = h[i] - max(o[i], c[i])
    stop_hunt_dn = (low_wick  > bar_range * i_sh_wick and
                    v[i] > vol_base * i_sh_vol and c[i] > o[i])
    stop_hunt_up = (high_wick > bar_range * i_sh_wick and
                    v[i] > vol_base * i_sh_vol and c[i] < o[i])

    # ══ SEÑALES
    filters_ok = not in_spoof_zone and not in_blackout and in_time_window

    base_long  = (norm_score > 0.15 and sig_alive and exec_ok
                  and htf_bull and asym_bull and sell_exhausted)
    base_short = (norm_score < -0.15 and sig_alive and exec_ok
                  and htf_bear and asym_bear and buy_exhausted)

    dbg["base_long"]  = base_long
    dbg["base_short"] = base_short
    dbg["filters_ok"] = filters_ok

    long_std   = base_long  and filters_ok
    short_std  = base_short and filters_ok
    long_fuel  = long_std   and tl_break_long
    short_fuel = short_std  and tl_break_short
    long_sup   = long_fuel  and dp_buy
    short_sup  = short_fuel and dp_sell
    long_sup_v3  = long_sup  and cvd_z > 0 and not near_liq_short
    short_sup_v3 = short_sup and cvd_z < 0 and not near_liq_long
    sh_long_entry  = (stop_hunt_dn and htf_bull and sig_alive
                      and not in_blackout and in_time_window and not in_spoof_zone)
    sh_short_entry = (stop_hunt_up and htf_bear and sig_alive
                      and not in_blackout and in_time_window and not in_spoof_zone)

    if long_sup_v3:    return "LONG_SUP_V3",  decay_pct, dbg
    if short_sup_v3:   return "SHORT_SUP_V3", decay_pct, dbg
    if long_sup:       return "LONG_SUP",     decay_pct, dbg
    if short_sup:      return "SHORT_SUP",    decay_pct, dbg
    if long_fuel:      return "LONG_FUEL",    decay_pct, dbg
    if short_fuel:     return "SHORT_FUEL",   decay_pct, dbg
    if sh_long_entry:  return "HUNT_LONG",    decay_pct, dbg
    if sh_short_entry: return "HUNT_SHORT",   decay_pct, dbg
    return None, decay_pct, dbg

# ══════════════════════════════════════════════════
#  TRADING LOGIC
# ══════════════════════════════════════════════════
def calc_quantity(price: float, usdt: float) -> float:
    return max(round(usdt / price, 4), 0.001)

async def handle_signal(signal: str, symbol: str, decay_pct: float = 0.0) -> None:
    if SIGNAL_RANK.get(signal, 0) < MIN_RANK:
        return
    if len(open_trades) >= MAX_OPEN_TRADES:
        return

    is_long  = "LONG"  in signal
    is_short = "SHORT" in signal

    if symbol in open_trades:
        ex = open_trades[symbol]
        if (is_long and ex["side"] == "BUY") or (is_short and ex["side"] == "SELL"):
            return

    try:
        price       = await get_price(symbol)
        balance     = await get_balance()
        usdt_to_use = min(TRADE_SIZE_USDT, balance * 0.20)
        if usdt_to_use < 5:
            await tg_send(f"⚠️ Balance insuficiente: <code>{balance:.2f} USDT</code>")
            return

        qty  = calc_quantity(price, usdt_to_use)
        side = "BUY" if is_long else "SELL"
        sl   = round(price * (1 - SL_PCT/100) if is_long else price * (1 + SL_PCT/100), 4)
        tp   = round(price * (1 + TP_PCT/100) if is_long else price * (1 - TP_PCT/100), 4)

        order    = await place_order(symbol, side, qty, sl, tp)
        order_id = order.get("data", {}).get("orderId", "—")
        open_trades[symbol] = {
            "side": side, "entry": price, "qty": qty,
            "sl": sl, "tp": tp, "signal": signal,
            "order_id": order_id, "time": time.time(),
            "decay_pct": decay_pct,
        }
        await tg_send(fmt_signal_msg(signal, symbol, price, sl, tp, qty, decay_pct))
        log.info(f"✅ Trade: {side} {symbol} @ {price} decay={decay_pct:.0f}%")
    except Exception as e:
        log.error(f"Error trade {signal} {symbol}: {e}")
        await tg_send(f"❌ <b>ERROR</b> {signal} {symbol}\n<code>{e}</code>")

# ══════════════════════════════════════════════════
#  SCANNER
# ══════════════════════════════════════════════════
async def scan_symbol(symbol: str, semaphore: asyncio.Semaphore
                      ) -> Optional[tuple[str, str, float]]:
    async with semaphore:
        try:
            candles     = await get_klines(symbol, KLINE_INTERVAL, limit=KLINE_LIMIT)
            htf_candles = await get_klines(symbol, HTF_INTERVAL,   limit=60)
            signal, decay_pct, dbg = compute_signal(candles, htf_candles)
            if signal:
                log.info(f"🎯 SEÑAL {signal} {symbol} decay={decay_pct:.0f}% dbg={dbg}")
                return (symbol, signal, decay_pct)
            # Log periódico para el top-10 más activo (debug: porqué no entra)
            return None
        except Exception as e:
            log.debug(f"Scanner skip {symbol}: {e}")
            return None

# Contador global para log de debug periódico
_scan_debug_counter = 0

async def scanner_loop() -> None:
    global SYMBOLS, _scan_debug_counter
    await asyncio.sleep(10)

    if SYMBOLS_OVERRIDE:
        SYMBOLS = [s.strip() for s in SYMBOLS_OVERRIDE.split(",") if s.strip()]
        log.info(f"Símbolos manuales: {len(SYMBOLS)} → {SYMBOLS}")
    else:
        SYMBOLS = await get_all_symbols(min_volume=MIN_VOLUME_USDT)

    await tg_send(
        f"🔍 <b>Scanner activo — {len(SYMBOLS)} pares</b>\n"
        f"Top 5: <code>{', '.join(SYMBOLS[:5])}</code>\n"
        f"TF: <code>{KLINE_INTERVAL}</code> | HTF: <code>{HTF_INTERVAL}</code>\n"
        f"Ciclo: <code>{SCAN_INTERVAL}s</code> | MinSeñal: <code>{MIN_SIGNAL_LEVEL}</code>\n"
        f"Decay mín: <code>{MIN_DECAY_PCT*100:.0f}%</code> | "
        f"ExecThr: <code>{EXEC_BP_THRESHOLD}%</code>"
    )

    last_refresh = time.time()
    semaphore    = asyncio.Semaphore(10)
    scan_count   = 0

    while True:
        # Refresco de lista cada 6h
        if not SYMBOLS_OVERRIDE and (time.time() - last_refresh) > 21600:
            new_syms = await get_all_symbols(min_volume=MIN_VOLUME_USDT)
            if new_syms:
                added   = len(set(new_syms) - set(SYMBOLS))
                removed = len(set(SYMBOLS) - set(new_syms))
                SYMBOLS = new_syms
                last_refresh = time.time()
                log.info(f"Lista actualizada: {len(SYMBOLS)} pares (+{added} -{removed})")

        scan_count += 1
        _scan_debug_counter += 1
        t0 = time.time()
        log.info(f"Ciclo #{scan_count} — escaneando {len(SYMBOLS)} pares...")

        tasks   = [scan_symbol(sym, semaphore) for sym in SYMBOLS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals_found = []
        for result in results:
            if isinstance(result, tuple):
                sym, sig, dec = result
                signals_found.append((sym, sig, dec))
                await handle_signal(sig, sym, dec)

        elapsed = time.time() - t0

        # Cada 5 ciclos, escanea en modo debug los top-5 para ver porqué no entran
        if _scan_debug_counter % 5 == 0 and not signals_found:
            await _debug_top5()

        if signals_found:
            log.info(f"Ciclo #{scan_count} — {len(signals_found)} señales en {elapsed:.1f}s: "
                     f"{[(s,sig,f'{d:.0f}%') for s,sig,d in signals_found]}")
        else:
            log.info(f"Ciclo #{scan_count} — sin señales ({elapsed:.1f}s)")

        await asyncio.sleep(max(0, SCAN_INTERVAL - elapsed))


async def _debug_top5() -> None:
    """Loguea el estado de los primeros 5 pares para diagnosticar por qué no entran señales."""
    top5 = SYMBOLS[:5] if SYMBOLS else []
    lines = ["🔬 <b>Debug top-5 (sin señales):</b>"]
    for sym in top5:
        try:
            candles     = await get_klines(sym, KLINE_INTERVAL, limit=KLINE_LIMIT)
            htf_candles = await get_klines(sym, HTF_INTERVAL,   limit=60)
            _, decay_pct, dbg = compute_signal(candles, htf_candles)
            lines.append(
                f"<b>{sym}</b> score={dbg.get('norm_score','?')} "
                f"decay={decay_pct:.0f}% exec={dbg.get('exec_ok','?')} "
                f"htfB={dbg.get('htf_bull','?')} asymB={dbg.get('asym_bull','?')} "
                f"sellEx={dbg.get('sell_exhausted','?')} "
                f"tlL={dbg.get('tl_break_long','?')}"
            )
        except Exception as e:
            lines.append(f"{sym}: error {e}")
    await tg_send("\n".join(lines))

# ══════════════════════════════════════════════════
#  MONITOR DE POSICIONES
# ══════════════════════════════════════════════════
async def position_monitor() -> None:
    while True:
        await asyncio.sleep(30)
        for symbol, trade in list(open_trades.items()):
            try:
                live_pos = await get_open_position(symbol)
                price    = await get_price(symbol)
                entry    = trade["entry"]
                side     = trade["side"]
                pnl_pct  = ((price-entry)/entry*100) if side=="BUY" else ((entry-price)/entry*100)

                if live_pos is None:
                    await tg_send(fmt_close_msg(symbol, pnl_pct, "SL/TP ejecutado"))
                    open_trades.pop(symbol, None)
                    continue

                reason: Optional[str] = None
                if side == "BUY":
                    if price <= trade["sl"]:  reason = "SL alcanzado"
                    elif price >= trade["tp"]: reason = "TP alcanzado"
                else:
                    if price >= trade["sl"]:  reason = "SL alcanzado"
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
    await tg_send(
        "🤖 <b>QF×JP Bot v2 iniciado</b>\n"
        "Modo: <b>Scanner BingX directo</b>\n"
        f"Fixes: exec_ok={EXEC_BP_THRESHOLD}% | decay_min={MIN_DECAY_PCT*100:.0f}% | "
        f"HL/LH_min=1 | klines={KLINE_LIMIT} | sesión=24h"
    )
    log.info("Bot v2 iniciado")
    yield
    log.info("Bot detenido")

app = FastAPI(title="QF×JP Bot v2", lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    signal = data.get("signal", "").strip()
    symbol = data.get("symbol", "").strip().upper().replace("USDT", "-USDT")
    if signal and symbol:
        asyncio.create_task(handle_signal(signal, symbol))
    return JSONResponse({"status": "ok"})

@app.get("/health")
async def health():
    return {
        "status":          "running",
        "version":         "v2-fixed",
        "total_symbols":   len(SYMBOLS),
        "symbols_sample":  SYMBOLS[:10],
        "scan_interval_s": SCAN_INTERVAL,
        "open_trades":     len(open_trades),
        "trades":          list(open_trades.keys()),
        "config": {
            "exec_bp_threshold": EXEC_BP_THRESHOLD,
            "min_decay_pct":     MIN_DECAY_PCT,
            "kline_limit":       KLINE_LIMIT,
            "min_signal":        MIN_SIGNAL_LEVEL,
        }
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
        signal, decay_pct, dbg = compute_signal(candles, htf_candles)
        return {"symbol": sym, "signal": signal or "none",
                "decay_pct": round(decay_pct, 1), "debug": dbg}
    except Exception as e:
        return {"symbol": sym, "error": str(e)}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
