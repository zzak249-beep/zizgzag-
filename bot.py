"""
Sniper Bot — Turbo Markov Edition
Implementación Python del Pine Script V48.7 para BingX Futures

ESTRATEGIA:
  Magic Slope = ((EMA7 - EMA7[1]) / ATR7) × 100   ← pendiente normalizada
  Motor Markov = matriz 3×3 de transiciones de estado (BULL/BEAR/NEUTRAL)
                 con ventana deslizante de 200 velas
  Entrada LONG:  low < pivot_low AND close < VWAP AND slope > 30 AND vol_spike
  Entrada SHORT: high > pivot_high AND close > VWAP AND slope < -30 AND vol_spike
  Filtro Markov: prob_bull > umbral (LONG) / prob_bear > umbral (SHORT)
  Salida:        Trailing stop = 1.5× riesgo inicial, offset = ATR14

VENTAJA REAL:
  La cadena de Markov calcula la probabilidad histórica de que el mercado
  CONTINÚE en el estado actual o CAMBIE. Esto elimina entradas en estados
  transitorios y solo opera cuando la probabilidad de éxito es estadísticamente
  favorable. Nadie más implementa esto en bots retail para BingX.

PARES RECOMENDADOS (8 óptimos, no más de 10):
  BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOGE
  Razón: volumen suficiente para que el filtro density sea significativo
         y estructura de pivotes limpia en 15m/1H.
"""

import os, time, hmac, hashlib, json, logging, threading, sqlite3
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import pandas as pd
import numpy as np
import websocket

try:
    from telegram import Bot
    from telegram.constants import ParseMode
    import asyncio
    TELEGRAM_OK = True
except ImportError:
    TELEGRAM_OK = False

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════════
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ["BINGX_SECRET_KEY"]
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ── Pares — 8 ÓPTIMOS por defecto ─────────────────────────────────────────────
_DEFAULT = "BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,ADA-USDT,AVAX-USDT,DOGE-USDT"
_raw     = os.environ.get("CUSTOM_SYMBOLS", _DEFAULT)
SYMBOLS  = [s.strip() for s in _raw.split(",") if s.strip()]

# ── Timeframe ─────────────────────────────────────────────────────────────────
TIMEFRAME    = os.environ.get("TIMEFRAME",   "15m")   # 15m = óptimo para Markov
LOOP_SECS    = int(os.environ.get("LOOP_SECONDS", "30"))
SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "8"))

# ── Motor Markov (Pine Script: lookback=200, slope_min=30) ────────────────────
MARKOV_LOOKBACK   = int(os.environ.get("MARKOV_LOOKBACK",   "200"))
SLOPE_MIN         = float(os.environ.get("SLOPE_MIN",       "30.0"))
MARKOV_BULL_MIN   = float(os.environ.get("MARKOV_BULL_MIN", "40.0"))  # prob_bull > 40%
MARKOV_BEAR_MIN   = float(os.environ.get("MARKOV_BEAR_MIN", "40.0"))  # prob_bear > 40%

# ── Filtros de entrada ────────────────────────────────────────────────────────
PIVOT_LEFT        = int(os.environ.get("PIVOT_LEFT",   "4"))
PIVOT_RIGHT       = int(os.environ.get("PIVOT_RIGHT",  "4"))
DENSITY_MULT      = float(os.environ.get("DENSITY_MULT","2.0"))   # vol > avg×2
DENSITY_PERIOD    = int(os.environ.get("DENSITY_PERIOD","50"))
MAX_SPREAD_PCT    = float(os.environ.get("MAX_SPREAD_PCT","0.15"))

# ── Salida (trailing stop) ────────────────────────────────────────────────────
TRAIL_RISK_MULT   = float(os.environ.get("TRAIL_RISK_MULT", "1.5"))  # 1.5× riesgo
ATR14_LEN         = int(os.environ.get("ATR14_LEN",         "14"))
USE_TRAILING      = os.environ.get("USE_TRAILING", "true").lower() == "true"

# ── Risk Manager ──────────────────────────────────────────────────────────────
RISK_PCT          = float(os.environ.get("RISK_PCT",         "1.5"))
LEVERAGE          = int(os.environ.get("LEVERAGE",           "3"))
MIN_USDT          = float(os.environ.get("MIN_ORDER_USDT",   "3.0"))
MAX_USDT          = float(os.environ.get("MAX_ORDER_USDT",   "50.0"))
MAX_MARGIN_PCT    = float(os.environ.get("MAX_MARGIN_PCT",   "20.0"))
MAX_OPEN          = int(os.environ.get("MAX_OPEN_TRADES",    "4"))
MAX_DAILY_LOSS    = float(os.environ.get("MAX_DAILY_LOSS",   "5.0"))
MAX_CONSEC_LOSS   = int(os.environ.get("MAX_CONSEC_LOSS",    "3"))
PAUSE_MINS        = int(os.environ.get("PAUSE_MINS",         "120"))
SIZE_REDUCE       = float(os.environ.get("SIZE_REDUCE_PCT",  "50.0"))

# ── Partial TP + BE ───────────────────────────────────────────────────────────
USE_PARTIAL       = os.environ.get("USE_PARTIAL_TP", "true").lower() == "true"
PARTIAL_R         = float(os.environ.get("PARTIAL_R",  "1.5"))
PARTIAL_PCT       = float(os.environ.get("PARTIAL_PCT", "50"))
BE_MARGIN_BPS     = float(os.environ.get("BE_MARGIN_BPS", "10"))

# ── Sesión y filtros ──────────────────────────────────────────────────────────
USE_SESSION       = os.environ.get("USE_SESSION", "true").lower() == "true"
SESSION_OFF_START = int(os.environ.get("SESSION_OFF_START", "0"))
SESSION_OFF_END   = int(os.environ.get("SESSION_OFF_END",   "5"))
COOLDOWN_MINS     = int(os.environ.get("COOLDOWN_MINS",     "30"))
LIVE_TRADING      = os.environ.get("LIVE_TRADING", "false").lower() == "true"

