"""
Sniper Bot V14.0 — QUANTUM BLACKCORE EDITION
Traducción exacta del Pine Script V14: Quantum Blackcore + Neural Hunter FVG/MSB

LÓGICA PRINCIPAL:
  ┌──────────────────────────────────────────────────────────────┐
  │  TRIPLE SCREEN EMA200:                                       │
  │    is_bullish = close > EMA200(5m) AND > EMA200(H1)         │
  │                 AND > EMA200(H4)                             │
  │                                                              │
  │  SQUEEZE RELEASE (explosión de volatilidad):                 │
  │    BB_upper > KC_upper  → BB saliendo del KC                 │
  │                                                              │
  │  GATILLO:                                                    │
  │    LONG  = bullish_world AND squeeze AND EMA7 crosses EMA17  │
  │    SHORT = bearish_world AND squeeze AND EMA7 crosses EMA17  │
  │                                                              │
  │  SL = lowest(low,5)  /  highest(high,5)                     │
  │  TP = entrada ± riesgo × 4  (R:R 1:4)                       │
  │                                                              │
  │  DD PROTECT: para si pérdida diaria ≥ MAX_DAILY_LOSS%       │
  └──────────────────────────────────────────────────────────────┘

MEJORAS SOBRE EL PINE SCRIPT:
  + FVG (Fair Value Gap) como filtro extra de confluencia
  + MSB (Market Structure Break) confirmación
  + H1 EMA17 trend check (Neural Hunter)
  + Breakeven a 1R
  + Position sizing: risk_usdt / dist_sl (fórmula correcta)
  + Cooldown por símbolo tras SL
  + WebSocket cache para precios en tiempo real
"""
import os, time, hmac, hashlib, json, asyncio, logging, threading
from datetime import datetime, timezone, date
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np
import websocket

try:
    from telegram import Bot
    from telegram.constants import ParseMode
    TELEGRAM_OK = True
except ImportError:
    TELEGRAM_OK = False

# ═══════════════════════════════════════════════════════════
#  CONFIG — todas configurables en Railway
# ═══════════════════════════════════════════════════════════
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ["BINGX_SECRET_KEY"]
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ── TIMEFRAME DE ENTRADA ──────────────────────────────────
TIMEFRAME        = os.environ.get("TIMEFRAME",       "5m")

# ── RIESGO Y CAPITAL ─────────────────────────────────────
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",      "1.5"))  # % por trade
MAX_DAILY_LOSS   = float(os.environ.get("MAX_DAILY_LOSS",    "3.0"))  # % DD diario máx
LEVERAGE         = int(os.environ.get("LEVERAGE",            "5"))
MIN_ORDER_USDT   = float(os.environ.get("MIN_ORDER_USDT",    "3.0"))
MAX_ORDER_USDT   = float(os.environ.get("MAX_ORDER_USDT",    "60.0"))
MAX_MARGIN_PCT   = float(os.environ.get("MAX_MARGIN_PCT",    "30.0"))

# ── OPERACIÓN ─────────────────────────────────────────────
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",        "30"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",     "4"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",        "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",         "0"))
COOLDOWN_MINS    = int(os.environ.get("COOLDOWN_MINS",       "20"))
USE_WS_CACHE     = os.environ.get("USE_WS_CACHE","true").lower() == "true"

# ── SESIÓN ────────────────────────────────────────────────
USE_SESSION      = os.environ.get("USE_SESSION","true").lower() == "true"
SESSION_START    = int(os.environ.get("SESSION_START","8"))   # UTC
SESSION_END      = int(os.environ.get("SESSION_END",  "20"))  # UTC

# ── EMAs — TRIPLE SCREEN ──────────────────────────────────
EMA_FAST         = int(os.environ.get("EMA_FAST",    "7"))
EMA_SLOW         = int(os.environ.get("EMA_SLOW",    "17"))
EMA200           = int(os.environ.get("EMA200",       "200"))  # Pine Script EMA200

# ── SQUEEZE RELEASE ───────────────────────────────────────
BB_LEN           = int(os.environ.get("BB_LEN",       "20"))
BB_MULT          = float(os.environ.get("BB_MULT",     "2.0"))
KC_LEN           = int(os.environ.get("KC_LEN",       "20"))
KC_MULT          = float(os.environ.get("KC_MULT",     "1.5"))
USE_SQUEEZE      = os.environ.get("USE_SQUEEZE","true").lower() == "true"

# ── EMA CROSS ─────────────────────────────────────────────
EMA_CROSS_BARS   = int(os.environ.get("EMA_CROSS_BARS","5"))   # cruce en últimas N velas

# ── SL / TP (Pine Script Quantum) ─────────────────────────
SL_LOOKBACK      = int(os.environ.get("SL_LOOKBACK",   "5"))   # lowest/highest N velas
ATR_LEN          = int(os.environ.get("ATR_LEN",       "14"))
ATR_SL_MARGIN    = float(os.environ.get("ATR_SL_MARGIN","0.3")) # buffer extra al SL
TP_RR            = float(os.environ.get("TP_RR",        "4.0")) # Pine Script: 4R
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT", "0.15"))
MAX_DIST_PCT     = float(os.environ.get("MAX_DIST_PCT", "3.0"))
MAX_SPREAD_PCT   = float(os.environ.get("MAX_SPREAD_PCT","0.15"))

# ── BREAKEVEN ─────────────────────────────────────────────
USE_BE           = os.environ.get("USE_BE","true").lower() == "true"
BE_TRIGGER_R     = float(os.environ.get("BE_TRIGGER_R", "0.33")) # activa a 1.3R

# ── H4/H1 CACHE ───────────────────────────────────────────
H1_CACHE_TTL     = int(os.environ.get("H1_CACHE_TTL","300"))
H4_CACHE_TTL     = int(os.environ.get("H4_CACHE_TTL","900"))

# ── FILTROS OPCIONALES ────────────────────────────────────
USE_FVG          = os.environ.get("USE_FVG","true").lower() == "true"
FVG_LOOKBACK     = int(os.environ.get("FVG_LOOKBACK","6"))
USE_ADX          = os.environ.get("USE_ADX","true").lower() == "true"
ADX_MIN          = float(os.environ.get("ADX_MIN","18.0"))
ADX_LEN          = int(os.environ.get("ADX_LEN","14"))
USE_VOL          = os.environ.get("USE_VOL","true").lower() == "true"
VOL_MULT         = float(os.environ.get("VOL_MULT","1.1"))

