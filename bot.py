"""
EMA Slope + ADX + Multi-Timeframe Elite V13.0 — APEX EDITION

MEJORAS V13 sobre V12:
  1. TRIPLE TIMEFRAME (5m + 15m + 1H): los 3 TF deben alinearse
  2. PARTIAL TP: cierra 50% a 1.5R, mueve SL a BE, deja correr el 50% a 3R
  3. SWING-POINT SL: SL en el último swing low/high real (no solo ATR)
  4. RISK MANAGER: controla pérdidas diarias, racha de pérdidas y tamaño dinámico
  5. MARKET REGIME (BTC): solo longs en bull, solo shorts en bear del BTC en 1H
  6. SESSION FILTER: evita 00:00-05:00 UTC (baja liquidez)
  7. CORRELATION FILTER: máx 2 trades en la misma dirección simultáneamente
  8. VWAP FILTER: precio sobre/bajo VWAP para confirmar bias
  9. STATS ENGINE: win rate, PnL diario, mejor/peor trade — Telegram diario
 10. VOLUME DELTA: confirma presión compradora/vendedora en la vela señal
"""

import os, time, hmac, hashlib, json, asyncio, logging, threading
from datetime import datetime, timezone, timedelta
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

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ["BINGX_SECRET_KEY"]
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Timeframes
TIMEFRAME        = os.environ.get("TIMEFRAME",   "5m")
TF_MID           = os.environ.get("TF_MID",      "15m")   # V13: intermedio
TF_SLOW          = os.environ.get("TF_SLOW",     "1h")    # V13: tendencia
TF_MID_CACHE_TTL = int(os.environ.get("TF_MID_CACHE_TTL", "120"))
H1_CACHE_TTL     = int(os.environ.get("H1_CACHE_TTL",     "300"))