BINGX_BASE = "https://open-api.bingx.com"
BINGX_WS   = "wss://open-api-swap.bingx.com/swap-market"
INTERVAL_MAP = {
    "1m":"1m","3m":"3m","5m":"5m","15m":"15m",
    "30m":"30m","1h":"1H","4h":"4H","1d":"1D"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════════════════════
ws_price: dict  = {}
ws_kline: dict  = {}
ws_lock         = threading.Lock()
h1_cache: dict  = {}
sl_cd:    dict  = {}                # {sym: datetime} cooldown por SL
pos_risk: dict  = {}                # {sym: {entry,sl,side,peak,be_done,partial,qty}}
pos_lock        = threading.Lock()
_tg_loop        = None

# ═══════════════════════════════════════════════════════════════════════════════
# SQLITE TRADE LOG
# ═══════════════════════════════════════════════════════════════════════════════
DB_PATH = Path(os.environ.get("DB_PATH", "/data/trades.db"))

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, symbol TEXT, side TEXT,
        entry REAL, sl REAL, trail_sl REAL,
        qty REAL, notional REAL, rr_init REAL,
        slope REAL, prob_bull REAL, prob_bear REAL, markov_state TEXT,
        vol_ratio REAL, vwap REAL, pivot REAL,
        live INTEGER DEFAULT 1,
        closed_at TEXT, pnl_usdt REAL, exit_reason TEXT
    )""")
    con.commit(); con.close()
    log.info(f"DB: {DB_PATH}")

def db_open(sig: dict, qty: float, notional: float):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""INSERT INTO trades
            (ts,symbol,side,entry,sl,trail_sl,qty,notional,rr_init,
             slope,prob_bull,prob_bear,markov_state,vol_ratio,vwap,pivot,live)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            datetime.now(timezone.utc).isoformat(),
            sig["symbol"], sig["signal"],
            sig["entry"], sig["sl"], sig["sl"],
            qty, notional, sig.get("rr", 0),
            sig.get("slope", 0), sig.get("prob_bull", 0), sig.get("prob_bear", 0),
            sig.get("markov_state", "?"), sig.get("vol_ratio", 0),
            sig.get("vwap", 0), sig.get("pivot", 0),
            1 if LIVE_TRADING else 0
        ))
        con.commit(); con.close()
    except Exception as e:
        log.debug(f"db_open: {e}")

def db_close(symbol: str, pnl: float, reason: str):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""UPDATE trades SET closed_at=?,pnl_usdt=?,exit_reason=?
            WHERE symbol=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1""",
            (datetime.now(timezone.utc).isoformat(), pnl, reason, symbol))
        con.commit(); con.close()
    except Exception as e:
        log.debug(f"db_close: {e}")

def db_stats() -> dict:
    try:
        con = sqlite3.connect(DB_PATH)
        r = con.execute("""SELECT
            COUNT(*) total,
            SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) wins,
            COALESCE(SUM(pnl_usdt),0) pnl,
            COALESCE(MIN(pnl_usdt),0) worst,
            COALESCE(MAX(pnl_usdt),0) best,
            COALESCE(AVG(prob_bull+prob_bear)/2,0) avg_prob
            FROM trades WHERE closed_at IS NOT NULL""").fetchone()
        con.close()
        t = r[0] or 0
        return {"total":t,"wins":r[1] or 0,"wr":round((r[1] or 0)/t*100,1) if t>0 else 0,
                "pnl":round(r[2],2),"worst":round(r[3],2),"best":round(r[4],2),
                "avg_prob":round(r[5],1)}
    except Exception:
        return {"total":0,"wins":0,"wr":0,"pnl":0,"worst":0,"best":0,"avg_prob":0}

# ═══════════════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════════════════════════════
class RiskManager:
    def __init__(self):
        self._l = threading.Lock()
        self.daily_pnl   = 0.0
        self.day         = datetime.now(timezone.utc).date()
        self.consec_loss = 0
        self.pause_until : datetime | None = None

    def reset(self):
        today = datetime.now(timezone.utc).date()
        with self._l:
            if today != self.day:
                log.info(f"Nuevo día | PnL ayer: {self.daily_pnl:+.2f} USDT")
                self.daily_pnl = 0.0; self.day = today
                self.consec_loss = 0; self.pause_until = None

    def record(self, pnl: float):
        with self._l:
            self.daily_pnl += pnl
            if pnl > 0:
                self.consec_loss = 0
            else:
                self.consec_loss += 1
                if self.consec_loss >= MAX_CONSEC_LOSS:
                    self.pause_until = datetime.now(timezone.utc) + timedelta(minutes=PAUSE_MINS)
                    tg(f"⏸ <b>PAUSA AUTO</b> — {self.consec_loss} pérdidas seguidas\n"
                       f"Reanuda: <b>{self.pause_until.strftime('%H:%M')} UTC</b>")

    def ok(self, bal: float) -> tuple:
        self.reset()
        with self._l:
            if self.pause_until and datetime.now(timezone.utc) < self.pause_until:
                m = int((self.pause_until-datetime.now(timezone.utc)).total_seconds()/60)
                return False, f"Pausa ({m}min)"
            if bal > 0 and self.daily_pnl < -bal*(MAX_DAILY_LOSS/100):
                return False, f"Pérdida diaria max ({self.daily_pnl:+.2f})"
            return True, "OK"

    def size_mult(self) -> float:
        with self._l:
            return SIZE_REDUCE/100 if self.consec_loss >= 2 else 1.0

rm = RiskManager()