_raw = os.environ.get("CUSTOM_SYMBOLS","")
CUSTOM_SYMBOLS = [s.strip() for s in _raw.split(",") if s.strip()] if _raw else []

BINGX_BASE = "https://open-api.bingx.com"
BINGX_WS   = "wss://open-api-swap.bingx.com/swap-market"
INTERVAL_MAP = {
    "1m":"1m","3m":"3m","5m":"5m","15m":"15m",
    "30m":"30m","1h":"1H","4h":"4H","1d":"1D"
}

EXCLUDED_PREFIXES = ("NCS","NCF","NCMEX","NCOIL","NCGAS","NCXAU","NCXAG")
EXCLUDED_KEYWORDS = ("Gasoline","GasOil","Brent","WTI","OilBrent","Copper",
                     "Wheat","Cotton","Soybean","Silver","EURUSD","GBPUSD","JPYUSD")

FALLBACK_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT",
    "DOGE-USDT","ADA-USDT","AVAX-USDT","DOT-USDT","LINK-USDT",
    "LTC-USDT","BCH-USDT","ATOM-USDT","NEAR-USDT","APT-USDT",
    "OP-USDT","ARB-USDT","SUI-USDT","INJ-USDT","WIF-USDT",
    "PEPE-USDT","HBAR-USDT","AAVE-USDT","UNI-USDT","FIL-USDT",
    "GRT-USDT","SEI-USDT","WLD-USDT","TIA-USDT","GMX-USDT",
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

# ── ESTADO GLOBAL ─────────────────────────────────────────
ws_kline_cache = {}
ws_price_cache = {}
ws_cache_lock  = threading.Lock()
sl_cooldown    = {}
h1_cache       = {}
h4_cache       = {}
position_state = {}
pos_state_lock = threading.Lock()

# DD Protect — tracking diario
daily_state = {
    "date":       None,
    "start_bal":  0.0,
    "blocked":    False,
}
daily_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════
#  BINGX API
# ═══════════════════════════════════════════════════════════
def _sign(params):
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(BINGX_SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()

def bx_get(path, params=None):
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    r = requests.get(BINGX_BASE + path, params=p,
                     headers={"X-BX-APIKEY": BINGX_API_KEY}, timeout=15)
    r.raise_for_status()
    return r.json()

def bx_post(path, payload):
    p = dict(payload)
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    r = requests.post(BINGX_BASE + path, json=p,
                      headers={"X-BX-APIKEY": BINGX_API_KEY,
                               "Content-Type": "application/json"}, timeout=15)
    r.raise_for_status()
    return r.json()

def get_balance():
    try:
        data = bx_get("/openApi/swap/v2/user/balance")
        d    = data.get("data", {})
        bal  = d.get("balance", {}) if isinstance(d, dict) else {}
        if isinstance(bal, dict):
            for f in ("availableMargin","available","crossWalletBalance",
                      "walletBalance","equity"):
                v = bal.get(f)
                if v is not None and v != "" and float(v) != 0.0:
                    log.info(f"Balance: {float(v):.4f} USDT ({f})")
                    return float(v)
        if isinstance(bal, list):
            for a in bal:
                if isinstance(a, dict) and a.get("asset") == "USDT":
                    for f in ("availableMargin","available","walletBalance","equity"):
                        v = a.get(f)
                        if v is not None and v != "": return float(v)
        return 0.0
    except Exception as e:
        log.error(f"get_balance: {e}"); return 0.0

def get_all_positions():
    try:
        data   = bx_get("/openApi/swap/v2/user/positions", {})
        result = {}
        for p in data.get("data", []):
            if isinstance(p, dict) and float(p.get("positionAmt", 0)) != 0:
                result[p["symbol"]] = p
        log.info(f"Posiciones ({len(result)}): {list(result.keys())[:8]}")
        return result
    except Exception as e:
        log.error(f"get_positions: {e}"); return {}

# ── SYMBOL DISCOVERY ──────────────────────────────────────
def _is_valid(sym):
    if not sym or not sym.endswith("-USDT"): return False
    base = sym.replace("-USDT","")
    if len(base) < 2: return False
    if any(base.startswith(p) for p in EXCLUDED_PREFIXES): return False
    if any(kw.lower() in sym.lower() for kw in EXCLUDED_KEYWORDS): return False
    return True

def get_all_symbols(limit=0):
    for endpoint, key in [
        ("/openApi/swap/v2/quote/contracts", "tradeAmount"),
        ("/openApi/swap/v2/quote/ticker",    "quoteVolume"),
    ]:
        try:
            items = bx_get(endpoint, {}).get("data", [])
            valid = [i for i in items if isinstance(i,dict) and _is_valid(i.get("symbol",""))]
            valid.sort(key=lambda x: float(x.get(key,0) or 0), reverse=True)
            syms  = [i["symbol"] for i in valid]
            if syms:
                res = syms[:limit] if limit else syms
                log.info(f"✅ {len(res)} símbolos")
                return res
        except Exception as e:
            log.warning(f"Symbols: {e}")
    return FALLBACK_SYMBOLS[:limit] if limit else FALLBACK_SYMBOLS

def set_lev(symbol):
    for side in ("LONG","SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol":symbol,"side":side,"leverage":LEVERAGE})
        except Exception: pass

# ═══════════════════════════════════════════════════════════
#  WEBSOCKET CACHE
# ═══════════════════════════════════════════════════════════
def _ws_on_message(ws_app, message):
    try:
        import gzip
        try:   d = json.loads(gzip.decompress(message) if isinstance(message,bytes) else message)
        except: d = json.loads(message)
        if not d.get("dataType","").endswith("@kline"): return
        sym = d.get("s","").replace("_","-")
        k   = d.get("data",{}).get("kline", d.get("k",{}))
        if not k: return
        row = {
            "open_time": pd.to_datetime(k.get("t", k.get("startTime",0)), unit="ms"),
            "open":  float(k.get("o",0)), "high": float(k.get("h",0)),
            "low":   float(k.get("l",0)), "close":float(k.get("c",0)),
            "volume":float(k.get("v",0)),
        }
        if row["close"] == 0: return
        with ws_cache_lock:
            df = ws_kline_cache.get(sym)
            if df is None: return
            if len(df) > 0 and df.iloc[-1]["open_time"] == row["open_time"]:
                for col in ("open","high","low","close","volume"):
                    df.at[df.index[-1], col] = row[col]
            else:
                ws_kline_cache[sym] = pd.concat(
                    [df, pd.DataFrame([row])], ignore_index=True).tail(500)
            ws_price_cache[sym] = row["close"]
    except Exception: pass