# Loop y workers
LOOP_SECONDS     = int(os.environ.get("LOOP_SECONDS",    "30"))
SCAN_WORKERS     = int(os.environ.get("SCAN_WORKERS",    "20"))
MAX_SYMBOLS      = int(os.environ.get("MAX_SYMBOLS",     "0"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES", "9"))

# EMA 5m
EMA_FAST         = int(os.environ.get("EMA_FAST",    "7"))
EMA_SLOW         = int(os.environ.get("EMA_SLOW",    "17"))
EMA_TREND        = int(os.environ.get("EMA_TREND",   "100"))
SLOPE_LIMIT      = float(os.environ.get("SLOPE_LIMIT","30.0"))
SLOPE_LOOK       = int(os.environ.get("SLOPE_LOOK",  "3"))

# EMA 15m (intermedio)
MID_EMA_FAST     = int(os.environ.get("MID_EMA_FAST",  "8"))
MID_EMA_SLOW     = int(os.environ.get("MID_EMA_SLOW",  "21"))
MID_EMA_TREND    = int(os.environ.get("MID_EMA_TREND", "50"))

# EMA H1 (tendencia)
H1_EMA_FAST      = int(os.environ.get("H1_EMA_FAST",  "7"))
H1_EMA_SLOW      = int(os.environ.get("H1_EMA_SLOW",  "17"))

# ADX
ADX_LEN          = int(os.environ.get("ADX_LEN",  "14"))
ADX_MIN          = float(os.environ.get("ADX_MIN", "25.0"))
USE_ADX          = os.environ.get("USE_ADX", "true").lower() == "true"
USE_DI           = os.environ.get("USE_DI",  "true").lower() == "true"

# RSI
RSI_LEN          = int(os.environ.get("RSI_LEN", "14"))
RSI_OB           = float(os.environ.get("RSI_OB", "70.0"))
RSI_OS           = float(os.environ.get("RSI_OS", "30.0"))
USE_RSI          = os.environ.get("USE_RSI", "true").lower() == "true"

# Volumen
USE_VOL          = os.environ.get("USE_VOL",  "true").lower() == "true"
VOL_MULT         = float(os.environ.get("VOL_MULT", "1.2"))
USE_VOL_DELTA    = os.environ.get("USE_VOL_DELTA", "true").lower() == "true"

# VWAP
USE_VWAP         = os.environ.get("USE_VWAP", "true").lower() == "true"

# ATR / SL / TP
ATR_LEN          = int(os.environ.get("ATR_LEN",      "14"))
TP_MULT          = float(os.environ.get("TP_MULT",    "3.0"))
SL_ATR_MULT      = float(os.environ.get("SL_ATR_MULT","1.5"))
MIN_DIST_PCT     = float(os.environ.get("MIN_DIST_PCT","0.50"))
MIN_RR           = float(os.environ.get("MIN_RR",     "2.5"))
USE_SWING_SL     = os.environ.get("USE_SWING_SL", "true").lower() == "true"
SWING_LOOK       = int(os.environ.get("SWING_LOOK", "10"))  # velas para buscar swing

# Partial TP (V13)
USE_PARTIAL_TP   = os.environ.get("USE_PARTIAL_TP", "true").lower() == "true"
PARTIAL_TP_R     = float(os.environ.get("PARTIAL_TP_R",    "1.5"))  # cerrar 50% a 1.5R
PARTIAL_TP_PCT   = float(os.environ.get("PARTIAL_TP_PCT",  "50"))   # % a cerrar
BE_MARGIN_PCT    = float(os.environ.get("BE_MARGIN_PCT",   "0.05")) # % margen sobre entrada

# Breakeven
BE_R_MULT        = float(os.environ.get("BE_R_MULT", "1.5"))

# Filtros de calidad
MIN_SCORE        = float(os.environ.get("MIN_SCORE",       "55.0"))
MAX_SPREAD_PCT   = float(os.environ.get("MAX_SPREAD_PCT",  "0.15"))
ATR_MAX_PCT      = float(os.environ.get("ATR_MAX_PCT",     "3.5"))
MAX_ENTRY_DRIFT  = float(os.environ.get("MAX_ENTRY_DRIFT", "0.30"))

# Patrones de vela
USE_CANDLE_PATTERNS = os.environ.get("USE_CANDLE_PATTERNS", "true").lower() == "true"
PIN_BAR_RATIO       = float(os.environ.get("PIN_BAR_RATIO",    "0.30"))
PIN_TAIL_RATIO      = float(os.environ.get("PIN_TAIL_RATIO",   "0.55"))
ENGULF_MIN_RATIO    = float(os.environ.get("ENGULF_MIN_RATIO", "1.05"))
MOMENTUM_BODY_MIN   = float(os.environ.get("MOMENTUM_BODY_MIN","0.65"))

# H1 S/R
USE_H1_CONFIRM   = os.environ.get("USE_H1_CONFIRM", "true").lower() == "true"
USE_H1_SR        = os.environ.get("USE_H1_SR",      "true").lower() == "true"
H1_SR_DIST_MIN   = float(os.environ.get("H1_SR_DIST_MIN", "1.2"))
ANTI_CHOP        = os.environ.get("ANTI_CHOP", "true").lower() == "true"

# Market Regime (V13)
USE_REGIME       = os.environ.get("USE_REGIME", "true").lower() == "true"
REGIME_SYMBOL    = os.environ.get("REGIME_SYMBOL", "BTC-USDT")
REGIME_EMA       = int(os.environ.get("REGIME_EMA", "50"))

# Session filter (V13)
USE_SESSION      = os.environ.get("USE_SESSION", "true").lower() == "true"
SESSION_OFF_UTC_START = int(os.environ.get("SESSION_OFF_UTC_START", "0"))   # 00:00
SESSION_OFF_UTC_END   = int(os.environ.get("SESSION_OFF_UTC_END",   "5"))   # 05:00

# Correlation filter (V13)
USE_CORRELATION  = os.environ.get("USE_CORRELATION", "true").lower() == "true"
MAX_SAME_DIR     = int(os.environ.get("MAX_SAME_DIR", "4"))   # máx trades misma dirección

# Risk Manager (V13)
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT",     "1.5"))
LEVERAGE         = int(os.environ.get("LEVERAGE",           "5"))
MIN_ORDER_USDT   = float(os.environ.get("MIN_ORDER_USDT",   "3.0"))
MAX_ORDER_USDT   = float(os.environ.get("MAX_ORDER_USDT",   "40.0"))
MAX_MARGIN_PCT   = float(os.environ.get("MAX_MARGIN_PCT",   "25.0"))
MAX_DAILY_LOSS   = float(os.environ.get("MAX_DAILY_LOSS",   "8.0"))   # % del balance
MAX_CONSEC_LOSS  = int(os.environ.get("MAX_CONSEC_LOSS",    "4"))     # pausa tras N pérd.
CONSEC_LOSS_WAIT = int(os.environ.get("CONSEC_LOSS_WAIT",   "60"))    # minutos de pausa
SIZE_REDUCE_PCT  = float(os.environ.get("SIZE_REDUCE_PCT",  "50.0"))  # reducción tras 2 pérd.

COOLDOWN_MINS    = int(os.environ.get("COOLDOWN_MINS",  "20"))
TRAILING_STOP    = os.environ.get("TRAILING_STOP", "true").lower() == "true"
USE_WS_CACHE     = os.environ.get("USE_WS_CACHE",  "true").lower() == "true"

_raw = os.environ.get("CUSTOM_SYMBOLS", "")
CUSTOM_SYMBOLS = [s.strip() for s in _raw.split(",") if s.strip()] if _raw else []

BINGX_BASE   = "https://open-api.bingx.com"
BINGX_WS     = "wss://open-api-swap.bingx.com/swap-market"
INTERVAL_MAP = {
    "1m":"1m","3m":"3m","5m":"5m","15m":"15m",
    "30m":"30m","1h":"1H","4h":"4H","1d":"1D"
}
EXCLUDED_PREFIXES = ("NCS","NCF","NCMEX","NCOIL","NCGAS","NCXAU","NCXAG")
EXCLUDED_KEYWORDS = ("Gasoline","GasOil","Brent","WTI","OilBrent","Copper",
                     "Wheat","Cotton","Soybean","Silver","EURUSD","GBPUSD","JPYUSD")
FALLBACK_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT","DOGE-USDT",
    "ADA-USDT","AVAX-USDT","DOT-USDT","LINK-USDT","MATIC-USDT","UNI-USDT",
    "LTC-USDT","BCH-USDT","ATOM-USDT","XLM-USDT","ETC-USDT","NEAR-USDT",
    "APT-USDT","OP-USDT","ARB-USDT","FIL-USDT","ICP-USDT","HBAR-USDT",
    "AAVE-USDT","GRT-USDT","INJ-USDT","SUI-USDT","TIA-USDT","SEI-USDT",
    "WIF-USDT","PEPE-USDT","WLD-USDT","GMX-USDT","JTO-USDT",
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════════════════════
ws_kline_cache  = {}
ws_price_cache  = {}
ws_cache_lock   = threading.Lock()
tf_mid_cache    = {}   # {sym: (df, ts)}
h1_cache        = {}   # {sym: (df, ts)}
sl_cooldown     = {}
position_risk   = {}   # {sym: {entry, sl_initial, side, be_done, partial_done, qty}}
position_risk_lock = threading.Lock()
regime_cache    = {"regime": "NEUTRAL", "ts": 0.0}  # bull/bear/neutral

# ═══════════════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════════════════════════════
class RiskManager:
    """Controla pérdidas diarias, rachas negativas y ajuste dinámico del tamaño."""
    def __init__(self):
        self._lock        = threading.Lock()
        self.daily_pnl    = 0.0       # USDT ganado/perdido hoy
        self.day_start    = datetime.now(timezone.utc).date()
        self.consec_loss  = 0
        self.total_trades = 0
        self.total_wins   = 0
        self.paused_until = None      # datetime o None
        self.trades_log   = []        # lista de resultados para stats

    def reset_if_new_day(self):
        today = datetime.now(timezone.utc).date()
        with self._lock:
            if today != self.day_start:
                log.info(f"Nuevo día — reset RiskManager (PnL ayer: {self.daily_pnl:.2f})")
                self.daily_pnl   = 0.0
                self.day_start   = today
                self.consec_loss = 0
                self.paused_until = None

    def record_result(self, pnl_usdt: float):
        with self._lock:
            self.daily_pnl    += pnl_usdt
            self.total_trades += 1
            if pnl_usdt > 0:
                self.total_wins  += 1
                self.consec_loss  = 0
            else:
                self.consec_loss += 1
                if self.consec_loss >= MAX_CONSEC_LOSS:
                    self.paused_until = datetime.now(timezone.utc) + timedelta(minutes=CONSEC_LOSS_WAIT)
                    tg(f"⏸ <b>PAUSA AUTOMÁTICA</b> — {self.consec_loss} pérdidas seguidas.\n"
                       f"Reanudar a las <b>{self.paused_until.strftime('%H:%M')} UTC</b>")
            self.trades_log.append({
                "pnl": pnl_usdt,
                "ts":  datetime.now(timezone.utc).isoformat()
            })

    def can_trade(self, balance: float) -> tuple[bool, str]:
        self.reset_if_new_day()
        with self._lock:
            if self.paused_until and datetime.now(timezone.utc) < self.paused_until:
                remaining = (self.paused_until - datetime.now(timezone.utc)).seconds // 60
                return False, f"Pausa por racha ({remaining}min restantes)"
            max_loss = balance * (MAX_DAILY_LOSS / 100)
            if self.daily_pnl < -max_loss:
                return False, f"Pérdida diaria máxima alcanzada ({self.daily_pnl:.2f} USDT)"
            return True, "OK"

    def size_multiplier(self) -> float:
        """Reduce el tamaño tras 2+ pérdidas consecutivas."""
        with self._lock:
            if self.consec_loss >= 2:
                return SIZE_REDUCE_PCT / 100
            return 1.0

    def win_rate(self) -> float:
        with self._lock:
            if self.total_trades == 0:
                return 0.0
            return round(self.total_wins / self.total_trades * 100, 1)

    def daily_summary(self) -> str:
        with self._lock:
            wr = self.win_rate()
            pnl_sign = "+" if self.daily_pnl >= 0 else ""
            return (f"📊 <b>Stats del día</b>\n"
                    f"PnL: <code>{pnl_sign}{self.daily_pnl:.2f} USDT</code>\n"
                    f"Trades: {self.total_trades} | Win rate: {wr}%\n"
                    f"Racha pérdidas: {self.consec_loss}")

risk_mgr = RiskManager()

# ═══════════════════════════════════════════════════════════════════════════════
# BINGX API
# ═══════════════════════════════════════════════════════════════════════════════
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
        for field in ("availableMargin","available","walletBalance","equity","balance"):
            v = (bal if isinstance(bal, dict) else d).get(field)
            if v not in (None, "", 0, "0"):
                val = float(v)
                if val > 0:
                    return val
        return 0.0
    except Exception as e:
        log.error(f"get_balance: {e}")
        return 0.0

def get_all_positions():
    try:
        data   = bx_get("/openApi/swap/v2/user/positions", {})
        result = {}
        for p in data.get("data", []):
            if isinstance(p, dict) and float(p.get("positionAmt", 0)) != 0:
                result[p["symbol"]] = p
        log.info(f"Open positions ({len(result)}): {list(result.keys())[:10]}")
        return result
    except Exception as e:
        log.error(f"get_positions: {e}")
        return {}

# ── SYMBOLS ───────────────────────────────────────────────────────────────────
def _is_valid(sym):
    if not sym or not sym.endswith("-USDT"):
        return False
    base = sym.replace("-USDT", "")
    if len(base) < 2:
        return False
    if any(base.startswith(p) for p in EXCLUDED_PREFIXES):
        return False
    if any(kw.lower() in sym.lower() for kw in EXCLUDED_KEYWORDS):
        return False
    return True

def get_all_symbols(limit=0):
    for fn_name, endpoint, sort_key, filter_fn in [
        ("contracts", "/openApi/swap/v2/quote/contracts",
         lambda c: float(c.get("tradeAmount", 0) or 0),
         lambda c: c.get("asset") == "USDT" and c.get("status") == 1),
        ("ticker", "/openApi/swap/v2/quote/ticker",
         lambda t: float(t.get("quoteVolume", 0) or 0),
         lambda t: _is_valid(t.get("symbol", ""))),
    ]:
        try:
            data  = bx_get(endpoint, {})
            items = data.get("data", [])
            if not isinstance(items, list) or not items:
                continue
            filtered = [x for x in items if isinstance(x, dict) and filter_fn(x)]
            filtered.sort(key=sort_key, reverse=True)
            syms = [x["symbol"] for x in filtered if _is_valid(x.get("symbol", ""))]
            if syms:
                result = syms if limit == 0 else syms[:limit]
                log.info(f"✅ {len(result)} symbols via {fn_name}")
                return result
        except Exception as e:
            log.warning(f"{fn_name} failed: {e}")
    return FALLBACK_SYMBOLS if limit == 0 else FALLBACK_SYMBOLS[:limit]

def set_lev(symbol):
    for side in ("LONG", "SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol": symbol, "side": side, "leverage": LEVERAGE})
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════
def _ws_on_message(ws_app, message):
    try:
        import gzip
        try:
            data = json.loads(gzip.decompress(message) if isinstance(message, bytes) else message)
        except Exception:
            data = json.loads(message)
        if not data.get("dataType", "").endswith("@kline"):
            return
        sym   = data.get("s", "").replace("_", "-")
        kdata = data.get("data", {}).get("kline", data.get("k", {}))
        if not kdata:
            return
        row = {
            "open_time": pd.to_datetime(kdata.get("t", 0), unit="ms"),
            "open":  float(kdata.get("o", 0)),
            "high":  float(kdata.get("h", 0)),
            "low":   float(kdata.get("l", 0)),
            "close": float(kdata.get("c", 0)),
            "volume":float(kdata.get("v", 0)),
        }
        if row["close"] == 0:
            return
        with ws_cache_lock:
            df = ws_kline_cache.get(sym)
            if df is None:
                return
            if len(df) > 0 and df.iloc[-1]["open_time"] == row["open_time"]:
                for col in ("open","high","low","close","volume"):
                    df.at[df.index[-1], col] = row[col]
            else:
                ws_kline_cache[sym] = pd.concat(
                    [df, pd.DataFrame([row])], ignore_index=True).tail(400)
            ws_price_cache[sym] = row["close"]
    except Exception:
        pass

def start_ws_cache(symbols):
    if not USE_WS_CACHE:
        return
    ivl = INTERVAL_MAP.get(TIMEFRAME, "5m").lower()
    def _run():
        while True:
            try:
                def on_open(app):
                    for sym in symbols[:200]:
                        try:
                            app.send(json.dumps({
                                "id": f"sub_{sym}",
                                "reqType": "sub",
                                "dataType": f"{sym.replace('-','_')}@kline_{ivl}"
                            }))
                        except Exception:
                            pass
                ws = websocket.WebSocketApp(
                    BINGX_WS,
                    on_message=_ws_on_message,
                    on_error=lambda a, e: log.warning(f"WS error: {e}"),
                    on_close=lambda a, *x: log.info("WS closed"),
                    on_open=on_open
                )
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log.warning(f"WS thread: {e}")
            time.sleep(5)
    threading.Thread(target=_run, daemon=True).start()
    log.info(f"✅ WS cache iniciado para {min(len(symbols),200)} símbolos")

# ═══════════════════════════════════════════════════════════════════════════════
# PRECIO Y KLINES
# ═══════════════════════════════════════════════════════════════════════════════
def get_live_price(symbol):
    if USE_WS_CACHE:
        with ws_cache_lock:
            p = ws_price_cache.get(symbol)
        if p and p > 0:
            return p
    errors = []
    for endpoint, key1, key2 in [
        ("/openApi/swap/v2/quote/premiumIndex", "markPrice", None),
        ("/openApi/swap/v2/quote/ticker",       "lastPrice", "price"),
    ]:
        try:
            items = bx_get(endpoint, {"symbol": symbol}).get("data", [])
            for item in ([items] if isinstance(items, dict) else items):
                if item.get("symbol") == symbol:
                    for k in filter(None, [key1, key2]):
                        v = item.get(k)
                        if v:
                            return float(v)
        except Exception as e:
            errors.append(str(e))
    raise ValueError(f"get_live_price({symbol}) failed: {errors}")

def get_spread_pct(symbol):
    try:
        data = bx_get("/openApi/swap/v2/quote/bookTicker", {"symbol": symbol})
        d = data.get("data", {})
        if isinstance(d, list):
            d = next((x for x in d if x.get("symbol") == symbol), {})
        ask, bid = float(d.get("askPrice", 0) or 0), float(d.get("bidPrice", 0) or 0)
        return (ask - bid) / bid * 100 if ask > 0 and bid > 0 else 999.0
    except Exception:
        return 999.0

def _fetch_klines_raw(symbol, tf, limit):
    params = {"symbol": symbol, "interval": INTERVAL_MAP.get(tf, tf), "limit": limit}
    data   = bx_get("/openApi/swap/v3/quote/klines", params)
    rows   = data.get("data", [])
    if not rows or not isinstance(rows, list):
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time"])
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.dropna(subset=["open","high","low","close","volume"], inplace=True)
    return df.sort_values("open_time").reset_index(drop=True)

def get_klines(symbol, limit=300):
    if USE_WS_CACHE:
        with ws_cache_lock:
            df = ws_kline_cache.get(symbol)
        if df is not None and len(df) >= limit // 2:
            return df.copy()
    df = _fetch_klines_raw(symbol, TIMEFRAME, limit)
    if not df.empty and USE_WS_CACHE:
        with ws_cache_lock:
            ws_kline_cache[symbol] = df.copy()
    return df

def get_mid_klines(symbol, limit=120):
    now = time.time()
    cached = tf_mid_cache.get(symbol)
    if cached and now - cached[1] < TF_MID_CACHE_TTL and len(cached[0]) >= 50:
        return cached[0].copy()
    df = _fetch_klines_raw(symbol, TF_MID, limit)
    if not df.empty:
        tf_mid_cache[symbol] = (df.copy(), now)
    return df

def get_h1_klines(symbol, limit=60):
    now = time.time()
    cached = h1_cache.get(symbol)
    if cached and now - cached[1] < H1_CACHE_TTL and len(cached[0]) >= 30:
        return cached[0].copy()
    df = _fetch_klines_raw(symbol, "1h", limit)
    if not df.empty:
        h1_cache[symbol] = (df.copy(), now)
    return df

# ═══════════════════════════════════════════════════════════════════════════════
# INDICADORES
# ═══════════════════════════════════════════════════════════════════════════════
def calc_atr(high, low, close, period):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def calc_ema_angle(ema_s, atr_s, look):
    pc = ema_s - ema_s.shift(look)
    return pd.Series(
        np.degrees(np.arctan2(pc.values, (atr_s * look).values)),
        index=ema_s.index
    )

def calc_adx(high, low, close, period):
    up, down = high.diff(), -low.diff()
    pdm = np.where((up > down) & (up > 0),   up,   0.0)
    mdm = np.where((down > up) & (down > 0), down, 0.0)
    tr  = pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
    a   = 1.0 / period
    def w(arr):
        return pd.Series(arr, index=high.index).ewm(alpha=a, adjust=False).mean()
    tr_s = w(tr)
    di_p = 100 * w(pdm) / tr_s.replace(0, np.nan)
    di_m = 100 * w(mdm) / tr_s.replace(0, np.nan)
    dx   = 100 * (di_p-di_m).abs() / (di_p+di_m).replace(0, np.nan)
    return di_p, di_m, dx.ewm(alpha=a, adjust=False).mean()

def calc_rsi(close, period):
    d = close.diff()
    g = d.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def calc_vwap(df):
    """VWAP del día actual o de toda la serie si no hay fecha."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    return (pv.cumsum() / df["volume"].cumsum()).rename("vwap")

def calc_swing_sl(df, i, direction, atr_val):
    """
    Busca el swing low/high más reciente en los últimos SWING_LOOK cierres
    para colocar el SL en un nivel de precio significativo.
    """
    lookback = min(SWING_LOOK, i)
    window   = df.iloc[i-lookback:i+1]
    margin   = atr_val * 0.15

    if direction == "LONG":
        swing = float(window["low"].min())
        return swing - margin
    else:
        swing = float(window["high"].max())
        return swing + margin

def volume_delta_ok(df, i, direction):
    """
    Confirma que la vela señal tiene sesgo de volumen correcto.
    Proxy: vela alcista (close>open) para LONG, bajista para SHORT.
    """
    if not USE_VOL_DELTA:
        return True
    c = float(df["close"].iloc[i])
    o = float(df["open"].iloc[i])
    if direction == "LONG":
        return c >= o
    return c <= o

# ═══════════════════════════════════════════════════════════════════════════════
# MARKET REGIME (BTC-based)
# ═══════════════════════════════════════════════════════════════════════════════
def get_market_regime() -> str:
    """
    BULL  : precio BTC H1 > EMA50 y EMA50 subiendo
    BEAR  : precio BTC H1 < EMA50 y EMA50 bajando
    NEUTRAL: cualquier otra condición
    Cachea el resultado 5 minutos.
    """
    if not USE_REGIME:
        return "NEUTRAL"
    now = time.time()
    if now - regime_cache["ts"] < 300:
        return regime_cache["regime"]
    try:
        df    = get_h1_klines(REGIME_SYMBOL, limit=100)
        if df.empty or len(df) < REGIME_EMA + 5:
            return "NEUTRAL"
        ema   = df["close"].ewm(span=REGIME_EMA, adjust=False).mean()
        e_now = float(ema.iloc[-1])
        e_pre = float(ema.iloc[-5])
        c_now = float(df["close"].iloc[-1])
        if c_now > e_now and e_now > e_pre:
            regime = "BULL"
        elif c_now < e_now and e_now < e_pre:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"
        regime_cache.update({"regime": regime, "ts": now})
        log.info(f"Market regime (BTC H1): {regime}")
        return regime
    except Exception as e:
        log.debug(f"Regime detection: {e}")
        return "NEUTRAL"

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION FILTER
# ═══════════════════════════════════════════════════════════════════════════════
def is_session_ok() -> bool:
    if not USE_SESSION:
        return True
    hour = datetime.now(timezone.utc).hour
    if SESSION_OFF_UTC_START <= hour < SESSION_OFF_UTC_END:
        return False
    return True

# ═══════════════════════════════════════════════════════════════════════════════
# TRIPLE-TF ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_mid_tf(symbol, direction):
    """
    15m: EMA fast/slow alineadas y precio > EMA trend.
    Retorna (ok: bool, bonus: int 0-10)
    """
    df = get_mid_klines(symbol, limit=120)
    if df.empty or len(df) < max(MID_EMA_TREND + 5, 60):
        return True, 0   # no hay datos = no bloquear, sin bonus

    close   = df["close"]
    ema_f   = close.ewm(span=MID_EMA_FAST,  adjust=False).mean()
    ema_s   = close.ewm(span=MID_EMA_SLOW,  adjust=False).mean()
    ema_t   = close.ewm(span=MID_EMA_TREND, adjust=False).mean()

    ef, es, et, c = (float(ema_f.iloc[-1]), float(ema_s.iloc[-1]),
                     float(ema_t.iloc[-1]), float(close.iloc[-1]))

    if direction == "LONG":
        aligned = ef > es and c > et
    else:
        aligned = ef < es and c < et

    bonus = 10 if aligned else 0
    return aligned, bonus

def analyze_h1(symbol, direction):
    """
    H1: tendencia EMA7/17, S/R por pivotes, distancia mínima a S/R.
    """
    df = get_h1_klines(symbol, limit=60)
    if df.empty or len(df) < 30:
        return None

    close = df["close"]
    ema_f = close.ewm(span=H1_EMA_FAST, adjust=False).mean()
    ema_s = close.ewm(span=H1_EMA_SLOW, adjust=False).mean()

    ef_now, ef_prev = float(ema_f.iloc[-1]), float(ema_f.iloc[-4] if len(ema_f) > 4 else ema_f.iloc[-1])
    es_now = float(ema_s.iloc[-1])
    c_now  = float(close.iloc[-1])

    if c_now > es_now and ef_now > es_now and ef_now > ef_prev:
        h1_trend = "BULL"
    elif c_now < es_now and ef_now < es_now and ef_now < ef_prev:
        h1_trend = "BEAR"
    else:
        h1_trend = "NEUTRAL"

    high, low = df["high"], df["low"]
    ph_vals, pl_vals = [], []
    for idx in range(3, min(len(df)-3, 40)):
        if float(high.iloc[idx]) == float(high.iloc[idx-3:idx+4].max()):
            ph_vals.append(float(high.iloc[idx]))
        if float(low.iloc[idx]) == float(low.iloc[idx-3:idx+4].min()):
            pl_vals.append(float(low.iloc[idx]))

    res = sorted([v for v in ph_vals if v > c_now])
    sup = sorted([v for v in pl_vals if v < c_now], reverse=True)
    h1_resistance = res[0] if res else c_now * 1.08
    h1_support    = sup[0] if sup else c_now * 0.92

    return {
        "h1_trend":      h1_trend,
        "h1_resistance": h1_resistance,
        "h1_support":    h1_support,
        "dist_to_res":   round((h1_resistance - c_now) / c_now * 100, 2),
        "dist_to_sup":   round((c_now - h1_support)    / c_now * 100, 2),
        "h1_rsi":        round(float(calc_rsi(close, 14).iloc[-1]), 1),
        "close_h1":      c_now,
    }

# ═══════════════════════════════════════════════════════════════════════════════
# PATRONES DE VELA
# ═══════════════════════════════════════════════════════════════════════════════
def detect_pin_bar(df, i, direction):
    o,h,l,c = float(df["open"].iloc[i]),float(df["high"].iloc[i]),\
               float(df["low"].iloc[i]),float(df["close"].iloc[i])
    rng = h - l
    if rng < 1e-10:
        return False, 0.0
    body = abs(c - o)
    if body / rng > PIN_BAR_RATIO:
        return False, 0.0
    uw, lw = h - max(o,c), min(o,c) - l
    if direction == "LONG":
        tr = lw / rng
        if tr >= PIN_TAIL_RATIO and lw >= 2*max(body,1e-10):
            return True, round(min(tr*120,100),1)
    else:
        tr = uw / rng
        if tr >= PIN_TAIL_RATIO and uw >= 2*max(body,1e-10):
            return True, round(min(tr*120,100),1)
    return False, 0.0

def detect_engulfing(df, i, direction):
    if i < 1:
        return False, 0.0
    oc,cc = float(df["open"].iloc[i]),float(df["close"].iloc[i])
    op,cp = float(df["open"].iloc[i-1]),float(df["close"].iloc[i-1])
    bp, bc = abs(cp-op), abs(cc-oc)
    if bp < 1e-10 or bc/bp < ENGULF_MIN_RATIO:
        return False, 0.0
    if direction == "LONG" and cc>oc and cp<op and cc>max(op,cp) and oc<min(op,cp):
        return True, round(min(bc/bp*45,100),1)
    if direction == "SHORT" and cc<oc and cp>op and cc<min(op,cp) and oc>max(op,cp):
        return True, round(min(bc/bp*45,100),1)
    return False, 0.0

def detect_momentum_candle(df, i, direction, atr):
    o,h,l,c = float(df["open"].iloc[i]),float(df["high"].iloc[i]),\
               float(df["low"].iloc[i]),float(df["close"].iloc[i])
    rng, body = h-l, abs(c-o)
    if rng < 1e-10 or atr < 1e-10 or body/rng < MOMENTUM_BODY_MIN or body < atr*0.5:
        return False, 0.0
    if direction == "LONG" and c > o and (h-c) < body*0.35:
        return True, round(min(body/rng*90,100),1)
    if direction == "SHORT" and c < o and (c-l) < body*0.35:
        return True, round(min(body/rng*90,100),1)
    return False, 0.0

def detect_inside_bar_breakout(df, i, direction):
    if i < 2:
        return False, 0.0
    h2,l2 = float(df["high"].iloc[i-2]),float(df["low"].iloc[i-2])
    h1,l1 = float(df["high"].iloc[i-1]),float(df["low"].iloc[i-1])
    c     = float(df["close"].iloc[i])
    if not (h1<=h2 and l1>=l2):
        return False, 0.0
    if direction == "LONG" and c > h2:
        return True, 65.0
    if direction == "SHORT" and c < l2:
        return True, 65.0
    return False, 0.0

def is_choppy(df, adx_val):
    if not ANTI_CHOP:
        return False
    if adx_val < ADX_MIN:
        return True
    if len(df) < 15:
        return False
    atr = float(calc_atr(df["high"],df["low"],df["close"],ATR_LEN).iloc[-1])
    avg = float((df["high"].iloc[-12:] - df["low"].iloc[-12:]).mean())
    return atr > 0 and avg < atr * 0.75

# ═══════════════════════════════════════════════════════════════════════════════
# POSITION SIZING
# ═══════════════════════════════════════════════════════════════════════════════
def calc_qty(balance, entry, sl, size_mult=1.0):
    dist = abs(entry - sl) / entry
    if dist < 1e-8:
        return 0, 0
    risk_usdt = balance * (RISK_PERCENT / 100) * size_mult
    notional  = risk_usdt / dist
    max_n     = min(MAX_ORDER_USDT, balance*(MAX_MARGIN_PCT/100)*LEVERAGE)
    notional  = max(MIN_ORDER_USDT, min(notional, max_n))
    qty       = notional / entry
    return round(max(qty, 0.001), 4), round(notional, 2)

# ═══════════════════════════════════════════════════════════════════════════════
# ÓRDENES
# ═══════════════════════════════════════════════════════════════════════════════
def open_order(symbol, side, qty, sl, tp):
    payload = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type":         "MARKET",
        "quantity":     round(qty, 4),
        "stopLoss":  json.dumps({"type":"STOP_MARKET",       "stopPrice":round(sl,6),"workingType":"MARK_PRICE"}),
        "takeProfit":json.dumps({"type":"TAKE_PROFIT_MARKET","stopPrice":round(tp,6),"workingType":"MARK_PRICE"}),
    }
    resp = bx_post("/openApi/swap/v2/trade/order", payload)
    if resp.get("code", -1) != 0:
        raise ValueError(f"code={resp.get('code')}: {resp.get('msg','?')}")
    return resp

def open_order_retry(symbol, side, qty, sl, tp):
    for attempt in range(2):
        try:
            return open_order(symbol, side, qty, sl, tp)
        except ValueError as e:
            if "101400" in str(e) and attempt == 0:
                time.sleep(1)
                try:
                    live = get_live_price(symbol)
                    d    = MIN_DIST_PCT / 100
                    sl   = live*(1-d) if side=="BUY" else live*(1+d)
                    tp   = (live+(live-sl)*TP_MULT) if side=="BUY" else (live-(sl-live)*TP_MULT)
                    sl, tp = round(sl,6), round(tp,6)
                except Exception:
                    raise e
            else:
                raise

def close_partial(symbol, side, qty_to_close, position_side):
    """Cierra qty_to_close contratos de la posición."""
    payload = {
        "symbol":       symbol,
        "side":         side,
        "positionSide": position_side,
        "type":         "MARKET",
        "quantity":     round(qty_to_close, 4),
        "reduceOnly":   "true",
    }
    resp = bx_post("/openApi/swap/v2/trade/order", payload)
    return resp

def move_sl_to_be(symbol, side, position_side, new_sl_price):
    """Cancela el SL existente y pone uno nuevo en BE."""
    sl_side = "SELL" if position_side == "LONG" else "BUY"
    try:
        bx_post("/openApi/swap/v2/trade/order", {
            "symbol":        symbol,
            "type":          "STOP_MARKET",
            "side":          sl_side,
            "positionSide":  position_side,
            "stopPrice":     round(new_sl_price, 6),
            "closePosition": "true",
            "workingType":   "MARK_PRICE"
        })
        log.info(f"✅ SL→BE {symbol} {position_side} @ {new_sl_price:.6g}")
    except Exception as e:
        log.warning(f"move_sl_to_be {symbol}: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# TRAILING + PARTIAL TP
# ═══════════════════════════════════════════════════════════════════════════════
def update_positions(positions):
    if not positions:
        return

    for sym, pos in positions.items():
        try:
            side  = pos.get("positionSide", "LONG")
            entry = float(pos.get("avgPrice", 0) or 0)
            qty   = abs(float(pos.get("positionAmt", 0) or 0))
            if entry == 0 or qty == 0:
                continue

            live = get_live_price(sym)

            with position_risk_lock:
                risk = position_risk.get(sym)

            if not risk:
                continue

            sl_init    = risk["sl_initial"]
            sl_dist    = abs(entry - sl_init)
            be_trigger = entry + sl_dist * BE_R_MULT if side == "LONG" else entry - sl_dist * BE_R_MULT
            be_sl      = entry * (1 + 0.001) if side == "LONG" else entry * (1 - 0.001)

            # ── Partial TP a 1.5R ─────────────────────────────────────────
            if USE_PARTIAL_TP and not risk.get("partial_done", False):
                pt_trigger = entry + sl_dist * PARTIAL_TP_R if side == "LONG" else entry - sl_dist * PARTIAL_TP_R
                partial_hit = (live >= pt_trigger) if side == "LONG" else (live <= pt_trigger)

                if partial_hit:
                    close_qty = round(qty * PARTIAL_TP_PCT / 100, 4)
                    close_side = "SELL" if side == "LONG" else "BUY"
                    if close_qty >= 0.001:
                        try:
                            close_partial(sym, close_side, close_qty, side)
                            move_sl_to_be(sym, close_side, side, be_sl)
                            with position_risk_lock:
                                if sym in position_risk:
                                    position_risk[sym]["partial_done"] = True
                                    position_risk[sym]["be_done"]      = True
                            log.info(f"💰 Partial TP {sym} {side}: cerrado {close_qty:.4f} @ {live:.4f}")
                            tg(f"💰 <b>PARTIAL TP {sym} {side}</b>\n"
                               f"Cerrado {PARTIAL_TP_PCT:.0f}% @ <code>{live:.6g}</code> "
                               f"({PARTIAL_TP_R}R) ✅\nSL → BE <code>{be_sl:.6g}</code>")
                            risk_mgr.record_result(+abs(live - entry) * close_qty * 0.5)  # estimado
                        except Exception as e:
                            log.warning(f"Partial TP {sym}: {e}")
                    continue

            # ── Breakeven (si no hubo partial TP) ────────────────────────
            if not risk.get("be_done", False):
                be_hit = (live >= be_trigger) if side == "LONG" else (live <= be_trigger)
                if be_hit:
                    move_sl_to_be(sym, None, side, be_sl)
                    with position_risk_lock:
                        if sym in position_risk:
                            position_risk[sym]["be_done"] = True
                    tg(f"🔐 <b>BREAKEVEN {sym} {side}</b> @ {BE_R_MULT}R\n"
                       f"SL → <code>{be_sl:.6g}</code> ✅")

        except Exception as e:
            log.debug(f"update_positions {sym}: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# ESCÁNER PRINCIPAL — TRIPLE TF
# ═══════════════════════════════════════════════════════════════════════════════
def scan_symbol(symbol, regime):
    # Cooldown check
    if symbol in sl_cooldown:
        elapsed = (datetime.now(timezone.utc) - sl_cooldown[symbol]).total_seconds() / 60
        if elapsed < COOLDOWN_MINS:
            return None

    try:
        # ── 1. DATOS Y FILTROS BÁSICOS ────────────────────────────────────
        df = get_klines(symbol, limit=300)
        min_bars = max(EMA_TREND+10, ADX_LEN*2+5, RSI_LEN+5, 60)
        if df.empty or len(df) < min_bars:
            return None

        atr_s      = calc_atr(df["high"], df["low"], df["close"], ATR_LEN)
        ema_f      = df["close"].ewm(span=EMA_FAST,  adjust=False).mean()
        ema_s      = df["close"].ewm(span=EMA_SLOW,  adjust=False).mean()
        ema_trend  = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
        angle      = calc_ema_angle(ema_f, atr_s, SLOPE_LOOK)
        di_p,di_m,adx_s = calc_adx(df["high"], df["low"], df["close"], ADX_LEN)
        rsi_s      = calc_rsi(df["close"], RSI_LEN)
        vol_ma     = df["volume"].rolling(20).mean()

        i = len(df) - 2
        if i < max(EMA_TREND+2, ADX_LEN*2, 50):
            return None

        c_now    = float(df["close"].iloc[i])
        o_now    = float(df["open"].iloc[i])
        h_now    = float(df["high"].iloc[i])
        l_now    = float(df["low"].iloc[i])
        ef_now   = float(ema_f.iloc[i]);     ef_prev = float(ema_f.iloc[i-1])
        es_now   = float(ema_s.iloc[i]);     es_prev = float(ema_s.iloc[i-1])
        et_now   = float(ema_trend.iloc[i])
        ang_now  = float(angle.iloc[i])
        adx_now  = float(adx_s.iloc[i])
        dip_now  = float(di_p.iloc[i]);     dim_now = float(di_m.iloc[i])
        rsi_now  = float(rsi_s.iloc[i])
        vol_now  = float(df["volume"].iloc[i])
        vma      = float(vol_ma.iloc[i])
        catr     = float(atr_s.iloc[i])

        if any(np.isnan(x) for x in [ang_now,adx_now,catr,ef_now,es_now,et_now,rsi_now,dip_now,dim_now]):
            return None
        if vma <= 0 or catr <= 0:
            return None

        atr_pct = catr / c_now * 100
        if atr_pct > ATR_MAX_PCT:
            return None
        if is_choppy(df, adx_now):
            return None

        vratio = round(vol_now / vma, 2) if vma > 0 else 0.0

        # ── 2. CONDICIONES BASE 5m ────────────────────────────────────────
        trend_long  = c_now > et_now
        trend_short = c_now < et_now

        base_long = (
            ef_now > es_now and
            ang_now >= SLOPE_LIMIT and
            ((not USE_ADX) or adx_now > ADX_MIN) and
            ((not USE_VOL) or vratio >= VOL_MULT) and
            trend_long and
            ((not USE_RSI) or rsi_now < RSI_OB) and
            ((not USE_DI)  or dip_now > dim_now) and
            ef_now > ef_prev and
            (ef_now-es_now) > (ef_prev-es_prev)
        )
        base_short = (
            ef_now < es_now and
            ang_now <= -SLOPE_LIMIT and
            ((not USE_ADX) or adx_now > ADX_MIN) and
            ((not USE_VOL) or vratio >= VOL_MULT) and
            trend_short and
            ((not USE_RSI) or rsi_now > RSI_OS) and
            ((not USE_DI)  or dim_now > dip_now) and
            ef_now < ef_prev and
            (es_now-ef_now) > (es_prev-ef_prev)
        )

        if not base_long and not base_short:
            return None

        direction = "LONG" if base_long else "SHORT"

        # ── 3. MARKET REGIME FILTER ───────────────────────────────────────
        if USE_REGIME and regime != "NEUTRAL":
            if direction == "LONG"  and regime == "BEAR":
                return None
            if direction == "SHORT" and regime == "BULL":
                return None

        # ── 4. TF INTERMEDIO (15m) ────────────────────────────────────────
        mid_ok, mid_bonus = analyze_mid_tf(symbol, direction)
        if not mid_ok:
            return None

        # ── 5. H1 CONFIRMACIÓN ───────────────────────────────────────────
        h1_ctx    = None
        h1_bonus  = 0
        h1_trend  = "UNKNOWN"

        if USE_H1_CONFIRM:
            h1_ctx = analyze_h1(symbol, direction)
            if h1_ctx:
                h1_trend = h1_ctx["h1_trend"]
                if direction == "LONG" and h1_trend == "BULL":
                    h1_bonus = 20
                elif direction == "SHORT" and h1_trend == "BEAR":
                    h1_bonus = 20
                elif h1_trend == "NEUTRAL":
                    h1_bonus = 5
                else:
                    return None   # contra tendencia H1

                if USE_H1_SR and h1_ctx:
                    if direction == "LONG"  and h1_ctx["dist_to_res"] < H1_SR_DIST_MIN:
                        return None
                    if direction == "SHORT" and h1_ctx["dist_to_sup"] < H1_SR_DIST_MIN:
                        return None

        # ── 6. VWAP FILTER ────────────────────────────────────────────────
        vwap_bonus = 0
        if USE_VWAP:
            vwap_s = calc_vwap(df)
            vwap_v = float(vwap_s.iloc[i]) if not np.isnan(float(vwap_s.iloc[i])) else c_now
            if direction == "LONG" and c_now > vwap_v:
                vwap_bonus = 5
            elif direction == "SHORT" and c_now < vwap_v:
                vwap_bonus = 5
            elif direction == "LONG" and c_now < vwap_v:
                return None   # bajo VWAP = no longs
            elif direction == "SHORT" and c_now > vwap_v:
                return None   # sobre VWAP = no shorts

        # ── 7. VOLUME DELTA ───────────────────────────────────────────────
        if not volume_delta_ok(df, i, direction):
            return None

        # ── 8. PATRONES DE VELA ───────────────────────────────────────────
        pattern_name      = "SLOPE"
        pattern_score     = 0.0
        sl_candle         = None
        sl_candle_strict  = False

        if USE_CANDLE_PATTERNS:
            is_pin, pin_s = detect_pin_bar(df, i, direction)
            is_eng, eng_s = detect_engulfing(df, i, direction)
            is_mom, mom_s = detect_momentum_candle(df, i, direction, catr)
            is_ib,  ib_s  = detect_inside_bar_breakout(df, i, direction)

            m = catr * 0.08
            if is_pin:
                pattern_name, pattern_score = "PIN_BAR", pin_s
                sl_candle = (l_now - m) if direction == "LONG" else (h_now + m)
                sl_candle_strict = True
            elif is_eng:
                pattern_name, pattern_score = "ENGULF", eng_s
                sl_candle = (l_now - catr*0.10) if direction == "LONG" else (h_now + catr*0.10)
                sl_candle_strict = True
            elif is_mom:
                pattern_name, pattern_score = "MOMENTUM", mom_s
                sl_candle = (l_now - catr*0.12) if direction == "LONG" else (h_now + catr*0.12)
            elif is_ib:
                pattern_name, pattern_score = "INSIDE_BR", ib_s
                pl = float(df["low"].iloc[i-1]);  ph = float(df["high"].iloc[i-1])
                sl_candle = (pl - catr*0.10) if direction == "LONG" else (ph + catr*0.10)

        # ── 9. SL CALCULADO ──────────────────────────────────────────────
        # Prioridad: swing point > vela señal > ATR
        atr_sl = catr * SL_ATR_MULT

        swing_sl = None
        if USE_SWING_SL:
            swing_sl = calc_swing_sl(df, i, direction, catr)

        if direction == "LONG":
            candidates = [c_now - atr_sl]
            if sl_candle is not None:
                candidates.append(sl_candle)
            if swing_sl is not None:
                candidates.append(swing_sl)
            # Usar el más ajustado (mayor para LONG) salvo que sea strict
            if sl_candle_strict and sl_candle is not None:
                sl_price = sl_candle
            else:
                sl_price = max(candidates)   # SL más ajustado = mayor precio = menor riesgo

            dist_need = c_now * (1 - MIN_DIST_PCT/100)
            if sl_price > dist_need:
                sl_price = dist_need
            if sl_price >= c_now:
                return None
            tp_price = c_now + (c_now - sl_price) * TP_MULT
        else:
            candidates = [c_now + atr_sl]
            if sl_candle is not None:
                candidates.append(sl_candle)
            if swing_sl is not None:
                candidates.append(swing_sl)
            if sl_candle_strict and sl_candle is not None:
                sl_price = sl_candle
            else:
                sl_price = min(candidates)   # SL más ajustado = menor precio = menor riesgo

            dist_need = c_now * (1 + MIN_DIST_PCT/100)
            if sl_price < dist_need:
                sl_price = dist_need
            if sl_price <= c_now:
                return None
            tp_price = c_now - (sl_price - c_now) * TP_MULT

        dist     = abs(c_now - sl_price)
        dist_pct = dist / c_now * 100
        if dist_pct < MIN_DIST_PCT:
            return None

        rr = abs(tp_price - c_now) / dist
        if rr < MIN_RR:
            return None

        # ── 10. SCORING V13 ───────────────────────────────────────────────
        # Pesos: ángulo(25) + ADX(15) + H1(20) + 15m(10) + patrón(10) + VWAP(5) + vol(8) + RR(4) + DI(3)
        score  = min(abs(ang_now)  / SLOPE_LIMIT * 25,      25)
        score += min((adx_now - ADX_MIN) / ADX_MIN * 15,    15)
        score += h1_bonus
        score += mid_bonus
        score += min(pattern_score / 10,                     10)
        score += vwap_bonus
        score += min(vratio * 4,                              8)
        score += min((rr - MIN_RR) * 2,                       4)
        score += min(abs(dip_now - dim_now) / 10,             3)

        if score < MIN_SCORE:
            return None

        return {
            "symbol":     symbol,
            "signal":     direction,
            "pattern":    pattern_name,
            "close":      c_now,
            "candle_h":   h_now,
            "candle_l":   l_now,
            "sl":         round(sl_price, 6),
            "tp":         round(tp_price, 6),
            "atr":        catr,
            "atr_pct":    round(atr_pct, 2),
            "vol_ratio":  vratio,
            "angle":      round(ang_now, 1),
            "adx":        round(adx_now, 1),
            "rsi":        round(rsi_now, 1),
            "score":      round(score, 1),
            "rr":         round(rr, 2),
            "dist_pct":   round(dist_pct, 3),
            "di_spread":  round(abs(dip_now - dim_now), 1),
            "h1_trend":   h1_trend,
            "h1_ctx":     h1_ctx,
            "pat_score":  round(pattern_score, 1),
            "sl_strict":  sl_candle_strict,
            "vwap_bonus": vwap_bonus,
            "mid_bonus":  mid_bonus,
        }

    except Exception as e:
        log.debug(f"Scan {symbol}: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════════
async def _send(msg):
    if not TELEGRAM_OK or not TELEGRAM_TOKEN:
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    cid = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
    await bot.send_message(chat_id=cid, text=msg, parse_mode=ParseMode.HTML)

def tg(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        asyncio.run(_send(msg))
    except Exception as e:
        log.warning(f"Telegram: {e}")

PAT_ICON = {"PIN_BAR":"📌","ENGULF":"🔄","MOMENTUM":"💥","INSIDE_BR":"📦","SLOPE":"📈"}

def tg_startup(balance, symbols, regime):
    tg(
        f"🚀 <b>EMA+ADX+MTF Elite V13.0 — APEX EDITION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <b>Triple TF:</b> {TIMEFRAME} + {TF_MID} + {TF_SLOW}\n"
        f"📊 <b>5m:</b> EMA{EMA_FAST}/{EMA_SLOW}/T{EMA_TREND} Slope≥{SLOPE_LIMIT}° ADX≥{ADX_MIN}\n"
        f"📊 <b>15m:</b> EMA{MID_EMA_FAST}/{MID_EMA_SLOW}/T{MID_EMA_TREND}\n"
        f"📊 <b>H1:</b> EMA{H1_EMA_FAST}/{H1_EMA_SLOW}\n"
        f"🎯 <b>TP:</b> {TP_MULT}× | <b>SL:</b> {SL_ATR_MULT}× ATR+Swing | R:R≥{MIN_RR}\n"
        f"💰 <b>Partial TP:</b> {'✅ ' + str(PARTIAL_TP_PCT)+'% @ '+str(PARTIAL_TP_R)+'R' if USE_PARTIAL_TP else '❌'}\n"
        f"🔐 <b>BE:</b> {BE_R_MULT}R | <b>Score≥:</b> {MIN_SCORE}\n"
        f"🌍 <b>Regime:</b> {'✅ BTC-based' if USE_REGIME else '❌'} = <b>{regime}</b>\n"
        f"🕐 <b>Session:</b> {'✅ Sin 00-05 UTC' if USE_SESSION else '❌'}\n"
        f"📉 <b>VWAP:</b> {'✅' if USE_VWAP else '❌'} | "
        f"<b>Swing SL:</b> {'✅' if USE_SWING_SL else '❌'}\n"
        f"🛡 <b>Risk:</b> {RISK_PERCENT}% | Pausa tras {MAX_CONSEC_LOSS} pérd. | "
        f"Pérd.diaria max {MAX_DAILY_LOSS}%\n"
        f"<b>Max trades:</b> {MAX_OPEN_TRADES} | <b>Max dir:</b> {MAX_SAME_DIR}\n"
        f"<b>Balance:</b> {balance:.2f} USDT | <b>Símbolos:</b> {len(symbols)}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )

def tg_entry(sig, qty, notional, balance, spread_pct):
    d    = "🟢 LONG" if sig["signal"] == "LONG" else "🔴 SHORT"
    pi   = PAT_ICON.get(sig.get("pattern","SLOPE"),"⚡")
    sl_t = " <i>(strict vela)</i>" if sig.get("sl_strict") else ""
    h1   = sig.get("h1_ctx")
    sr   = ""
    if h1:
        sr = (f"\n<b>H1 Res:</b> {h1['h1_resistance']:.6g} (+{h1['dist_to_res']:.1f}%) | "
              f"<b>H1 Sup:</b> {h1['h1_support']:.6g} (-{h1['dist_to_sup']:.1f}%)")
    partial_line = (f"\n💰 <b>Partial TP:</b> {PARTIAL_TP_PCT:.0f}% @ "
                    f"<code>{sig['close'] + abs(sig['close']-sig['sl'])*PARTIAL_TP_R*(1 if sig['signal']=='LONG' else -1):.6g}</code> ({PARTIAL_TP_R}R)"
                    if USE_PARTIAL_TP else "")
    tg(
        f"<b>✅ ORDEN V13 — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{d} | {pi} <b>{sig['pattern']}</b> | Score: {sig['score']}/100\n"
        f"H1:{sig.get('h1_trend','?')} | 15m:{'+' if sig.get('mid_bonus',0)>0 else '='} | "
        f"VWAP:{'+' if sig.get('vwap_bonus',0)>0 else '='}\n"
        f"<b>Ang:</b> {sig['angle']}° <b>ADX:</b> {sig['adx']} <b>RSI:</b> {sig['rsi']} "
        f"<b>Vol:</b> {sig['vol_ratio']}x <b>Spread:</b> {spread_pct:.3f}%"
        f"{sr}\n"
        f"<b>Entrada:</b> <code>{sig['close']:.6g}</code>\n"
        f"<b>SL:</b> <code>{sig['sl']:.6g}</code> ({sig['dist_pct']}%){sl_t}\n"
        f"<b>TP:</b> <code>{sig['tp']:.6g}</code> | R:R 1:{sig['rr']}"
        f"{partial_line}\n"
        f"<b>BE:</b> @ {BE_R_MULT}R | <b>Qty:</b> {qty:.4f} | <b>Notional:</b> {notional:.2f} USDT\n"
        f"<b>Riesgo:</b> {balance*RISK_PERCENT/100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

def tg_scan(signals, total, open_count, regime):
    if not signals:
        return
    r_icon = {"BULL":"🟢","BEAR":"🔴","NEUTRAL":"⚪"}.get(regime,"⚪")
    lines = [
        f"🔍 <b>{len(signals)} señal(es)/{total}</b> | {open_count}/{MAX_OPEN_TRADES} | {r_icon}{regime}",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in signals[:6]:
        e  = "🟢" if s["signal"] == "LONG" else "🔴"
        pi = PAT_ICON.get(s.get("pattern","SLOPE"),"⚡")
        lines.append(
            f"{e}{pi} <b>{s['symbol']}</b> H1:{s.get('h1_trend','?')} "
            f"Score:{s['score']} Ang:{s['angle']}° ADX:{s['adx']} "
            f"Vol:{s['vol_ratio']}x RR:1:{s['rr']}"
        )
    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    tg("\n".join(lines))

def tg_diag(signals, skip_reasons):
    lines = [f"⚠️ <b>DIAG V13: {len(signals)} señales, 0 órdenes</b>", "━━━━━━━━━━━━━━━━━━━━━"]
    for sym, reason in list(skip_reasons.items())[:8]:
        lines.append(f"  • <b>{sym}</b>: {reason}")
    tg("\n".join(lines))

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    log.info("=== EMA+ADX+MTF Elite V13.0 APEX EDITION ===")
    symbols   = CUSTOM_SYMBOLS if CUSTOM_SYMBOLS else get_all_symbols(MAX_SYMBOLS)
    if not symbols:
        symbols = FALLBACK_SYMBOLS

    balance   = get_balance()
    positions = get_all_positions()
    log.info(f"Balance: {balance:.4f} | Symbols: {len(symbols)} | Open: {len(positions)}")

    # Pre-cargar caches
    log.info("Pre-cargando klines 5m...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(lambda s: get_klines(s, 300), symbols[:100]))

    def _prefetch():
        log.info("Pre-cargando 15m + H1...")
        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(lambda s: get_mid_klines(s, 120), symbols[:80]))
            list(ex.map(lambda s: get_h1_klines(s, 60),  symbols[:80]))
        log.info("Caches listos.")
    threading.Thread(target=_prefetch, daemon=True).start()

    start_ws_cache(symbols)
    time.sleep(2)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))

    regime = get_market_regime()
    tg_startup(balance, symbols, regime)

    last_daily_report = datetime.now(timezone.utc).hour
    errors = 0

    while True:
        t0 = time.time()
        try:
            # Reporte diario a las 08:00 UTC
            now_hour = datetime.now(timezone.utc).hour
            if now_hour == 8 and last_daily_report != 8:
                tg(risk_mgr.daily_summary())
                last_daily_report = 8
            elif now_hour != 8:
                last_daily_report = now_hour

            risk_mgr.reset_if_new_day()
            balance   = get_balance()
            positions = get_all_positions()
            open_count= len(positions)
            regime    = get_market_regime()

            # ── Limpiar registro de posiciones cerradas ────────────────
            with position_risk_lock:
                for s in [k for k in position_risk if k not in positions]:
                    del position_risk[s]

            # ── Partial TP / Trailing ──────────────────────────────────
            if positions:
                update_positions(positions)

            log.info(f"── V13 [{regime}] {balance:.4f} USDT | {open_count}/{MAX_OPEN_TRADES} ──")

            # ── Session filter ─────────────────────────────────────────
            if not is_session_ok():
                hour = datetime.now(timezone.utc).hour
                log.info(f"Session OFF ({hour}:00 UTC). Esperando {LOOP_SECONDS}s...")
                time.sleep(LOOP_SECONDS)
                continue

            # ── Risk Manager check ─────────────────────────────────────
            can, reason = risk_mgr.can_trade(balance)
            if not can:
                log.warning(f"Risk Manager: {reason}")
                time.sleep(LOOP_SECONDS)
                continue

            # ── Scan ───────────────────────────────────────────────────
            signals = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(scan_symbol, s, regime): s for s in symbols}
                for f in as_completed(futs):
                    r = f.result()
                    if r:
                        signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Signals: {len(signals)}/{len(symbols)}")

            if signals:
                tg_scan(signals, len(symbols), open_count, regime)

            # ── Correlation filter ─────────────────────────────────────
            long_count  = sum(1 for p in positions.values() if p.get("positionSide") == "LONG")
            short_count = sum(1 for p in positions.values() if p.get("positionSide") == "SHORT")

            entered      : set  = set()
            skip_reasons : dict = {}
            orders_opened = 0
            size_mult = risk_mgr.size_multiplier()
            if size_mult < 1.0:
                log.warning(f"Tamaño reducido al {size_mult*100:.0f}% por racha")

            for sig in signals:
                sym = sig["symbol"]

                if sym in positions:
                    skip_reasons[sym] = "ya en posición"; continue
                if sym in entered:
                    skip_reasons[sym] = "ya intentado";   continue
                if open_count >= MAX_OPEN_TRADES:
                    log.info(f"Max trades ({MAX_OPEN_TRADES}) alcanzado."); break
                if balance < 2:
                    skip_reasons[sym] = "balance bajo"; break

                # Correlation
                if USE_CORRELATION:
                    if sig["signal"] == "LONG"  and long_count  >= MAX_SAME_DIR:
                        skip_reasons[sym] = f"max longs ({MAX_SAME_DIR}) ya abiertos"; continue
                    if sig["signal"] == "SHORT" and short_count >= MAX_SAME_DIR:
                        skip_reasons[sym] = f"max shorts ({MAX_SAME_DIR}) ya abiertos"; continue

                spread = get_spread_pct(sym)
                if spread > MAX_SPREAD_PCT:
                    skip_reasons[sym] = f"spread {spread:.3f}%"; continue

                side = "BUY" if sig["signal"] == "LONG" else "SELL"
                try:
                    set_lev(sym)
                    live_price = get_live_price(sym)

                    # Drift filter
                    drift = abs(live_price - sig["close"]) / sig["close"] * 100
                    if drift > MAX_ENTRY_DRIFT:
                        skip_reasons[sym] = f"drift {drift:.2f}%"; continue

                    # Recalcular SL/TP con precio live
                    dist_sl = abs(sig["close"] - sig["sl"]) / sig["close"]
                    if sig["signal"] == "LONG":
                        sl_live = live_price * (1 - dist_sl)
                        tp_live = live_price + (live_price - sl_live) * TP_MULT
                    else:
                        sl_live = live_price * (1 + dist_sl)
                        tp_live = live_price - (sl_live - live_price) * TP_MULT
                    sl_live = round(sl_live, 6)
                    tp_live = round(tp_live, 6)

                    rr_live = abs(tp_live-live_price)/abs(live_price-sl_live)
                    if rr_live < MIN_RR:
                        skip_reasons[sym] = f"RR live {rr_live:.2f} < {MIN_RR}"; continue

                    qty, notional = calc_qty(balance, live_price, sl_live, size_mult)
                    if qty <= 0:
                        skip_reasons[sym] = "qty=0"; continue

                    log.info(f"→ {sym} {side} qty={qty:.4f} "
                             f"live={live_price:.4f} sl={sl_live:.4f} tp={tp_live:.4f} "
                             f"drift={drift:.2f}% score={sig['score']}")

                    res = open_order_retry(sym, side, qty, sl_live, tp_live)
                    log.info(f"✅ {sym} abierto | {res}")

                    with position_risk_lock:
                        position_risk[sym] = {
                            "entry":        live_price,
                            "sl_initial":   sl_live,
                            "side":         "LONG" if side == "BUY" else "SHORT",
                            "be_done":      False,
                            "partial_done": False,
                            "qty":          qty,
                        }

                    sig.update({
                        "close": live_price, "sl": sl_live,
                        "tp": tp_live,
                        "dist_pct": round(abs(live_price-sl_live)/live_price*100, 3),
                        "rr": round(rr_live, 2),
                    })
                    tg_entry(sig, qty, notional, balance, spread)

                    entered.add(sym)
                    open_count += 1
                    orders_opened += 1
                    if sig["signal"] == "LONG":
                        long_count += 1
                    else:
                        short_count += 1
                    time.sleep(0.5)

                except Exception as e:
                    reason = str(e)[:100]
                    log.error(f"Order FAILED {sym}: {e}")
                    skip_reasons[sym] = f"error: {reason}"
                    if "stop" in reason.lower() or "liquidat" in reason.lower():
                        sl_cooldown[sym] = datetime.now(timezone.utc)
                    tg(f"⚠️ <b>Error {sym}</b>: <code>{str(e)[:150]}</code>")

            if signals and orders_opened == 0 and skip_reasons:
                log.warning(f"Señales={len(signals)} 0 órdenes: {skip_reasons}")
                tg_diag(signals, skip_reasons)

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Bot V13 detenido</b>\n" + risk_mgr.daily_summary())
            break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle error #{errors}: {e}")
            if errors <= 3:
                tg(f"⚠️ <b>Error #{errors}</b>: <code>{str(e)[:200]}</code>")
            if errors >= 10:
                tg("🔴 <b>CRÍTICO: 10 errores consecutivos. Detenido.</b>")
                break

        time.sleep(max(0, LOOP_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