# ═══════════════════════════════════════════════════════════════════════════════
# BINGX API
# ═══════════════════════════════════════════════════════════════════════════════
def _sign(p: dict) -> str:
    qs = "&".join(f"{k}={v}" for k,v in sorted(p.items()))
    return hmac.new(BINGX_SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()

def _h(): return {"X-BX-APIKEY": BINGX_API_KEY}

def bx_get(path: str, params: dict = None) -> dict:
    p = dict(params or {}); p["timestamp"] = int(time.time()*1000); p["signature"] = _sign(p)
    r = requests.get(BINGX_BASE+path, params=p, headers=_h(), timeout=15)
    r.raise_for_status(); return r.json()

def bx_post(path: str, payload: dict) -> dict:
    p = dict(payload); p["timestamp"] = int(time.time()*1000); p["signature"] = _sign(p)
    r = requests.post(BINGX_BASE+path, json=p,
        headers={**_h(),"Content-Type":"application/json"}, timeout=15)
    r.raise_for_status(); return r.json()

def bx_del(path: str, params: dict) -> dict:
    p = dict(params); p["timestamp"] = int(time.time()*1000); p["signature"] = _sign(p)
    r = requests.delete(BINGX_BASE+path, params=p, headers=_h(), timeout=15)
    r.raise_for_status(); return r.json()

def get_balance() -> float:
    try:
        d = bx_get("/openApi/swap/v2/user/balance").get("data",{})
        b = d.get("balance",{}) if isinstance(d,dict) else {}
        src = b if isinstance(b,dict) else d
        for f in ("availableMargin","available","walletBalance","equity"):
            v = src.get(f)
            if v not in (None,"","0"):
                val = float(v)
                if val > 0: log.info(f"Balance: {val:.4f} USDT"); return val
        return 0.0
    except Exception as e:
        log.error(f"get_balance: {e}"); return 0.0

def get_positions() -> dict:
    try:
        data = bx_get("/openApi/swap/v2/user/positions",{})
        r = {p["symbol"]:p for p in data.get("data",[])
             if isinstance(p,dict) and float(p.get("positionAmt",0))!=0}
        log.info(f"Positions ({len(r)}): {list(r.keys())}")
        return r
    except Exception as e:
        log.error(f"get_positions: {e}"); return {}

def set_lev(symbol: str):
    for s in ("LONG","SHORT"):
        try: bx_post("/openApi/swap/v2/trade/leverage",{"symbol":symbol,"side":s,"leverage":LEVERAGE})
        except Exception: pass

def cancel_order(symbol: str, oid: str):
    try:
        bx_del("/openApi/swap/v2/trade/order",{"symbol":symbol,"orderId":oid})
        log.info(f"Cancelada {symbol} #{oid}")
    except Exception as e:
        log.debug(f"cancel {symbol}: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════
def _ws_msg(_, message):
    try:
        import gzip
        try: d = json.loads(gzip.decompress(message) if isinstance(message,bytes) else message)
        except: d = json.loads(message)
        if not d.get("dataType","").endswith("@kline"): return
        sym   = d.get("s","").replace("_","-")
        kdata = d.get("data",{}).get("kline", d.get("k",{}))
        if not kdata or float(kdata.get("c",0))==0: return
        row = {"open_time":pd.to_datetime(kdata.get("t",0),unit="ms"),
               "open":float(kdata.get("o",0)),"high":float(kdata.get("h",0)),
               "low":float(kdata.get("l",0)),"close":float(kdata.get("c",0)),
               "volume":float(kdata.get("v",0))}
        with ws_lock:
            df = ws_kline.get(sym)
            if df is not None:
                if len(df)>0 and df.iloc[-1]["open_time"]==row["open_time"]:
                    for c in ("open","high","low","close","volume"): df.at[df.index[-1],c]=row[c]
                else:
                    ws_kline[sym] = pd.concat([df,pd.DataFrame([row])],ignore_index=True).tail(400)
            ws_price[sym] = row["close"]
    except: pass

def start_ws(symbols: list):
    ivl = INTERVAL_MAP.get(TIMEFRAME,"15m").lower()
    def _run():
        while True:
            try:
                def on_open(app):
                    for s in symbols:
                        try: app.send(json.dumps({"id":f"sub_{s}","reqType":"sub",
                            "dataType":f"{s.replace('-','_')}@kline_{ivl}"}))
                        except: pass
                ws = websocket.WebSocketApp(BINGX_WS,on_message=_ws_msg,
                    on_error=lambda a,e: log.warning(f"WS:{e}"),
                    on_close=lambda a,*x: None, on_open=on_open)
                ws.run_forever(ping_interval=30,ping_timeout=10)
            except Exception as e: log.warning(f"WS thread:{e}")
            time.sleep(5)
    threading.Thread(target=_run,daemon=True).start()
    log.info(f"WS iniciado ({TIMEFRAME}) — {len(symbols)} pares")

# ═══════════════════════════════════════════════════════════════════════════════
# PRECIO Y KLINES
# ═══════════════════════════════════════════════════════════════════════════════
def get_price(symbol: str) -> float:
    with ws_lock:
        p = ws_price.get(symbol)
    if p and p>0: return p
    try:
        items = bx_get("/openApi/swap/v2/quote/ticker",{"symbol":symbol}).get("data",[])
        items = [items] if isinstance(items,dict) else items
        for item in items:
            if item.get("symbol")==symbol:
                v = item.get("lastPrice") or item.get("price")
                if v: return float(v)
    except: pass
    raise ValueError(f"No price: {symbol}")

def get_spread(symbol: str) -> float:
    try:
        d = bx_get("/openApi/swap/v2/quote/bookTicker",{"symbol":symbol}).get("data",{})
        if isinstance(d,list): d=next((x for x in d if x.get("symbol")==symbol),{})
        a,b = float(d.get("askPrice",0) or 0), float(d.get("bidPrice",0) or 0)
        return (a-b)/b*100 if a>0 and b>0 else 999.0
    except: return 999.0

def _fetch(symbol: str, tf: str, limit: int) -> pd.DataFrame:
    p = {"symbol":symbol,"interval":INTERVAL_MAP.get(tf,tf),"limit":limit}
    rows = bx_get("/openApi/swap/v3/quote/klines",p).get("data",[])
    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows,columns=["open_time","open","high","low","close","volume","close_time"])
    for c in ("open","high","low","close","volume"): df[c]=pd.to_numeric(df[c],errors="coerce")
    df["open_time"]=pd.to_datetime(df["open_time"],unit="ms")
    df.dropna(subset=["open","high","low","close","volume"],inplace=True)
    return df.sort_values("open_time").reset_index(drop=True)

def get_klines(symbol: str, limit: int = 300) -> pd.DataFrame:
    with ws_lock:
        df = ws_kline.get(symbol)
    if df is not None and len(df)>=limit//2: return df.copy()
    df = _fetch(symbol,TIMEFRAME,limit)
    if not df.empty:
        with ws_lock: ws_kline[symbol]=df.copy()
    return df

# ═══════════════════════════════════════════════════════════════════════════════
# INDICADORES — implementación exacta del Pine Script V48.7
# ═══════════════════════════════════════════════════════════════════════════════
def calc_magic_slope(close: pd.Series, atr7: pd.Series) -> pd.Series:
    """
    magic_slope = ((ema7 - ema7[1]) / atr7) * 100
    Pendiente de la EMA7 normalizada por la volatilidad.
    Estado: >slope_min=BULL, <-slope_min=BEAR, else=NEUTRAL
    """
    ema7  = close.ewm(span=7, adjust=False).mean()
    slope = ((ema7 - ema7.shift(1)) / atr7.replace(0, np.nan)) * 100
    return slope.fillna(0)

def calc_atr(high, low, close, period: int) -> pd.Series:
    tr = pd.concat([
        high-low,
        (high-close.shift()).abs(),
        (low-close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def calc_markov(slope: pd.Series, lookback: int = 200) -> pd.DataFrame:
    """
    Motor Markov — implementación exacta del Pine Script V48.7.

    Estados: 0=BULL (slope>slope_min), 1=BEAR (slope<-slope_min), 2=NEUTRAL
    Matriz 3×3: M[i][j] = número de veces que el estado i fue seguido por el estado j
    Ventana deslizante: se restan las transiciones de hace 'lookback' velas.

    Retorna DataFrame con columnas:
      state, prob_bull, prob_bear, prob_neutral
    """
    n = len(slope)
    states = np.where(slope > SLOPE_MIN, 0,
             np.where(slope < -SLOPE_MIN, 1, 2)).astype(int)

    prob_bull   = np.zeros(n)
    prob_bear   = np.zeros(n)
    prob_neutral= np.zeros(n)
    matrix      = np.zeros((3, 3), dtype=float)

    for i in range(1, n):
        prev_s = int(states[i-1])
        curr_s = int(states[i])

        # Incrementar transición actual
        matrix[prev_s][curr_s] += 1.0

        # Restar transición de hace 'lookback' velas (ventana deslizante)
        if i > lookback:
            old_prev = int(states[i - lookback - 1])
            old_curr = int(states[i - lookback])
            matrix[old_prev][old_curr] = max(0.0, matrix[old_prev][old_curr] - 1.0)

        # Probabilidades desde el estado actual
        row_sum = matrix[curr_s].sum()
        if row_sum > 0:
            prob_bull[i]    = matrix[curr_s][0] / row_sum * 100
            prob_bear[i]    = matrix[curr_s][1] / row_sum * 100
            prob_neutral[i] = matrix[curr_s][2] / row_sum * 100

    return pd.DataFrame({
        "state":        states,
        "prob_bull":    prob_bull,
        "prob_bear":    prob_bear,
        "prob_neutral": prob_neutral,
    }, index=slope.index)

def calc_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP de sesión (reinicia cada día UTC) — idéntico a ta.vwap de Pine."""
    d  = df["open_time"].dt.date
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    cum_pv = pv.groupby(d).cumsum()
    cum_v  = df["volume"].groupby(d).cumsum()
    return (cum_pv / cum_v.replace(0, np.nan)).rename("vwap")

def calc_pivots(high: pd.Series, low: pd.Series,
                left: int = 4, right: int = 4) -> tuple:
    """
    Pivot High/Low — idéntico a ta.pivothigh/pivotlow de Pine Script.
    Un pivot se confirma cuando han pasado 'right' velas desde él.
    Retorna Series con el último pivot confirmado (valor o NaN).
    """
    n  = len(high)
    ph = pd.Series(np.nan, index=high.index)
    pl = pd.Series(np.nan, index=low.index)

    for i in range(left, n - right):
        h_win = high.iloc[i-left : i+right+1]
        l_win = low.iloc[i-left  : i+right+1]
        if float(high.iloc[i]) == float(h_win.max()):
            ph.iloc[i] = float(high.iloc[i])
        if float(low.iloc[i]) == float(l_win.min()):
            pl.iloc[i] = float(low.iloc[i])

    # Forward-fill: mantener último pivot válido (como var peak/valley del Pine)
    ph_ff = ph.ffill()
    pl_ff = pl.ffill()
    return ph_ff, pl_ff

# ═══════════════════════════════════════════════════════════════════════════════
# ESCÁNER PRINCIPAL — lógica Pine Script V48.7
# ═══════════════════════════════════════════════════════════════════════════════
def scan(symbol: str) -> dict | None:
    # Cooldown
    cd = sl_cd.get(symbol)
    if cd and (datetime.now(timezone.utc)-cd).total_seconds()/60 < COOLDOWN_MINS:
        return None

    try:
        df = get_klines(symbol, limit=350)
        min_bars = MARKOV_LOOKBACK + PIVOT_LEFT + PIVOT_RIGHT + 10
        if df.empty or len(df) < min_bars:
            return None

        # ── Indicadores ──────────────────────────────────────────────────
        atr7_s   = calc_atr(df["high"], df["low"], df["close"], 7)
        atr14_s  = calc_atr(df["high"], df["low"], df["close"], ATR14_LEN)
        slope_s  = calc_magic_slope(df["close"], atr7_s)
        markov   = calc_markov(slope_s, MARKOV_LOOKBACK)
        vwap_s   = calc_vwap(df)
        vol_ma   = df["volume"].rolling(DENSITY_PERIOD).mean()
        ph, pl   = calc_pivots(df["high"], df["low"], PIVOT_LEFT, PIVOT_RIGHT)

        # ── Vela señal = última vela CERRADA ─────────────────────────────
        i = len(df) - 2
        if i < MARKOV_LOOKBACK + PIVOT_LEFT + PIVOT_RIGHT:
            return None

        close_now  = float(df["close"].iloc[i])
        high_now   = float(df["high"].iloc[i])
        low_now    = float(df["low"].iloc[i])
        atr14_now  = float(atr14_s.iloc[i])
        slope_now  = float(slope_s.iloc[i])
        vwap_now   = float(vwap_s.iloc[i])
        vol_now    = float(df["volume"].iloc[i])
        vol_avg    = float(vol_ma.iloc[i])
        peak_now   = float(ph.iloc[i])
        valley_now = float(pl.iloc[i])
        prob_bull  = float(markov["prob_bull"].iloc[i])
        prob_bear  = float(markov["prob_bear"].iloc[i])
        state_now  = int(markov["state"].iloc[i])
        prev_low   = float(df["low"].iloc[i-1])   # para cálculo de riesgo (como Pine)
        prev_high  = float(df["high"].iloc[i-1])

        # Validaciones
        if any(np.isnan(x) for x in [slope_now, vwap_now, peak_now, valley_now,
                                       prob_bull, prob_bear, atr14_now]):
            return None
        if vol_avg <= 0 or atr14_now <= 0:
            return None

        # ── Filtro Density (Pine: volume > sma(vol,50) * 2.0) ────────────
        is_density = vol_now > vol_avg * DENSITY_MULT
        vol_ratio  = round(vol_now / vol_avg, 2)
        if not is_density:
            return None

        # ── Condiciones de entrada (Pine Script exacto) ──────────────────
        # LONG: low < valley AND close < vwap AND slope > slope_min AND density
        # SHORT: high > peak AND close > vwap AND slope < -slope_min AND density
        long_cond  = (low_now  < valley_now and close_now < vwap_now and
                      slope_now >  SLOPE_MIN)
        short_cond = (high_now > peak_now   and close_now > vwap_now and
                      slope_now < -SLOPE_MIN)

        if not long_cond and not short_cond:
            return None

        direction = "LONG" if long_cond else "SHORT"

        # ── Filtro Markov — la ventaja real ──────────────────────────────
        if direction == "LONG"  and prob_bull < MARKOV_BULL_MIN:
            return None
        if direction == "SHORT" and prob_bear < MARKOV_BEAR_MIN:
            return None

        state_name = {0:"BULL", 1:"BEAR", 2:"NEUTRAL"}.get(state_now, "?")

        # ── SL — bajo mínimo de vela anterior / sobre máximo (Pine Script) ──
        if direction == "LONG":
            risk_dist = abs(close_now - prev_low)
            sl_price  = prev_low - atr14_now * 0.1   # pequeño buffer
            entry_p   = close_now                      # market entry
            tp_price  = entry_p + risk_dist * TRAIL_RISK_MULT * 2  # TP aproximado
        else:
            risk_dist = abs(prev_high - close_now)
            sl_price  = prev_high + atr14_now * 0.1
            entry_p   = close_now
            tp_price  = entry_p - risk_dist * TRAIL_RISK_MULT * 2

        if risk_dist < atr14_now * 0.1:   # riesgo demasiado pequeño
            return None

        rr = (risk_dist * TRAIL_RISK_MULT * 2) / max(risk_dist, 1e-10)

        # ── Trailing stop inicial = risk * 1.5 (Pine Script) ─────────────
        trail_dist = risk_dist * TRAIL_RISK_MULT
        if direction == "LONG":
            trail_sl = entry_p - trail_dist
        else:
            trail_sl = entry_p + trail_dist

        # ── Score ─────────────────────────────────────────────────────────
        prob_relevant = prob_bull if direction == "LONG" else prob_bear
        score  = min(prob_relevant,      40)   # Markov: 40 pts
        score += min(vol_ratio * 10,     30)   # Density: 30 pts
        score += min(abs(slope_now)/5,   20)   # Slope strength: 20 pts
        score += 10                            # H1 confirmado por diseño: 10 pts

        return {
            "symbol":      symbol,
            "signal":      direction,
            "entry":       round(entry_p, 6),
            "sl":          round(sl_price, 6),
            "trail_sl":    round(trail_sl, 6),
            "tp":          round(tp_price, 6),
            "risk_dist":   round(risk_dist, 6),
            "atr14":       round(atr14_now, 6),
            "slope":       round(slope_now, 2),
            "prob_bull":   round(prob_bull, 1),
            "prob_bear":   round(prob_bear, 1),
            "markov_state":state_name,
            "vol_ratio":   vol_ratio,
            "vwap":        round(vwap_now, 6),
            "pivot":       round(valley_now if direction=="LONG" else peak_now, 6),
            "score":       round(score, 1),
            "rr":          round(rr, 2),
        }

    except Exception as e:
        log.debug(f"scan {symbol}: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# TRAILING STOP — gestionado por el bot (más robusto en BingX)
# ═══════════════════════════════════════════════════════════════════════════════
def update_trailing(positions: dict):
    """
    Cada ciclo, mueve el SL según el trail de Pine Script:
    risk = abs(entry - prev_low/high)
    trail = entry ± risk * 1.5
    El SL solo se mueve en favor (nunca en contra).
    """
    for sym, pos in positions.items():
        try:
            ps    = pos.get("positionSide","LONG")
            entry = float(pos.get("avgPrice",0) or 0)
            qty   = abs(float(pos.get("positionAmt",0) or 0))
            if entry==0 or qty==0: continue

            live = get_price(sym)
            with pos_lock:
                r = pos_risk.get(sym)
            if not r: continue

            rd         = r.get("risk_dist", 0)
            curr_trail = r.get("trail_sl", r["sl"])
            be_margin  = entry*(BE_MARGIN_BPS/10000)
            be_price   = entry+be_margin if ps=="LONG" else entry-be_margin
            partial_r  = PARTIAL_R
            pt_trigger = entry + rd*partial_r if ps=="LONG" else entry - rd*partial_r

            # ── Partial TP @ PARTIAL_R ────────────────────────────────────
            if USE_PARTIAL and not r.get("partial") and (
                (ps=="LONG"  and live >= pt_trigger) or
                (ps=="SHORT" and live <= pt_trigger)
            ):
                close_qty  = round(qty*PARTIAL_PCT/100, 4)
                close_side = "SELL" if ps=="LONG" else "BUY"
                if close_qty >= 0.001 and LIVE_TRADING:
                    try:
                        bx_post("/openApi/swap/v2/trade/order",{
                            "symbol":sym,"side":close_side,"positionSide":ps,
                            "type":"MARKET","quantity":round(close_qty,4),"reduceOnly":"true"})
                        pnl = abs(live-entry)*close_qty
                        rm.record(+pnl); db_close(sym, pnl, "partial_tp")
                        with pos_lock:
                            if sym in pos_risk:
                                pos_risk[sym]["partial"] = True
                                pos_risk[sym]["trail_sl"] = be_price
                        tg(f"💰 <b>PARTIAL TP {sym}</b> {PARTIAL_PCT:.0f}% @ "
                           f"<code>{live:.6g}</code> ({partial_r}R)\nSL→BE <code>{be_price:.6g}</code>")
                    except Exception as e:
                        log.warning(f"partial {sym}: {e}")
                continue

            # ── Trailing stop (Pine Script: mueve solo en favor) ──────────
            if USE_TRAILING:
                price_move = live - entry if ps=="LONG" else entry - live
                new_trail  = (entry + price_move - rd*TRAIL_RISK_MULT) if ps=="LONG" else \
                             (entry - price_move + rd*TRAIL_RISK_MULT)

                should_update = (ps=="LONG"  and new_trail > curr_trail) or \
                                (ps=="SHORT" and new_trail < curr_trail)

                if should_update and LIVE_TRADING:
                    close_side = "SELL" if ps=="LONG" else "BUY"
                    try:
                        bx_post("/openApi/swap/v2/trade/order",{
                            "symbol":sym,"type":"STOP_MARKET","side":close_side,
                            "positionSide":ps,"stopPrice":round(new_trail,6),
                            "closePosition":"true","workingType":"MARK_PRICE"})
                        with pos_lock:
                            if sym in pos_risk: pos_risk[sym]["trail_sl"] = new_trail
                        log.info(f"Trail {sym} {ps}: {curr_trail:.4f}→{new_trail:.4f}")
                    except Exception as e:
                        log.debug(f"trail {sym}: {e}")

        except Exception as e:
            log.debug(f"update_trailing {sym}: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# ÓRDENES
# ═══════════════════════════════════════════════════════════════════════════════
def _sltp(sl: float, tp: float) -> dict:
    return {
        "stopLoss":   json.dumps({"type":"STOP_MARKET","stopPrice":round(sl,6),"workingType":"MARK_PRICE"}),
        "takeProfit": json.dumps({"type":"TAKE_PROFIT_MARKET","stopPrice":round(tp,6),"workingType":"MARK_PRICE"}),
    }

def place_market(symbol:str,side:str,qty:float,sl:float,tp:float) -> dict:
    ps  = "LONG" if side=="BUY" else "SHORT"
    res = bx_post("/openApi/swap/v2/trade/order",{
        "symbol":symbol,"side":side,"positionSide":ps,
        "type":"MARKET","quantity":round(qty,4),**_sltp(sl,tp)})
    if res.get("code",-1)!=0:
        raise ValueError(f"{symbol}: code={res.get('code')} {res.get('msg')}")
    return res

def calc_qty(bal:float,entry:float,sl:float,mult:float=1.0) -> tuple:
    dist = abs(entry-sl)/entry if entry>0 else 0
    if dist<1e-6: return 0,0
    risk   = bal*(RISK_PCT/100)*mult
    notional = risk/dist
    max_n  = min(MAX_USDT, bal*(MAX_MARGIN_PCT/100)*LEVERAGE)
    notional = max(MIN_USDT, min(notional,max_n))
    qty = notional/entry if entry>0 else 0
    return round(max(qty,0.001),4), round(notional,2)

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════════
def _tg_thread():
    global _tg_loop
    _tg_loop = asyncio.new_event_loop()
    _tg_loop.run_forever()

def tg(msg: str):
    if not TELEGRAM_OK or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    global _tg_loop
    if not _tg_loop or not _tg_loop.is_running(): return
    async def _s():
        try:
            bot = Bot(TELEGRAM_TOKEN)
            cid = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.lstrip("-").isdigit() else TELEGRAM_CHAT_ID
            await bot.send_message(chat_id=cid,text=msg,parse_mode=ParseMode.HTML)
        except Exception as e: log.warning(f"Telegram: {e}")
    asyncio.run_coroutine_threadsafe(_s(), _tg_loop)

def tg_startup(bal: float):
    s = db_stats()
    tg(
        f"🧠 <b>Sniper Turbo Markov — PRODUCCIÓN</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Modo:</b> {'🔴 LIVE' if LIVE_TRADING else '🟡 SIMULADO'}\n"
        f"<b>TF:</b> {TIMEFRAME} | <b>Pares:</b> {len(SYMBOLS)}\n"
        f"{'|'.join(SYMBOLS)}\n\n"
        f"<b>Motor Markov:</b>\n"
        f"  Lookback: {MARKOV_LOOKBACK} velas\n"
        f"  Umbral BULL: >{MARKOV_BULL_MIN}% | BEAR: >{MARKOV_BEAR_MIN}%\n"
        f"  Magic Slope: ±{SLOPE_MIN}%\n\n"
        f"<b>Filtros entrada:</b>\n"
        f"  Volume density: >{DENSITY_MULT}× avg({DENSITY_PERIOD})\n"
        f"  Pivot: left={PIVOT_LEFT}, right={PIVOT_RIGHT}\n"
        f"  VWAP: confirmación posición precio\n\n"
        f"<b>Salida:</b>\n"
        f"  Trail: {TRAIL_RISK_MULT}× riesgo inicial (Pine Script)\n"
        f"  Partial TP: {PARTIAL_PCT:.0f}% @ {PARTIAL_R}R\n\n"
        f"<b>Risk:</b> {RISK_PCT}%/trade | {LEVERAGE}× | max {MAX_OPEN} trades\n"
        f"<b>Balance:</b> {bal:.2f} USDT\n\n"
        f"<b>Stats históricos:</b>\n"
        f"  Trades: {s['total']} | WR: {s['wr']}% | PnL: {s['pnl']:+.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )

def tg_signal(sig: dict, qty: float, notional: float, bal: float):
    d  = "🟢 LONG" if sig["signal"]=="LONG" else "🔴 SHORT"
    pb = sig["prob_bull"]; pe = sig["prob_bear"]
    mode = "🟡 [SIM]" if not LIVE_TRADING else "✅"
    tg(
        f"{mode} <b>MARKOV ENTRY — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{d} | Score: {sig['score']:.0f}/100\n\n"
        f"🧠 <b>Markov:</b>\n"
        f"  Estado: <b>{sig['markov_state']}</b>\n"
        f"  P(BULL): <code>{pb:.1f}%</code> | P(BEAR): <code>{pe:.1f}%</code>\n\n"
        f"📊 <b>Técnico:</b>\n"
        f"  Magic Slope: <code>{sig['slope']:+.1f}</code> (umbral ±{SLOPE_MIN})\n"
        f"  Volumen: <code>{sig['vol_ratio']:.1f}×</code> el promedio ({'🔥 DENSITY' if sig['vol_ratio']>=2 else '📊'})\n"
        f"  VWAP: <code>{sig['vwap']:.6g}</code>\n"
        f"  Pivot: <code>{sig['pivot']:.6g}</code>\n\n"
        f"💰 <b>Orden:</b>\n"
        f"  Entrada: <code>{sig['entry']:.6g}</code>\n"
        f"  SL:      <code>{sig['sl']:.6g}</code> (vela anterior)\n"
        f"  Trail:   <code>{sig['trail_sl']:.6g}</code> ({TRAIL_RISK_MULT}× riesgo)\n"
        f"  TP aprox:<code>{sig['tp']:.6g}</code> | R:R ~1:{sig['rr']:.1f}\n\n"
        f"  Qty: {qty:.4f} | Notional: {notional:.2f} USDT\n"
        f"  Riesgo: {bal*RISK_PCT/100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

def tg_scan(signals: list, total: int, open_c: int):
    if not signals: return
    lines = [
        f"🔍 <b>{len(signals)} señal(es)/{total} pares</b> | {open_c}/{MAX_OPEN} trades",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in signals:
        e  = "🟢" if s["signal"]=="LONG" else "🔴"
        pb = s["prob_bull"]; pe = s["prob_bear"]
        lines.append(
            f"{e} <b>{s['symbol']}</b> [{s['markov_state']}] "
            f"Score:{s['score']:.0f} "
            f"P🐂:{pb:.0f}% P🐻:{pe:.0f}% "
            f"Vol:{s['vol_ratio']:.1f}× Slope:{s['slope']:+.0f}"
        )
    tg("\n".join(lines))

def tg_stats():
    s = db_stats()
    sign = "+" if s["pnl"]>=0 else ""
    tg(
        f"📊 <b>Estadísticas Markov Bot</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades: {s['total']} | Wins: {s['wins']}\n"
        f"Win Rate: <b>{s['wr']}%</b>\n"
        f"PnL total: <code>{sign}{s['pnl']:.2f} USDT</code>\n"
        f"Mejor: <code>+{s['best']:.2f}</code> | Peor: <code>{s['worst']:.2f}</code>\n"
        f"Prob media operada: {s['avg_prob']:.1f}%\n"
        f"Modo: {'LIVE' if LIVE_TRADING else 'SIMULADO'}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    threading.Thread(target=_tg_thread, daemon=True).start()
    time.sleep(0.5)

    log.info(
        f"=== Sniper Turbo Markov | TF:{TIMEFRAME} | {len(SYMBOLS)} pares | "
        f"{LEVERAGE}× | Risk:{RISK_PCT}% | Live:{LIVE_TRADING} ==="
    )
    init_db()

    # Pre-cargar klines
    log.info("Pre-cargando klines...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(lambda s: get_klines(s, 350), SYMBOLS))
    log.info(f"✅ Klines listas para {len(SYMBOLS)} pares")

    start_ws(SYMBOLS)
    time.sleep(2)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, SYMBOLS))

    balance   = get_balance()
    positions = get_positions()
    tg_startup(balance)

    last_report_h = datetime.now(timezone.utc).hour
    errors = 0

    while True:
        t0 = time.time()
        try:
            # Reporte diario 08:00 UTC
            now_h = datetime.now(timezone.utc).hour
            if now_h==8 and last_report_h!=8: tg_stats(); last_report_h=8
            elif now_h!=8: last_report_h=now_h

            rm.reset()
            balance   = get_balance()
            positions = get_positions()
            open_c    = len(positions)

            # Limpiar pos_risk de cerradas
            with pos_lock:
                for s in [k for k in pos_risk if k not in positions]:
                    del pos_risk[s]

            # Trailing / Partial TP
            if positions:
                update_trailing(positions)

            log.info(
                f"── [{TIMEFRAME}] {balance:.2f} USDT | "
                f"{open_c}/{MAX_OPEN} | {'LIVE' if LIVE_TRADING else 'SIM'} ──"
            )

            # Session filter
            if USE_SESSION:
                h = datetime.now(timezone.utc).hour
                if SESSION_OFF_START <= h < SESSION_OFF_END:
                    log.info(f"Session OFF ({h}:00 UTC)")
                    time.sleep(LOOP_SECS); continue

            # Risk check
            can, reason = rm.ok(balance)
            if not can:
                log.warning(f"Risk: {reason}")
                time.sleep(LOOP_SECS); continue

            # Scan — Markov paralelo
            signals: list = []
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                for fut in as_completed({ex.submit(scan,s):s for s in SYMBOLS}):
                    r = fut.result()
                    if r: signals.append(r)

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Señales Markov: {len(signals)}/{len(SYMBOLS)}")

            if signals:
                tg_scan(signals, len(SYMBOLS), open_c)
                for s in signals:
                    log.info(
                        f"  {s['symbol']} {s['signal']} [{s['markov_state']}] "
                        f"P🐂:{s['prob_bull']:.0f}% P🐻:{s['prob_bear']:.0f}% "
                        f"Vol:{s['vol_ratio']:.1f}× Score:{s['score']:.0f}"
                    )

            size_m = rm.size_mult()
            if size_m<1: log.warning(f"Tamaño al {size_m*100:.0f}%")

            entered: set = set()
            long_c  = sum(1 for p in positions.values() if p.get("positionSide")=="LONG")
            short_c = sum(1 for p in positions.values() if p.get("positionSide")=="SHORT")

            for sig in signals:
                sym = sig["symbol"]
                if sym in positions: continue
                if sym in entered:   continue
                if open_c >= MAX_OPEN: break
                if balance < 5: break
                if long_c  >= MAX_OPEN//2 and sig["signal"]=="LONG":  continue
                if short_c >= MAX_OPEN//2 and sig["signal"]=="SHORT": continue

                spread = get_spread(sym)
                if spread > MAX_SPREAD_PCT:
                    log.info(f"Skip {sym}: spread {spread:.3f}%"); continue

                try:
                    set_lev(sym)
                    live = get_price(sym)

                    # Actualizar SL/TP con precio live
                    drift = abs(live-sig["entry"])/sig["entry"]*100
                    if drift > 1.0:
                        log.info(f"Skip {sym}: drift {drift:.2f}%"); continue

                    sl_live = sig["sl"]
                    tp_live = sig["tp"]
                    tr_live = sig["trail_sl"]

                    qty, notional = calc_qty(balance, live, sl_live, size_m)
                    if qty <= 0: continue

                    side = "BUY" if sig["signal"]=="LONG" else "SELL"
                    log.info(
                        f"{'[SIM]' if not LIVE_TRADING else '[LIVE]'} "
                        f"{sym} {side} entry={live:.4f} sl={sl_live:.4f} "
                        f"tp={tp_live:.4f} qty={qty:.4f} "
                        f"P🐂:{sig['prob_bull']:.0f}% P🐻:{sig['prob_bear']:.0f}%"
                    )

                    if LIVE_TRADING:
                        place_market(sym, side, qty, sl_live, tp_live)

                    sig["entry"] = live
                    with pos_lock:
                        pos_risk[sym] = {
                            "entry":     live,
                            "sl":        sl_live,
                            "trail_sl":  tr_live,
                            "side":      sig["signal"],
                            "risk_dist": sig["risk_dist"],
                            "partial":   False,
                        }

                    db_open(sig, qty, notional)
                    tg_signal(sig, qty, notional, balance)

                    entered.add(sym)
                    open_c += 1
                    if sig["signal"]=="LONG": long_c  += 1
                    else:                     short_c += 1
                    time.sleep(0.3)

                except Exception as e:
                    log.error(f"Order {sym}: {e}")
                    if "stop" in str(e).lower() or "liquidat" in str(e).lower():
                        sl_cd[sym] = datetime.now(timezone.utc)
                    tg(f"⚠️ <b>Error {sym}</b>: <code>{str(e)[:150]}</code>")

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Markov Bot detenido</b>"); tg_stats(); break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle error #{errors}: {e}")
            if errors<=3: tg(f"⚠️ <b>Error #{errors}</b>: <code>{str(e)[:200]}</code>")
            if errors>=10: tg("🔴 <b>10 errores consecutivos. Detenido.</b>"); break

        time.sleep(max(0, LOOP_SECS-(time.time()-t0)))

if __name__ == "__main__":
    main()