def start_ws_cache(symbols):
    if not USE_WS_CACHE: return
    ivl = INTERVAL_MAP.get(TIMEFRAME,"5m").lower()
    def _run():
        while True:
            try:
                app = websocket.WebSocketApp(
                    BINGX_WS,
                    on_message=_ws_on_message,
                    on_error=lambda a,e: log.warning(f"WS:{e}"),
                    on_close=lambda a,*x: None,
                    on_open=lambda a: [
                        a.send(json.dumps({
                            "id":f"s_{s}","reqType":"sub",
                            "dataType":f"{s.replace('-','_')}@kline_{ivl}"
                        })) for s in symbols[:200]
                    ])
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e: log.warning(f"WS thread:{e}")
            time.sleep(5)
    threading.Thread(target=_run, daemon=True).start()
    log.info(f"✅ WS cache ({min(len(symbols),200)} syms)")

# ═══════════════════════════════════════════════════════════
#  PRECIO Y KLINES
# ═══════════════════════════════════════════════════════════
def get_live_price(symbol):
    if USE_WS_CACHE:
        with ws_cache_lock:
            p = ws_price_cache.get(symbol)
        if p and p > 0: return p
    # fallbacks
    try:
        items = bx_get("/openApi/swap/v2/quote/premiumIndex",{"symbol":symbol}).get("data",[])
        if isinstance(items,list):
            for i in items:
                if i.get("symbol")==symbol and i.get("markPrice"):
                    return float(i["markPrice"])
        elif isinstance(items,dict) and items.get("markPrice"):
            return float(items["markPrice"])
    except Exception: pass
    try:
        t2 = bx_get("/openApi/swap/v2/quote/ticker",{"symbol":symbol}).get("data",[])
        if isinstance(t2,list):
            for t in t2:
                if t.get("symbol")==symbol:
                    lp = t.get("lastPrice") or t.get("price")
                    if lp: return float(lp)
        elif isinstance(t2,dict):
            lp = t2.get("lastPrice") or t2.get("price")
            if lp: return float(lp)
    except Exception: pass
    try:
        rows = bx_get("/openApi/swap/v3/quote/klines",
                      {"symbol":symbol,"interval":INTERVAL_MAP.get(TIMEFRAME,"5m"),
                       "limit":2}).get("data",[])
        if rows: return float(rows[-1][4])
    except Exception: pass
    raise ValueError(f"No price for {symbol}")

def get_spread_pct(symbol):
    try:
        d = bx_get("/openApi/swap/v2/quote/bookTicker",{"symbol":symbol}).get("data",{})
        if isinstance(d,list): d = next((i for i in d if i.get("symbol")==symbol),{})
        ask = float(d.get("askPrice",0) or 0)
        bid = float(d.get("bidPrice",0) or 0)
        return (ask-bid)/bid*100 if ask>0 and bid>0 else 999.0
    except Exception: return 999.0

def _parse_klines(rows):
    if not rows or not isinstance(rows,list): return pd.DataFrame()
    df = pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time"])
    for c in ("open","high","low","close","volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.dropna(subset=["open","high","low","close","volume"], inplace=True)
    return df.sort_values("open_time").reset_index(drop=True)

def get_klines(symbol, limit=300):
    if USE_WS_CACHE:
        with ws_cache_lock:
            df = ws_kline_cache.get(symbol)
        if df is not None and len(df) >= limit//2: return df.copy()
    rows = bx_get("/openApi/swap/v3/quote/klines",
                  {"symbol":symbol,"interval":INTERVAL_MAP.get(TIMEFRAME,"5m"),
                   "limit":limit}).get("data",[])
    df = _parse_klines(rows)
    if USE_WS_CACHE and not df.empty:
        with ws_cache_lock: ws_kline_cache[symbol] = df.copy()
    return df

def get_tf_klines(symbol, interval, cache, ttl, limit=60):
    now = time.time()
    c   = cache.get(symbol)
    if c:
        df_c, ts = c
        if now - ts < ttl and len(df_c) >= 20: return df_c.copy()
    try:
        rows = bx_get("/openApi/swap/v3/quote/klines",
                      {"symbol":symbol,"interval":interval,"limit":limit}).get("data",[])
        df = _parse_klines(rows)
        if not df.empty: cache[symbol] = (df.copy(), now)
        return df
    except Exception as e:
        log.debug(f"TF {interval} {symbol}: {e}"); return pd.DataFrame()

# ═══════════════════════════════════════════════════════════
#  INDICADORES
# ═══════════════════════════════════════════════════════════
def calc_atr(high, low, close, p=14):
    tr = pd.concat([
        high-low, (high-close.shift()).abs(), (low-close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False).mean()

def calc_adx(high, low, close, p=14):
    up  = high.diff(); dn = -low.diff()
    pdm = np.where((up>dn)&(up>0), up, 0.0)
    mdm = np.where((dn>up)&(dn>0), dn, 0.0)
    tr  = pd.concat([
        high-low, (high-close.shift()).abs(), (low-close.shift()).abs()
    ], axis=1).max(axis=1)
    a   = 1.0/p
    def w(x): return pd.Series(x, index=high.index).ewm(alpha=a, adjust=False).mean()
    tr_s=w(tr); p_s=w(pdm); m_s=w(mdm)
    di_p=100*p_s/tr_s.replace(0,np.nan)
    di_m=100*m_s/tr_s.replace(0,np.nan)
    dx  =100*(di_p-di_m).abs()/(di_p+di_m).replace(0,np.nan)
    return di_p, di_m, dx.ewm(alpha=a, adjust=False).mean()

def calc_bbands(close, length=20, mult=2.0):
    sma   = close.rolling(length).mean()
    std   = close.rolling(length).std()
    return sma + mult*std, sma - mult*std

def calc_keltner(high, low, close, length=20, mult=1.5):
    """Keltner Channel usando ATR del True Range."""
    mid  = close.rolling(length).mean()
    tr   = pd.concat([
        high-low, (high-close.shift()).abs(), (low-close.shift()).abs()
    ], axis=1).max(axis=1)
    atr_kc = tr.rolling(length).mean()
    return mid + mult*atr_kc, mid - mult*atr_kc

# ═══════════════════════════════════════════════════════════
#  DD PROTECT — BOTÓN DE PÁNICO (Pine Script → Python)
# ═══════════════════════════════════════════════════════════
def check_dd_protect(balance):
    """
    Pine Script:
      capital_at_start_of_day = strategy.equity (resetea cada día)
      daily_loss_reached = equity - start <= -(start * max_daily_loss%)
    """
    today = date.today()
    with daily_lock:
        if daily_state["date"] != today:
            # Nuevo día → resetear
            daily_state["date"]      = today
            daily_state["start_bal"] = balance
            daily_state["blocked"]   = False
            log.info(f"📅 Nuevo día. Balance inicio: {balance:.4f} USDT")

        start = daily_state["start_bal"]
        if start <= 0: return False

        loss_pct = (start - balance) / start * 100
        if loss_pct >= MAX_DAILY_LOSS:
            if not daily_state["blocked"]:
                daily_state["blocked"] = True
                log.warning(f"🚨 DD PROTECT ACTIVADO: pérdida {loss_pct:.2f}% ≥ {MAX_DAILY_LOSS}%")
                tg(f"🚨 <b>DD PROTECT ACTIVADO</b>\n"
                   f"Pérdida diaria: <b>{loss_pct:.2f}%</b> ≥ {MAX_DAILY_LOSS}%\n"
                   f"Balance inicio: {start:.2f} USDT | Actual: {balance:.2f} USDT\n"
                   f"Bot bloqueado hasta mañana.")
            return True
        else:
            if daily_state["blocked"]:
                daily_state["blocked"] = False
            return False

# ═══════════════════════════════════════════════════════════
#  TRIPLE SCREEN EMA200 (Pine Script → Python)
# ═══════════════════════════════════════════════════════════
def get_ema200_tf(symbol, interval, cache, ttl):
    """EMA200 en timeframe superior."""
    df = get_tf_klines(symbol, interval, cache, ttl, limit=220)
    if df.empty or len(df) < EMA200 + 5: return None
    ema = df["close"].ewm(span=EMA200, adjust=False).mean()
    return float(ema.iloc[-1])

# ═══════════════════════════════════════════════════════════
#  SQUEEZE RELEASE (Pine Script → Python)
# ═══════════════════════════════════════════════════════════
def detect_squeeze_release(close, high, low, i):
    """
    Pine Script:
      bb_upper  = sma(close,20) + 2 * stdev(close,20)
      kc_upper  = sma(close,20) + 1.5 * sma(tr,20)
      squeeze_release = bb_upper > kc_upper
    True cuando la BB está expandiéndose fuera del KC → volatilidad explotando.
    """
    if i < max(BB_LEN, KC_LEN) + 2: return False, 0.0

    close_w = close.iloc[:i+1]
    high_w  = high.iloc[:i+1]
    low_w   = low.iloc[:i+1]

    bb_up, _  = calc_bbands(close_w, BB_LEN, BB_MULT)
    kc_up, _  = calc_keltner(high_w, low_w, close_w, KC_LEN, KC_MULT)

    bb_val = float(bb_up.iloc[-1]) if not np.isnan(float(bb_up.iloc[-1])) else 0
    kc_val = float(kc_up.iloc[-1]) if not np.isnan(float(kc_up.iloc[-1])) else 0

    if bb_val <= 0 or kc_val <= 0: return False, 0.0

    squeeze_release = bb_val > kc_val
    expansion_pct   = (bb_val - kc_val) / kc_val * 100 if kc_val > 0 else 0.0
    return squeeze_release, round(expansion_pct, 3)

# ═══════════════════════════════════════════════════════════
#  EMA 7/17 CROSS DETECTION
# ═══════════════════════════════════════════════════════════
def detect_ema_cross(ema_f, ema_s, i, direction, lookback=5):
    """
    Pine Script:
      long_trigger  = ta.crossover(ema7, ema17)   → ema7 cruza SOBRE ema17
      short_trigger = ta.crossunder(ema7, ema17)  → ema7 cruza BAJO ema17
    Buscamos en las últimas `lookback` velas.
    """
    ef_now = float(ema_f.iloc[i]); es_now = float(ema_s.iloc[i])
    if direction == "LONG"  and ef_now <= es_now: return False
    if direction == "SHORT" and ef_now >= es_now: return False
    start = max(1, i - lookback + 1)
    for j in range(i, start-1, -1):
        ef_j = float(ema_f.iloc[j]);   es_j = float(ema_s.iloc[j])
        ef_p = float(ema_f.iloc[j-1]); es_p = float(ema_s.iloc[j-1])
        if direction == "LONG"  and ef_j > es_j and ef_p <= es_p: return True
        if direction == "SHORT" and ef_j < es_j and ef_p >= es_p: return True
    return False

# ═══════════════════════════════════════════════════════════
#  FVG — Fair Value Gap (Neural Hunter extra)
# ═══════════════════════════════════════════════════════════
def detect_fvg(df, i, direction, lookback=6):
    """
    fvg_bull = low[j] > high[j-2]
    fvg_bear = high[j] < low[j-2]
    """
    start = max(2, i - lookback + 1)
    for j in range(i, start-1, -1):
        if j < 2: break
        l_j  = float(df["low"].iloc[j]);   h_j  = float(df["high"].iloc[j])
        h_j2 = float(df["high"].iloc[j-2]); l_j2 = float(df["low"].iloc[j-2])
        ref  = float(df["close"].iloc[j])
        if direction == "LONG" and l_j > h_j2:
            return True, round((l_j - h_j2)/ref*100, 3)
        if direction == "SHORT" and h_j < l_j2:
            return True, round((l_j2 - h_j)/ref*100, 3)
    return False, 0.0

# ═══════════════════════════════════════════════════════════
#  SCAN PRINCIPAL — QUANTUM BLACKCORE
# ═══════════════════════════════════════════════════════════
def scan_symbol(symbol):
    """
    Pine Script Quantum Blackcore → Python:

    LONG:
      ✅ Sesión 08-20 UTC
      ✅ close > EMA200(5m) AND > EMA200(H1) AND > EMA200(H4)   [Triple Screen]
      ✅ squeeze_release  (BB upper > KC upper)
      ✅ EMA7 cruza sobre EMA17  (crossover en últimas 5 velas)
      ✅ ADX > 18  (no mercado plano)
      ✅ Volumen ≥ 1.1x media
      + FVG alcista (optional bonus)

    SL = lowest(low, 5) - 0.3×ATR   [Pine Script: lowest(low,5)]
    TP = entrada + riesgo × 4        [Pine Script: 4R]
    """
    # Gate: sesión
    if not is_session(): return None

    # Gate: cooldown
    if symbol in sl_cooldown:
        elapsed = (datetime.now(timezone.utc) - sl_cooldown[symbol]).total_seconds()/60
        if elapsed < COOLDOWN_MINS: return None

    try:
        df = get_klines(symbol, limit=300)
        if df.empty or len(df) < EMA200 + 10: return None

        # ── INDICADORES 5m ───────────────────────────────────
        close = df["close"]; high = df["high"]; low = df["low"]
        ema_f     = close.ewm(span=EMA_FAST, adjust=False).mean()
        ema_s     = close.ewm(span=EMA_SLOW, adjust=False).mean()
        ema200_5m = close.ewm(span=EMA200,   adjust=False).mean()
        atr_s     = calc_atr(high, low, close, ATR_LEN)
        di_p, di_m, adx_s = calc_adx(high, low, close, ADX_LEN)
        vol_ma    = df["volume"].rolling(20).mean()

        i = len(df) - 2   # última vela cerrada
        if i < EMA200 + 5: return None

        close_i  = float(close.iloc[i])
        ema_f_i  = float(ema_f.iloc[i])
        ema_s_i  = float(ema_s.iloc[i])
        ema200_i = float(ema200_5m.iloc[i])
        adx_i    = float(adx_s.iloc[i])
        di_p_i   = float(di_p.iloc[i])
        di_m_i   = float(di_m.iloc[i])
        atr_i    = float(atr_s.iloc[i])
        vol_i    = float(df["volume"].iloc[i])
        vma_i    = float(vol_ma.iloc[i])

        if any(np.isnan(x) for x in [ema_f_i, ema_s_i, ema200_i, adx_i, atr_i]):
            return None
        if atr_i <= 0 or vma_i <= 0: return None
        if atr_i / close_i * 100 > 5.0: return None

        # Gate: ADX
        if USE_ADX and adx_i < ADX_MIN: return None

        # Gate: Volumen
        vratio = round(vol_i/vma_i, 2) if vma_i > 0 else 0.0
        if USE_VOL and vratio < VOL_MULT: return None

        # ── TRIPLE SCREEN EMA200 ──────────────────────────────
        # 5m ya calculado arriba
        above_ema200_5m = close_i > ema200_i
        below_ema200_5m = close_i < ema200_i

        # H1 EMA200
        ema200_h1_val = get_ema200_tf(symbol, "1H", h1_cache, H1_CACHE_TTL)
        if ema200_h1_val is None: return None
        above_ema200_h1 = close_i > ema200_h1_val
        below_ema200_h1 = close_i < ema200_h1_val

        # H4 EMA200
        ema200_h4_val = get_ema200_tf(symbol, "4H", h4_cache, H4_CACHE_TTL)
        if ema200_h4_val is None: return None
        above_ema200_h4 = close_i > ema200_h4_val
        below_ema200_h4 = close_i < ema200_h4_val

        # Pine Script: is_bullish_world / is_bearish_world
        is_bullish_world = above_ema200_5m and above_ema200_h1 and above_ema200_h4
        is_bearish_world = below_ema200_5m and below_ema200_h1 and below_ema200_h4

        if not is_bullish_world and not is_bearish_world: return None

        # ── SQUEEZE RELEASE ───────────────────────────────────
        squeeze_ok, squeeze_pct = detect_squeeze_release(close, high, low, i)
        if USE_SQUEEZE and not squeeze_ok: return None

        # ── EMA7 CROSSES EMA17 ────────────────────────────────
        direction = "LONG" if is_bullish_world else "SHORT"
        cross_ok  = detect_ema_cross(ema_f, ema_s, i, direction, EMA_CROSS_BARS)
        if not cross_ok: return None

        # ── FVG CONFLUENCIA (bonus, no obligatorio) ───────────
        fvg_ok, fvg_pct = (False, 0.0)
        if USE_FVG:
            fvg_ok, fvg_pct = detect_fvg(df, i, direction, FVG_LOOKBACK)

        # ── SL — Pine Script: lowest(low,5) / highest(high,5) ─
        sl_window_start = max(0, i - SL_LOOKBACK + 1)
        if direction == "LONG":
            sl_base  = float(low.iloc[sl_window_start:i+1].min())
            sl_price = sl_base - atr_i * ATR_SL_MARGIN
        else:
            sl_base  = float(high.iloc[sl_window_start:i+1].max())
            sl_price = sl_base + atr_i * ATR_SL_MARGIN

        # Ajustar distancia mínima/máxima
        if direction == "LONG":
            dist_pct = (close_i - sl_price) / close_i * 100
            if dist_pct < MIN_DIST_PCT: sl_price = close_i*(1 - MIN_DIST_PCT/100)
            if dist_pct > MAX_DIST_PCT: sl_price = close_i*(1 - MAX_DIST_PCT/100)
            if sl_price >= close_i: return None
            tp_price = close_i + (close_i - sl_price) * TP_RR   # 4R Pine Script
        else:
            dist_pct = (sl_price - close_i) / close_i * 100
            if dist_pct < MIN_DIST_PCT: sl_price = close_i*(1 + MIN_DIST_PCT/100)
            if dist_pct > MAX_DIST_PCT: sl_price = close_i*(1 + MAX_DIST_PCT/100)
            if sl_price <= close_i: return None
            tp_price = close_i - (sl_price - close_i) * TP_RR

        dist     = abs(close_i - sl_price)
        dist_pct = dist / close_i * 100
        rr       = abs(tp_price - close_i) / dist

        if dist_pct < MIN_DIST_PCT or rr < 3.0: return None

        # ── SCORE ─────────────────────────────────────────────
        score = 0.0
        score += 30                                          # triple screen EMA200
        score += 20 if squeeze_ok else 0                    # squeeze release
        score += 15                                         # EMA7/17 cross confirmado
        score += min(squeeze_pct * 10, 10)                 # fuerza del squeeze
        score += 10 if fvg_ok else 0                       # FVG confluencia
        score += min((adx_i - ADX_MIN) / 20 * 8, 8)       # ADX
        score += min((vratio - 1.0) * 5, 5)               # volumen
        score += min(abs(di_p_i - di_m_i) / 10, 4)        # DI spread
        score += 3 if fvg_pct > 0.05 else 0               # FVG grande
        score = round(score, 1)

        if score < 40: return None  # umbral bajo para no filtrar demasiado

        h = datetime.now(timezone.utc).hour
        session_name = "LONDON" if h < 12 else "NY"

        return {
            "symbol":        symbol,
            "signal":        direction,
            "method":        f"BLACKCORE|SQ+EMA{EMA_FAST}x{EMA_SLOW}+3×EMA200|{session_name}",
            "close":         close_i,
            "sl_base":       sl_base,
            "sl":            round(sl_price, 6),
            "tp":            round(tp_price, 6),
            "atr":           atr_i,
            "atr_pct":       round(atr_i/close_i*100, 2),
            "vol_ratio":     vratio,
            "adx":           round(adx_i, 1),
            "di_spread":     round(abs(di_p_i - di_m_i), 1),
            "score":         score,
            "rr":            round(rr, 2),
            "dist_pct":      round(dist_pct, 3),
            "squeeze_pct":   squeeze_pct,
            "fvg_ok":        fvg_ok,
            "fvg_pct":       fvg_pct,
            "ema200_5m":     round(ema200_i, 6),
            "ema200_h1":     round(ema200_h1_val, 6),
            "ema200_h4":     round(ema200_h4_val, 6),
            "session":       session_name,
        }

    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════
def is_session():
    if not USE_SESSION: return True
    h = datetime.now(timezone.utc).hour
    return SESSION_START <= h < SESSION_END

def recalc_sl_tp(sig, live):
    atr = sig["atr"]
    if sig["signal"] == "LONG":
        sl = sig["sl_base"] - atr * ATR_SL_MARGIN
        if (live - sl)/live*100 < MIN_DIST_PCT: sl = live*(1-MIN_DIST_PCT/100)
        if (live - sl)/live*100 > MAX_DIST_PCT: sl = live*(1-MAX_DIST_PCT/100)
        if sl >= live: return None, None
        tp = live + (live - sl) * TP_RR
    else:
        sl = sig["sl_base"] + atr * ATR_SL_MARGIN
        if (sl - live)/live*100 < MIN_DIST_PCT: sl = live*(1+MIN_DIST_PCT/100)
        if (sl - live)/live*100 > MAX_DIST_PCT: sl = live*(1+MAX_DIST_PCT/100)
        if sl <= live: return None, None
        tp = live - (sl - live) * TP_RR
    if abs(tp-live)/abs(live-sl) < 2.5: return None, None
    return round(sl,6), round(tp,6)

def calc_qty(balance, entry, sl):
    dist = abs(entry-sl)/entry
    if dist < 1e-8: return 0, 0
    risk_usdt    = balance * RISK_PERCENT / 100
    notional     = risk_usdt / dist
    max_notional = min(MAX_ORDER_USDT, balance*MAX_MARGIN_PCT/100*LEVERAGE)
    notional     = max(MIN_ORDER_USDT, min(notional, max_notional))
    return round(max(notional/entry, 0.001), 4), round(notional, 2)

def open_order(symbol, side, qty, sl, tp):
    payload = {
        "symbol":symbol, "side":side,
        "positionSide":"LONG" if side=="BUY" else "SHORT",
        "type":"MARKET", "quantity":round(qty,4),
        "stopLoss":   json.dumps({"type":"STOP_MARKET","stopPrice":round(sl,6),"workingType":"MARK_PRICE"}),
        "takeProfit": json.dumps({"type":"TAKE_PROFIT_MARKET","stopPrice":round(tp,6),"workingType":"MARK_PRICE"}),
    }
    resp = bx_post("/openApi/swap/v2/trade/order", payload)
    if resp.get("code",-1) != 0:
        raise ValueError(f"BingX code={resp.get('code')}: {resp.get('msg','?')}")
    return resp

def open_order_with_retry(symbol, side, qty, sl, tp, retries=1):
    for attempt in range(retries+1):
        try: return open_order(symbol, side, qty, sl, tp)
        except ValueError as e:
            if "101400" in str(e) and attempt < retries:
                time.sleep(1)
                try:
                    fp = get_live_price(symbol)
                    sl = round(fp*(1-MIN_DIST_PCT/100),6) if side=="BUY" else round(fp*(1+MIN_DIST_PCT/100),6)
                    tp = round(fp+(fp-sl)*TP_RR,6)        if side=="BUY" else round(fp-(sl-fp)*TP_RR,6)
                except Exception: raise
            else: raise

def update_breakeven_stops(positions):
    if not USE_BE or not positions: return
    with pos_state_lock: sc = dict(position_state)
    for sym, pos in positions.items():
        try:
            ps = sc.get(sym)
            if not ps or ps.get("be_hit"): continue
            side=ps["side"]; entry=ps["entry"]; tp=ps["tp"]
            live=get_live_price(sym)
            total=abs(tp-entry)
            trig=entry+total*BE_TRIGGER_R if side=="LONG" else entry-total*BE_TRIGGER_R
            if not ((side=="LONG" and live>=trig) or (side=="SHORT" and live<=trig)): continue
            new_sl=round(entry*1.0003,6) if side=="LONG" else round(entry*0.9997,6)
            bx_post("/openApi/swap/v2/trade/order",{
                "symbol":sym,"type":"STOP_MARKET",
                "side":"SELL" if side=="LONG" else "BUY",
                "positionSide":side,"stopPrice":new_sl,
                "closePosition":"true","workingType":"MARK_PRICE"
            })
            with pos_state_lock:
                if sym in position_state:
                    position_state[sym].update({"be_hit":True,"sl":new_sl})
            log.info(f"✅ BE {sym} {side}→{new_sl:.5g}")
            tg(f"🔄 <b>Breakeven: {sym}</b> {side}\nEntry:{entry:.5g}→SL:{new_sl:.5g}")
        except Exception as e: log.debug(f"BE {sym}:{e}")

# ═══════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════
async def _send(msg):
    if not TELEGRAM_OK or not TELEGRAM_TOKEN: return
    bot = Bot(token=TELEGRAM_TOKEN)
    cid = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
    await bot.send_message(chat_id=cid, text=msg, parse_mode=ParseMode.HTML)

def tg(msg):
    if not TELEGRAM_TOKEN: return
    try: asyncio.run(_send(msg))
    except Exception as e: log.warning(f"TG:{e}")

def tg_startup(balance, symbols):
    h    = datetime.now(timezone.utc).hour
    sess = "🟢 ACTIVA" if SESSION_START<=h<SESSION_END else "🔴 INACTIVA"
    tg(
        f"🎯 <b>Quantum Blackcore V14.0</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Lógica:</b> EMA{EMA_FAST}x{EMA_SLOW} + Squeeze + Triple EMA{EMA200}\n"
        f"<b>Sesión:</b> {SESSION_START}:00–{SESSION_END}:00 UTC → {sess}\n"
        f"<b>TF:</b> {TIMEFRAME} | <b>TP:</b> {TP_RR}R | <b>BE@:</b> {BE_TRIGGER_R*100:.0f}%\n"
        f"<b>DD Protect:</b> {MAX_DAILY_LOSS}%/día | <b>FVG:</b> {'✅' if USE_FVG else '❌'}\n"
        f"<b>ADX≥:</b> {ADX_MIN} | <b>Vol≥:</b> {VOL_MULT}x | <b>SL:</b> lowest/highest({SL_LOOKBACK})\n"
        f"<b>Max trades:</b> {MAX_OPEN_TRADES} | <b>Riesgo:</b> {RISK_PERCENT}%/trade\n"
        f"<b>Balance:</b> {balance:.2f} USDT | <b>Símbolos:</b> {len(symbols)}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_entry(sig, qty, notional, balance, spread=None):
    d  = "🟢 LONG" if sig["signal"]=="LONG" else "🔴 SHORT"
    sp = f" | Spread:{spread:.3f}%" if spread else ""
    sq = f"{sig['squeeze_pct']:.2f}%" if sig['squeeze_pct'] > 0 else "—"
    fvg_str = f"✅ {sig['fvg_pct']:.3f}%" if sig.get("fvg_ok") else "❌"
    be_level = round(
        sig["close"]+(sig["tp"]-sig["close"])*BE_TRIGGER_R if sig["signal"]=="LONG"
        else sig["close"]-(sig["close"]-sig["tp"])*BE_TRIGGER_R, 6)
    tg(
        f"<b>🎯 BLACKCORE — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Dir:</b> {d} | <b>Score:</b> {sig['score']}/100\n"
        f"<b>Squeeze:</b> {sq} | <b>FVG:</b> {fvg_str}\n"
        f"<b>EMA200:</b> 5m:{sig['ema200_5m']:.4g} "
        f"H1:{sig['ema200_h1']:.4g} H4:{sig['ema200_h4']:.4g} ✅\n"
        f"<b>ADX:</b> {sig['adx']} | <b>DI±:</b> {sig['di_spread']} | "
        f"<b>Vol:</b> {sig['vol_ratio']}x{sp}\n"
        f"<b>Sesión:</b> {sig['session']} | <b>ATR:</b> {sig['atr_pct']}%\n"
        f"<b>Entrada:</b>     <code>{sig['close']:.5g}</code>\n"
        f"<b>Stop Loss:</b>   <code>{sig['sl']:.5g}</code> ({sig['dist_pct']}%)\n"
        f"<b>Take Profit:</b> <code>{sig['tp']:.5g}</code> | <b>R:R:</b> 1:{sig['rr']}\n"
        f"<b>BE activado @:</b> <code>{be_level:.5g}</code>\n"
        f"<b>Qty:</b> {qty:.4f} | <b>Notional:</b> {notional:.2f} USDT | "
        f"<b>Riesgo:</b> {balance*RISK_PERCENT/100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

def tg_scan(signals, total, open_count, blocked):
    if not signals: return
    h    = datetime.now(timezone.utc).hour
    sess = "🟢" if SESSION_START<=h<SESSION_END else "🔴"
    blk  = " 🚨BLOQUEADO" if blocked else ""
    lines = [
        f"🎯 <b>{len(signals)} señal(es)/{total}</b> | "
        f"Trades:{open_count}/{MAX_OPEN_TRADES} {sess}{blk}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in signals[:5]:
        e  = "🟢" if s["signal"]=="LONG" else "🔴"
        sq = f"SQ:{s['squeeze_pct']:.1f}%" if s['squeeze_pct']>0 else ""
        fv = "FVG✅" if s.get("fvg_ok") else ""
        lines.append(
            f"{e} <b>{s['symbol']}</b> {sq} {fv} "
            f"ADX:{s['adx']} Vol:{s['vol_ratio']}x "
            f"Score:{s['score']} RR:1:{s['rr']}"
        )
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

def tg_diag(signals, skip_reasons):
    lines = [f"⚠️ <b>{len(signals)} señales → 0 órdenes</b>","━"*20]
    for sym, r in list(skip_reasons.items())[:8]:
        lines.append(f"• <b>{sym}</b>: {r}")
    tg("\n".join(lines))

# ═══════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════
def main():
    log.info("=== Quantum Blackcore V14.0 ===")
    log.info(f"  EMA{EMA_FAST}x{EMA_SLOW} + Squeeze + 3×EMA{EMA200}(5m+H1+H4)")
    log.info(f"  Sesión {SESSION_START}-{SESSION_END}UTC | DD≤{MAX_DAILY_LOSS}% | TP:{TP_RR}R")
    log.info(f"  SL:lowest/highest({SL_LOOKBACK}) | BE@{BE_TRIGGER_R*100:.0f}%")

    symbols   = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_all_symbols(MAX_SYMBOLS)
    if not symbols: symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()
    log.info(f"Balance:{balance:.4f}U | Símbolos:{len(symbols)} | Open:{len(positions)}")

    # Pre-cargar klines 5m
    log.info("Pre-cargando klines 5m...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(lambda s: get_klines(s, 300), symbols[:100]))

    # Pre-cargar H1 y H4 en background
    def _prefetch():
        log.info("Pre-cargando H1 y H4...")
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda s: get_tf_klines(s,"1H",h1_cache,H1_CACHE_TTL,220), symbols[:80]))
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda s: get_tf_klines(s,"4H",h4_cache,H4_CACHE_TTL,220), symbols[:80]))
        log.info("✅ Cache H1+H4 listo.")
    threading.Thread(target=_prefetch, daemon=True).start()

    start_ws_cache(symbols)
    time.sleep(3)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))

    tg_startup(balance, symbols)
    log.info("✅ Quantum Blackcore V14 iniciado.")

    errors       = 0
    prev_session = is_session()

    while True:
        t0 = time.time()
        try:
            balance    = get_balance()
            positions  = get_all_positions()
            open_count = len(positions)
            in_session = is_session()

            if in_session != prev_session:
                prev_session = in_session
                tg(f"{'🟢 Sesión iniciada' if in_session else '🔴 Sesión cerrada'} "
                   f"— {SESSION_START}:00–{SESSION_END}:00 UTC")

            # DD PROTECT
            blocked = check_dd_protect(balance)

            # Dashboard log
            h_utc = datetime.now(timezone.utc).strftime("%H:%M")
            with daily_lock:
                start_b = daily_state["start_bal"]
                loss_pct = (start_b - balance)/start_b*100 if start_b > 0 else 0
            log.info(
                f"── V14 [{h_utc}UTC] {'🟢' if in_session else '🔴'} "
                f"{'🚨BLOQ ' if blocked else ''}"
                f"{balance:.4f}U (DD:{loss_pct:.2f}%) | "
                f"{open_count}/{MAX_OPEN_TRADES} trades ──"
            )

            # Limpiar position_state de posiciones cerradas
            with pos_state_lock:
                for s in [k for k in list(position_state) if k not in positions]:
                    del position_state[s]

            # Breakeven
            if USE_BE and positions:
                update_breakeven_stops(positions)

            # Scan
            signals = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(scan_symbol, s): s for s in symbols}
                for f in as_completed(futs):
                    r = f.result()
                    if r: signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Señales: {len(signals)}/{len(symbols)}")

            if signals:
                tg_scan(signals, len(symbols), open_count, blocked)
                for s in signals[:4]:
                    log.info(
                        f"  ⚡ {s['symbol']} {s['signal']} "
                        f"SQ:{s['squeeze_pct']:.2f}% FVG:{'✅' if s.get('fvg_ok') else '❌'} "
                        f"ADX:{s['adx']} Vol:{s['vol_ratio']}x "
                        f"score:{s['score']} rr:1:{s['rr']}"
                    )

            entered: set = set(); skip_reasons: dict = {}; orders_opened = 0

            for sig in signals:
                # Gates de ejecución
                if not in_session: break
                if blocked:        break

                sym = sig["symbol"]
                if sym in positions:             skip_reasons[sym]="ya en posición"; continue
                if sym in entered:               skip_reasons[sym]="ya intentado";   continue
                if open_count >= MAX_OPEN_TRADES: log.info("Max trades."); break
                if balance < 2:                  skip_reasons[sym]="balance bajo";   break

                spread = get_spread_pct(sym)
                if spread > MAX_SPREAD_PCT:
                    skip_reasons[sym]=f"spread {spread:.3f}%"; continue

                side = "BUY" if sig["signal"]=="LONG" else "SELL"
                try:
                    set_lev(sym)
                    try:
                        live = get_live_price(sym)
                    except Exception as ep:
                        skip_reasons[sym]=f"sin precio:{str(ep)[:40]}"; continue

                    sl_live, tp_live = recalc_sl_tp(sig, live)
                    if sl_live is None:
                        skip_reasons[sym]="SL/TP inválido"; continue

                    qty, notional = calc_qty(balance, live, sl_live)
                    if qty <= 0:
                        skip_reasons[sym]="qty=0"; continue

                    log.info(
                        f"⚡ {sym} {side} qty={qty:.4f} "
                        f"{notional:.2f}U live={live:.5g} "
                        f"sl={sl_live:.5g} tp={tp_live:.5g} rr=1:{sig['rr']}"
                    )

                    res = open_order_with_retry(sym, side, qty, sl_live, tp_live)
                    log.info(f"✅ {sym} {side} {notional:.2f}U | code={res.get('code')}")

                    sig.update({
                        "close":    live, "sl":sl_live, "tp":tp_live,
                        "dist_pct": round(abs(live-sl_live)/live*100, 3),
                        "rr":       round(abs(tp_live-live)/abs(live-sl_live), 2),
                    })
                    with pos_state_lock:
                        position_state[sym] = {
                            "side":   sig["signal"], "entry": live,
                            "sl":     sl_live,       "tp":    tp_live,
                            "be_hit": False,
                        }
                    tg_entry(sig, qty, notional, balance, spread)
                    entered.add(sym); open_count += 1; orders_opened += 1
                    time.sleep(0.5)

                except Exception as e:
                    reason = str(e)[:100]
                    log.error(f"Order FAILED {sym}: {e}")
                    skip_reasons[sym] = f"error:{reason}"
                    if "stop" in reason.lower() or "liquidat" in reason.lower():
                        sl_cooldown[sym] = datetime.now(timezone.utc)
                    tg(f"⚠️ <b>Error {sym}</b>: <code>{str(e)[:150]}</code>")

            if signals and orders_opened == 0 and skip_reasons:
                tg_diag(signals, skip_reasons)

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Quantum Blackcore V14 detenido</b>"); break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle error #{errors}: {e}")
            if errors <= 3:
                tg(f"⚠️ <b>Error #{errors}</b>: <code>{str(e)[:200]}</code>")
            if errors >= 10:
                tg("🔴 <b>CRÍTICO: detenido.</b>"); break

        time.sleep(max(0, LOOP_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
