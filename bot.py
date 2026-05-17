"""
Sniper Turbo Markov — PRODUCTION FINAL
BingX Futures | Todas las monedas líquidas | 1H | Dinero real

FIXES aplicados:
  [FIX-1] Posiciones de otros bots no bloquean slots propios
  [FIX-2] WS reconexión estable con backoff exponencial
  [FIX-3] VWAP robusta con agrupación diaria correcta
  [FIX-4] Thresholds optimizados: density=1.2, slope=25, markov=30
  [FIX-5] SCAN_DEBUG muestra razones de rechazo
  [FIX-6] Trailing stop bug corregido (symbol vs positionSide)
  [FIX-7] Auto-descubrimiento de TODOS los pares USDT líquidos de BingX
  [FIX-8] requirements.txt limpio (sin python-binance)
  [FIX-9] Dockerfile fiable en vez de nixpacks inestable
  [FIX-10] Procfile + railway.toml apuntan a bot.py (no main.py)
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
# CONFIG — todos los valores por defecto son los optimizados
# ═══════════════════════════════════════════════════════════════════════════════
BINGX_API_KEY    = os.environ["BINGX_API_KEY"]
BINGX_SECRET_KEY = os.environ["BINGX_SECRET_KEY"]
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ── Pares ─────────────────────────────────────────────────────────────────────
# CUSTOM_SYMBOLS vacío = auto-descubrir todos los pares líquidos de BingX
_raw    = os.environ.get("CUSTOM_SYMBOLS", "")
SYMBOLS_OVERRIDE = [s.strip() for s in _raw.split(",") if s.strip()]
MAX_SYMBOLS = int(os.environ.get("MAX_SYMBOLS", "0"))   # 0 = todos los disponibles

# Pares excluidos (CFDs, materias primas, forex)
EXCLUDED_PREFIXES = ("NCS","NCF","NCMEX","NCOIL","NCGAS","NCXAU","NCXAG","NC")
EXCLUDED_KEYWORDS = ("Gasoline","GasOil","Brent","WTI","OilBrent","Copper",
                     "Wheat","Cotton","Soybean","Silver","EURUSD","GBPUSD","JPYUSD")

# ── Timeframe ─────────────────────────────────────────────────────────────────
TIMEFRAME    = os.environ.get("TIMEFRAME",    "1h")    # 1H por defecto (optimizado)
LOOP_SECS    = int(os.environ.get("LOOP_SECONDS", "60"))
SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "15"))

# ── Markov — parámetros optimizados por backtest ──────────────────────────────
MARKOV_LOOKBACK = int(os.environ.get("MARKOV_LOOKBACK",   "200"))
SLOPE_MIN       = float(os.environ.get("SLOPE_MIN",       "25.0"))   # optimizado
MARKOV_BULL_MIN = float(os.environ.get("MARKOV_BULL_MIN", "30.0"))   # optimizado
MARKOV_BEAR_MIN = float(os.environ.get("MARKOV_BEAR_MIN", "30.0"))   # optimizado

# ── Filtros ───────────────────────────────────────────────────────────────────
PIVOT_LEFT     = int(os.environ.get("PIVOT_LEFT",    "4"))
PIVOT_RIGHT    = int(os.environ.get("PIVOT_RIGHT",   "4"))
DENSITY_MULT   = float(os.environ.get("DENSITY_MULT","1.2"))          # optimizado
DENSITY_PERIOD = int(os.environ.get("DENSITY_PERIOD","50"))
MAX_SPREAD     = float(os.environ.get("MAX_SPREAD_PCT","0.15"))
SCAN_DEBUG     = os.environ.get("SCAN_DEBUG","true").lower() == "true"

# ── Salida ────────────────────────────────────────────────────────────────────
TRAIL_MULT   = float(os.environ.get("TRAIL_RISK_MULT","1.5"))
ATR14_LEN    = int(os.environ.get("ATR14_LEN",       "14"))
USE_TRAILING = os.environ.get("USE_TRAILING","true").lower() == "true"
USE_PARTIAL  = os.environ.get("USE_PARTIAL_TP","true").lower() == "true"
PARTIAL_R    = float(os.environ.get("PARTIAL_R",  "1.5"))
PARTIAL_PCT  = float(os.environ.get("PARTIAL_PCT", "50"))
BE_BPS       = float(os.environ.get("BE_MARGIN_BPS","10"))

# ── Risk ──────────────────────────────────────────────────────────────────────
RISK_PCT      = float(os.environ.get("RISK_PCT",        "1.5"))
LEVERAGE      = int(os.environ.get("LEVERAGE",          "3"))
MIN_USDT      = float(os.environ.get("MIN_ORDER_USDT",  "3.0"))
MAX_USDT      = float(os.environ.get("MAX_ORDER_USDT",  "50.0"))
MAX_MARGIN    = float(os.environ.get("MAX_MARGIN_PCT",  "20.0"))
MAX_OPEN      = int(os.environ.get("MAX_OPEN_TRADES",   "6"))
MAX_SAME_DIR  = int(os.environ.get("MAX_SAME_DIR",      "3"))
MAX_DAILY_LOSS= float(os.environ.get("MAX_DAILY_LOSS",  "5.0"))
MAX_CONSEC    = int(os.environ.get("MAX_CONSEC_LOSS",   "3"))
PAUSE_MINS    = int(os.environ.get("PAUSE_MINS",        "120"))
SIZE_REDUCE   = float(os.environ.get("SIZE_REDUCE_PCT", "50.0"))

# ── Sesión ────────────────────────────────────────────────────────────────────
USE_SESSION       = os.environ.get("USE_SESSION","true").lower() == "true"
SESSION_OFF_START = int(os.environ.get("SESSION_OFF_START","0"))
SESSION_OFF_END   = int(os.environ.get("SESSION_OFF_END",  "5"))
COOLDOWN_MINS     = int(os.environ.get("COOLDOWN_MINS",    "60"))

# ── Modo ──────────────────────────────────────────────────────────────────────
LIVE_TRADING = os.environ.get("LIVE_TRADING","false").lower() == "true"

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
ws_price : dict = {}
ws_kline : dict = {}
ws_lock          = threading.Lock()
sl_cd    : dict = {}
pos_risk : dict = {}
pos_lock         = threading.Lock()
_tg_loop         = None
_ws_connected    = threading.Event()

# ═══════════════════════════════════════════════════════════════════════════════
# SQLITE
# ═══════════════════════════════════════════════════════════════════════════════
DB_PATH = Path(os.environ.get("DB_PATH", "/data/trades.db"))

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, symbol TEXT, side TEXT,
            entry REAL, sl REAL, trail_sl REAL,
            qty REAL, notional REAL,
            slope REAL, prob_bull REAL, prob_bear REAL,
            vol_ratio REAL, vwap REAL, live INTEGER DEFAULT 1,
            closed_at TEXT, pnl_usdt REAL, exit_reason TEXT
        );
    """)
    con.commit(); con.close()
    log.info(f"DB: {DB_PATH}")

def db_open(sig: dict, qty: float, notional: float):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""INSERT INTO trades
            (ts,symbol,side,entry,sl,trail_sl,qty,notional,
             slope,prob_bull,prob_bear,vol_ratio,vwap,live)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            datetime.now(timezone.utc).isoformat(),
            sig["symbol"], sig["signal"],
            sig["entry"], sig["sl"], sig.get("trail_sl", sig["sl"]),
            qty, notional,
            sig.get("slope",0), sig.get("prob_bull",0), sig.get("prob_bear",0),
            sig.get("vol_ratio",0), sig.get("vwap",0),
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
        r = con.execute("""SELECT COUNT(*) total,
            SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) wins,
            COALESCE(SUM(pnl_usdt),0) pnl,
            COALESCE(MIN(pnl_usdt),0) worst,
            COALESCE(MAX(pnl_usdt),0) best
            FROM trades WHERE closed_at IS NOT NULL""").fetchone()
        con.close()
        t = r[0] or 0
        return {"total":t,"wins":r[1] or 0,
                "wr":round((r[1] or 0)/t*100,1) if t>0 else 0.0,
                "pnl":round(r[2],2),"worst":round(r[3],2),"best":round(r[4],2)}
    except Exception:
        return {"total":0,"wins":0,"wr":0.0,"pnl":0.0,"worst":0.0,"best":0.0}

# ═══════════════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════════════════════════════
class RiskManager:
    def __init__(self):
        self._l          = threading.Lock()
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
                if self.consec_loss >= MAX_CONSEC:
                    self.pause_until = datetime.now(timezone.utc) + timedelta(minutes=PAUSE_MINS)
                    tg(f"⏸ <b>PAUSA</b> — {self.consec_loss} pérdidas seguidas\n"
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
    p = dict(params or {})
    p["timestamp"] = int(time.time()*1000)
    p["signature"] = _sign(p)
    r = requests.get(BINGX_BASE+path, params=p, headers=_h(), timeout=15)
    r.raise_for_status(); return r.json()

def bx_post(path: str, payload: dict) -> dict:
    p = dict(payload)
    p["timestamp"] = int(time.time()*1000)
    p["signature"] = _sign(p)
    r = requests.post(BINGX_BASE+path, json=p,
                      headers={**_h(),"Content-Type":"application/json"}, timeout=15)
    r.raise_for_status(); return r.json()

def bx_del(path: str, params: dict) -> dict:
    p = dict(params)
    p["timestamp"] = int(time.time()*1000)
    p["signature"] = _sign(p)
    r = requests.delete(BINGX_BASE+path, params=p, headers=_h(), timeout=15)
    r.raise_for_status(); return r.json()

def get_balance() -> float:
    try:
        d   = bx_get("/openApi/swap/v2/user/balance").get("data", {})
        bal = d.get("balance", {}) if isinstance(d, dict) else {}
        src = bal if isinstance(bal, dict) else d
        for f in ("availableMargin","available","walletBalance","equity"):
            v = src.get(f)
            if v not in (None, "", "0"):
                val = float(v)
                if val > 0:
                    log.info(f"Balance: {val:.4f} USDT")
                    return val
        return 0.0
    except Exception as e:
        log.error(f"get_balance: {e}"); return 0.0

def get_positions() -> dict:
    """[FIX-1] Solo retorna posiciones en nuestros SYMBOLS — ignora las de otros bots."""
    try:
        data = bx_get("/openApi/swap/v2/user/positions", {})
        all_pos = {p["symbol"]: p for p in data.get("data",[])
                   if isinstance(p,dict) and float(p.get("positionAmt",0)) != 0}
        own_pos = {sym: pos for sym,pos in all_pos.items() if sym in _active_symbols}
        other   = [s for s in all_pos if s not in _active_symbols]
        if other:
            log.info(f"Ignoradas posiciones de otros bots: {other}")
        log.info(f"Positions ({len(own_pos)}): {list(own_pos.keys())}")
        return own_pos
    except Exception as e:
        log.error(f"get_positions: {e}"); return {}

def set_lev(symbol: str):
    for s in ("LONG","SHORT"):
        try:
            bx_post("/openApi/swap/v2/trade/leverage",
                    {"symbol":symbol,"side":s,"leverage":LEVERAGE})
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════════════════════
# [FIX-7] AUTO-DESCUBRIMIENTO DE TODOS LOS PARES USDT DE BINGX
# ═══════════════════════════════════════════════════════════════════════════════
def _is_valid_symbol(sym: str) -> bool:
    if not sym or not sym.endswith("-USDT"):
        return False
    base = sym.replace("-USDT","")
    if len(base) < 2:
        return False
    if any(base.startswith(p) for p in EXCLUDED_PREFIXES):
        return False
    if any(kw.lower() in sym.lower() for kw in EXCLUDED_KEYWORDS):
        return False
    return True

def discover_symbols(limit: int = 0) -> list:
    """
    Descubre todos los pares USDT perpetuos de BingX ordenados por volumen.
    limit=0 → todos los disponibles.
    """
    # Intento 1: contratos con volumen
    try:
        data  = bx_get("/openApi/swap/v2/quote/contracts", {})
        items = data.get("data", [])
        if isinstance(items, list) and items:
            usdt = [c for c in items
                    if isinstance(c,dict)
                    and c.get("asset","") == "USDT"
                    and c.get("status") == 1
                    and _is_valid_symbol(c.get("symbol",""))]
            usdt.sort(key=lambda x: float(x.get("tradeAmount",0) or 0), reverse=True)
            syms = [c["symbol"] for c in usdt]
            if syms:
                result = syms if limit == 0 else syms[:limit]
                log.info(f"✅ {len(result)} pares vía contracts")
                return result
    except Exception as e:
        log.warning(f"contracts: {e}")

    # Intento 2: ticker por volumen
    try:
        data  = bx_get("/openApi/swap/v2/quote/ticker", {})
        items = data.get("data", [])
        if isinstance(items, list) and items:
            usdt = [t for t in items
                    if isinstance(t,dict) and _is_valid_symbol(t.get("symbol",""))]
            usdt.sort(key=lambda x: float(x.get("quoteVolume",0) or 0), reverse=True)
            syms = [t["symbol"] for t in usdt]
            if syms:
                result = syms if limit == 0 else syms[:limit]
                log.info(f"✅ {len(result)} pares vía ticker")
                return result
    except Exception as e:
        log.warning(f"ticker: {e}")

    # Fallback mínimo
    fallback = [
        "BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT","XRP-USDT",
        "ADA-USDT","AVAX-USDT","DOGE-USDT","LINK-USDT","DOT-USDT",
        "NEAR-USDT","OP-USDT","ARB-USDT","SUI-USDT","APT-USDT",
        "PEPE-USDT","WIF-USDT","INJ-USDT","TIA-USDT","ATOM-USDT",
    ]
    log.warning(f"⚠️ Usando fallback ({len(fallback)} pares)")
    return fallback

_active_symbols: set = set()   # se rellena en main()

# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET — [FIX-2] backoff exponencial
# ═══════════════════════════════════════════════════════════════════════════════
def _ws_msg(_, message):
    try:
        import gzip
        try:
            d = json.loads(gzip.decompress(message) if isinstance(message,bytes) else message)
        except Exception:
            d = json.loads(message)
        if not d.get("dataType","").endswith("@kline"):
            return
        sym   = d.get("s","").replace("_","-")
        kdata = d.get("data",{}).get("kline", d.get("k",{}))
        if not kdata or float(kdata.get("c",0)) == 0:
            return
        row = {
            "open_time": pd.to_datetime(kdata.get("t",0), unit="ms"),
            "open":      float(kdata.get("o",0)),
            "high":      float(kdata.get("h",0)),
            "low":       float(kdata.get("l",0)),
            "close":     float(kdata.get("c",0)),
            "volume":    float(kdata.get("v",0)),
        }
        with ws_lock:
            df = ws_kline.get(sym)
            if df is not None:
                if len(df)>0 and df.iloc[-1]["open_time"]==row["open_time"]:
                    for col in ("open","high","low","close","volume"):
                        df.at[df.index[-1], col] = row[col]
                else:
                    ws_kline[sym] = pd.concat(
                        [df, pd.DataFrame([row])], ignore_index=True).tail(400)
            ws_price[sym] = row["close"]
    except Exception:
        pass

def start_ws(symbols: list):
    ivl = INTERVAL_MAP.get(TIMEFRAME,"1H").lower()
    # Limitar WS a 200 pares máximo (límite BingX)
    ws_syms = symbols[:200]

    def _run():
        backoff = 5
        while True:
            try:
                _ws_connected.clear()
                def on_open(app):
                    _ws_connected.set()
                    log.info(f"WS conectado — {len(ws_syms)} pares")
                    for s in ws_syms:
                        try:
                            app.send(json.dumps({
                                "id":       f"sub_{s}",
                                "reqType":  "sub",
                                "dataType": f"{s.replace('-','_')}@kline_{ivl}"
                            }))
                        except Exception:
                            pass
                ws = websocket.WebSocketApp(
                    BINGX_WS,
                    on_message=_ws_msg,
                    on_error=lambda a,e: log.warning(f"WS error: {e}"),
                    on_close=lambda a,*x: (_ws_connected.clear(),
                                           log.info("WS cerrado"))[1],
                    on_open=on_open,
                )
                ws.run_forever(ping_interval=60, ping_timeout=20)
                backoff = 5
            except Exception as e:
                log.warning(f"WS thread: {e}")
            log.info(f"WS reconectando en {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    threading.Thread(target=_run, daemon=True).start()
    log.info(f"WS iniciado — {len(ws_syms)} pares | {TIMEFRAME}")

# ═══════════════════════════════════════════════════════════════════════════════
# PRECIO Y KLINES
# ═══════════════════════════════════════════════════════════════════════════════
def get_price(symbol: str) -> float:
    with ws_lock:
        p = ws_price.get(symbol)
    if p and p > 0:
        return p
    try:
        items = bx_get("/openApi/swap/v2/quote/ticker",{"symbol":symbol}).get("data",[])
        items = [items] if isinstance(items,dict) else items
        for item in items:
            if item.get("symbol")==symbol:
                v = item.get("lastPrice") or item.get("price")
                if v: return float(v)
    except Exception:
        pass
    raise ValueError(f"Sin precio: {symbol}")

def get_spread(symbol: str) -> float:
    try:
        d = bx_get("/openApi/swap/v2/quote/bookTicker",{"symbol":symbol}).get("data",{})
        if isinstance(d,list): d = next((x for x in d if x.get("symbol")==symbol),{})
        a = float(d.get("askPrice",0) or 0)
        b = float(d.get("bidPrice",0) or 0)
        return (a-b)/b*100 if a>0 and b>0 else 999.0
    except Exception:
        return 999.0

def _fetch(symbol: str, tf: str, limit: int) -> pd.DataFrame:
    p = {"symbol":symbol,"interval":INTERVAL_MAP.get(tf,tf),"limit":limit}
    rows = bx_get("/openApi/swap/v3/quote/klines",p).get("data",[])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows,
                      columns=["open_time","open","high","low","close","volume","close_time"])
    for c in ("open","high","low","close","volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.dropna(subset=["open","high","low","close","volume"], inplace=True)
    return df.sort_values("open_time").reset_index(drop=True)

def get_klines(symbol: str, limit: int = 300) -> pd.DataFrame:
    with ws_lock:
        df = ws_kline.get(symbol)
    if df is not None and len(df) >= limit//2:
        return df.copy()
    df = _fetch(symbol, TIMEFRAME, limit)
    if not df.empty:
        with ws_lock:
            ws_kline[symbol] = df.copy()
    return df

# ═══════════════════════════════════════════════════════════════════════════════
# INDICADORES
# ═══════════════════════════════════════════════════════════════════════════════
def calc_atr(h, l, c, p):
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False).mean()

def calc_magic_slope(close, atr7):
    ema7  = close.ewm(span=7, adjust=False).mean()
    return ((ema7-ema7.shift(1))/atr7.replace(0,np.nan)*100).fillna(0)

def calc_markov(slope):
    n      = len(slope)
    states = np.where(slope>SLOPE_MIN,0,np.where(slope<-SLOPE_MIN,1,2)).astype(int)
    pb     = np.zeros(n); pe=np.zeros(n); m=np.zeros((3,3))
    for i in range(1,n):
        m[states[i-1]][states[i]] += 1.0
        if i > MARKOV_LOOKBACK:
            op,oc = states[i-MARKOV_LOOKBACK-1], states[i-MARKOV_LOOKBACK]
            m[op][oc] = max(0.0, m[op][oc]-1.0)
        if i >= 50:
            rs = m[states[i]].sum()
            if rs>0: pb[i]=m[states[i]][0]/rs*100; pe[i]=m[states[i]][1]/rs*100
    return pd.DataFrame({"state":states,"prob_bull":pb,"prob_bear":pe},index=slope.index)

def calc_vwap(df):
    """[FIX-3] VWAP robusta con agrupación diaria correcta."""
    tp  = (df["high"]+df["low"]+df["close"])/3
    pv  = tp*df["volume"]
    try:
        d      = df["open_time"].dt.date
        cum_pv = pv.groupby(d).cumsum()
        cum_v  = df["volume"].groupby(d).cumsum()
    except Exception:
        cum_pv = pv.cumsum()
        cum_v  = df["volume"].cumsum()
    return (cum_pv/cum_v.replace(0,np.nan)).rename("vwap")

def calc_pivots(high, low, left=4, right=4):
    n  = len(high)
    ph = pd.Series(np.nan, index=high.index)
    pl = pd.Series(np.nan, index=low.index)
    for i in range(left, n-right):
        if float(high.iloc[i]) == float(high.iloc[i-left:i+right+1].max()):
            ph.iloc[i] = float(high.iloc[i])
        if float(low.iloc[i]) == float(low.iloc[i-left:i+right+1].min()):
            pl.iloc[i] = float(low.iloc[i])
    return ph.ffill(), pl.ffill()

# ═══════════════════════════════════════════════════════════════════════════════
# ESCÁNER — con debug de rechazos [FIX-5]
# ═══════════════════════════════════════════════════════════════════════════════
def scan(symbol: str) -> tuple:
    """Retorna (señal|None, motivo_rechazo)."""
    cd = sl_cd.get(symbol)
    if cd and (datetime.now(timezone.utc)-cd).total_seconds()/60 < COOLDOWN_MINS:
        return None, "cooldown"
    try:
        df = get_klines(symbol, limit=350)
        min_bars = MARKOV_LOOKBACK+PIVOT_LEFT+PIVOT_RIGHT+20
        if df.empty or len(df) < min_bars:
            return None, f"pocas_barras({len(df)})"

        atr7_s  = calc_atr(df["high"],df["low"],df["close"],7)
        atr14_s = calc_atr(df["high"],df["low"],df["close"],ATR14_LEN)
        slope_s = calc_magic_slope(df["close"],atr7_s)
        markov  = calc_markov(slope_s)
        vwap_s  = calc_vwap(df)
        vol_ma  = df["volume"].rolling(DENSITY_PERIOD).mean()
        ph, pl  = calc_pivots(df["high"],df["low"],PIVOT_LEFT,PIVOT_RIGHT)

        i = len(df)-2
        if i < MARKOV_LOOKBACK+PIVOT_LEFT+PIVOT_RIGHT:
            return None, "i_insuficiente"

        c  = float(df["close"].iloc[i])
        h  = float(df["high"].iloc[i])
        l  = float(df["low"].iloc[i])
        at = float(atr14_s.iloc[i])
        sl = float(slope_s.iloc[i])
        vw = float(vwap_s.iloc[i])
        vm = float(vol_ma.iloc[i])
        pk = float(ph.iloc[i])
        vl = float(pl.iloc[i])
        pb = float(markov["prob_bull"].iloc[i])
        pe = float(markov["prob_bear"].iloc[i])
        p1 = float(df["low"].iloc[i-1])
        p2 = float(df["high"].iloc[i-1])

        if any(np.isnan(x) for x in [sl,vw,at,pk,vl,pb,pe]):
            return None, "nan"
        if vm <= 0 or at <= 0:
            return None, "vol_cero"

        vr = round(df["volume"].iloc[i]/vm, 2)
        if vr < DENSITY_MULT:
            return None, f"density({vr:.1f}<{DENSITY_MULT})"
        if abs(sl) <= SLOPE_MIN:
            return None, f"slope({sl:.1f})"

        lc = l<vl and c<vw and sl>SLOPE_MIN
        sc = h>pk  and c>vw and sl<-SLOPE_MIN
        if not lc and not sc:
            return None, f"sin_setup(slope={sl:.1f},vwap={'ok' if c<vw else 'no'},piv={'ok' if l<vl else 'no'})"

        direction = "LONG" if lc else "SHORT"
        if direction=="LONG"  and pb<MARKOV_BULL_MIN:
            return None, f"markov_bull({pb:.0f}%<{MARKOV_BULL_MIN}%)"
        if direction=="SHORT" and pe<MARKOV_BEAR_MIN:
            return None, f"markov_bear({pe:.0f}%<{MARKOV_BEAR_MIN}%)"

        state = {0:"BULL",1:"BEAR",2:"NEUTRAL"}.get(int(markov["state"].iloc[i]),"?")
        rd    = max(abs(c-(p1 if direction=="LONG" else p2)), at*0.2)
        sl_p  = (p1-at*0.1) if direction=="LONG" else (p2+at*0.1)
        tp    = c+rd*TRAIL_MULT*2 if direction=="LONG" else c-rd*TRAIL_MULT*2
        trail = c-rd*TRAIL_MULT   if direction=="LONG" else c+rd*TRAIL_MULT

        score  = min(pb if direction=="LONG" else pe, 40)
        score += min(vr*10, 30)
        score += min(abs(sl)/5, 20)
        score += 10

        return {
            "symbol":      symbol,
            "signal":      direction,
            "entry":       round(c,6),
            "sl":          round(sl_p,6),
            "trail_sl":    round(trail,6),
            "tp":          round(tp,6),
            "risk_dist":   round(rd,6),
            "atr14":       round(at,6),
            "slope":       round(sl,2),
            "prob_bull":   round(pb,1),
            "prob_bear":   round(pe,1),
            "markov_state":state,
            "vol_ratio":   vr,
            "vwap":        round(vw,6),
            "score":       round(score,1),
        }, "OK"

    except Exception as e:
        log.debug(f"scan {symbol}: {e}")
        return None, f"error({str(e)[:50]})"

# ═══════════════════════════════════════════════════════════════════════════════
# TRAILING / PARTIAL TP — [FIX-6] bug del símbolo corregido
# ═══════════════════════════════════════════════════════════════════════════════
def _stop_order(symbol: str, close_side: str, pos_side: str, stop_price: float):
    """[FIX-6] Usa 'symbol' correctamente (v1 usaba pos_side aquí)."""
    try:
        bx_post("/openApi/swap/v2/trade/order",{
            "symbol":        symbol,
            "type":          "STOP_MARKET",
            "side":          close_side,
            "positionSide":  pos_side,
            "stopPrice":     round(stop_price,6),
            "closePosition": "true",
            "workingType":   "MARK_PRICE",
        })
    except Exception as e:
        log.warning(f"stop_order {symbol}: {e}")

def update_positions(positions: dict):
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

            rd         = r.get("risk_dist",0) or 0
            curr_trail = r.get("trail_sl",r["sl"])
            be_margin  = entry*(BE_BPS/10000)
            be_price   = entry+be_margin if ps=="LONG" else entry-be_margin
            pt_trigger = entry+rd*PARTIAL_R if ps=="LONG" else entry-rd*PARTIAL_R

            # Partial TP
            pt_hit = (live>=pt_trigger) if ps=="LONG" else (live<=pt_trigger)
            if USE_PARTIAL and not r.get("partial") and pt_hit:
                cq = round(qty*PARTIAL_PCT/100, 4)
                cs = "SELL" if ps=="LONG" else "BUY"
                if cq >= 0.001 and LIVE_TRADING:
                    try:
                        bx_post("/openApi/swap/v2/trade/order",{
                            "symbol":sym,"side":cs,"positionSide":ps,
                            "type":"MARKET","quantity":round(cq,4),"reduceOnly":"true"})
                        _stop_order(sym, cs, ps, be_price)
                        pnl = abs(live-entry)*cq
                        rm.record(+pnl); db_close(sym,pnl,"partial_tp")
                        with pos_lock:
                            if sym in pos_risk:
                                pos_risk[sym]["partial"]  = True
                                pos_risk[sym]["trail_sl"] = be_price
                        tg(f"💰 <b>PARTIAL TP {sym}</b> {PARTIAL_PCT:.0f}% @ "
                           f"<code>{live:.6g}</code> ({PARTIAL_R}R)\n"
                           f"SL→BE <code>{be_price:.6g}</code>")
                    except Exception as e:
                        log.warning(f"partial {sym}: {e}")
                continue

            # Trailing stop
            if USE_TRAILING and rd > 0:
                mv  = (live-entry) if ps=="LONG" else (entry-live)
                nt  = (entry+mv-rd*TRAIL_MULT) if ps=="LONG" else (entry-mv+rd*TRAIL_MULT)
                ok  = (ps=="LONG" and nt>curr_trail) or (ps=="SHORT" and nt<curr_trail)
                if ok and LIVE_TRADING:
                    cs = "SELL" if ps=="LONG" else "BUY"
                    _stop_order(sym, cs, ps, nt)
                    with pos_lock:
                        if sym in pos_risk: pos_risk[sym]["trail_sl"] = nt
                    log.info(f"Trail {sym}: {curr_trail:.4f}→{nt:.4f}")
        except Exception as e:
            log.debug(f"update_positions {sym}: {e}")

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
    if res.get("code",-1) != 0:
        raise ValueError(f"{symbol}: code={res.get('code')} {res.get('msg')}")
    return res

def calc_qty(bal:float,entry:float,sl:float,mult:float=1.0) -> tuple:
    dist = abs(entry-sl)/entry if entry>0 else 0
    if dist < 1e-6: return 0, 0
    risk     = bal*(RISK_PCT/100)*mult
    notional = risk/dist
    max_n    = min(MAX_USDT, bal*(MAX_MARGIN/100)*LEVERAGE)
    notional = max(MIN_USDT, min(notional, max_n))
    if notional > bal*LEVERAGE: return 0, 0
    qty = notional/entry if entry>0 else 0
    return round(max(qty,0.001),4), round(notional,2)

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — loop dedicado con retry
# ═══════════════════════════════════════════════════════════════════════════════
def _tg_thread():
    global _tg_loop
    _tg_loop = asyncio.new_event_loop()
    _tg_loop.run_forever()

def tg(msg: str):
    if not TELEGRAM_OK or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    global _tg_loop
    if not _tg_loop or not _tg_loop.is_running(): return
    async def _send(retries=3):
        for attempt in range(retries):
            try:
                bot = Bot(TELEGRAM_TOKEN)
                cid = (int(TELEGRAM_CHAT_ID)
                       if TELEGRAM_CHAT_ID.lstrip("-").isdigit()
                       else TELEGRAM_CHAT_ID)
                await bot.send_message(chat_id=cid, text=msg,
                                       parse_mode=ParseMode.HTML)
                return
            except Exception as e:
                log.warning(f"Telegram intento {attempt+1}: {e}")
                if attempt < retries-1:
                    await asyncio.sleep(2**attempt)
    asyncio.run_coroutine_threadsafe(_send(), _tg_loop)

def tg_startup(bal: float, symbols: list):
    s    = db_stats()
    mode = "🔴 LIVE" if LIVE_TRADING else "🟡 SIMULADO"
    tg(
        f"🧠 <b>Sniper Turbo Markov — {mode}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>TF:</b> {TIMEFRAME} | <b>Pares:</b> {len(symbols)}\n"
        f"<b>Markov:</b> lookback={MARKOV_LOOKBACK} | "
        f"P≥{MARKOV_BULL_MIN}% | slope±{SLOPE_MIN}\n"
        f"<b>Density:</b> >{DENSITY_MULT}×avg({DENSITY_PERIOD})\n"
        f"<b>Trail:</b> {TRAIL_MULT}× | "
        f"<b>Partial:</b> {PARTIAL_PCT:.0f}%@{PARTIAL_R}R\n"
        f"<b>Risk:</b> {RISK_PCT}%/trade | {LEVERAGE}× | max {MAX_OPEN}\n"
        f"<b>Balance:</b> {bal:.2f} USDT\n"
        f"<b>Histórico:</b> {s['total']} trades | WR {s['wr']}% | "
        f"PnL {s['pnl']:+.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )

def tg_signal(sig:dict, qty:float, notional:float, bal:float):
    d    = "🟢 LONG" if sig["signal"]=="LONG" else "🔴 SHORT"
    mode = "🟡[SIM] " if not LIVE_TRADING else ""
    tg(
        f"{mode}<b>MARKOV ENTRY — {sig['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{d} | [{sig['markov_state']}] Score:{sig['score']:.0f}\n"
        f"🧠 P🐂{sig['prob_bull']:.0f}% P🐻{sig['prob_bear']:.0f}% "
        f"Slope:{sig['slope']:+.1f} Vol:{sig['vol_ratio']:.1f}×\n"
        f"Entrada: <code>{sig['entry']:.6g}</code>\n"
        f"SL: <code>{sig['sl']:.6g}</code> | "
        f"Trail: <code>{sig['trail_sl']:.6g}</code>\n"
        f"TP: <code>{sig['tp']:.6g}</code>\n"
        f"Qty:{qty:.4f} | {notional:.2f} USDT | "
        f"Riesgo:{bal*RISK_PCT/100:.2f} USDT\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

def tg_scan(signals, total, open_c, rejects):
    if not signals: return
    lines = [
        f"🔍 <b>{len(signals)}/{total}</b> señales | {open_c}/{MAX_OPEN} trades",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in signals[:8]:
        e = "🟢" if s["signal"]=="LONG" else "🔴"
        lines.append(f"{e} <b>{s['symbol']}</b> [{s['markov_state']}] "
                     f"P🐂{s['prob_bull']:.0f}% Vol{s['vol_ratio']:.1f}× "
                     f"Score{s['score']:.0f}")
    if SCAN_DEBUG and rejects:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🔎 <b>Rechazos:</b>")
        for sym,reason in list(rejects.items())[:5]:
            lines.append(f"  • {sym}: {reason}")
    tg("\n".join(lines))

def tg_stats():
    s    = db_stats()
    sign = "+" if s["pnl"]>=0 else ""
    tg(f"📊 <b>Estadísticas</b>\n"
       f"Trades:{s['total']} | Wins:{s['wins']} | WR:<b>{s['wr']}%</b>\n"
       f"PnL:<code>{sign}{s['pnl']:.2f} USDT</code>\n"
       f"Mejor:+{s['best']:.2f} | Peor:{s['worst']:.2f}\n"
       f"Modo:{'LIVE' if LIVE_TRADING else 'SIMULADO'}")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global _active_symbols

    threading.Thread(target=_tg_thread, daemon=True).start()
    time.sleep(0.5)

    log.info(f"=== Sniper Turbo Markov | TF:{TIMEFRAME} | "
             f"{LEVERAGE}× | Risk:{RISK_PCT}% | Live:{LIVE_TRADING} ===")
    init_db()

    # Descubrir pares
    if SYMBOLS_OVERRIDE:
        symbols = SYMBOLS_OVERRIDE
        log.info(f"Usando pares manuales: {symbols}")
    else:
        symbols = discover_symbols(MAX_SYMBOLS)
        log.info(f"Auto-descubiertos {len(symbols)} pares de BingX")

    _active_symbols = set(symbols)

    # Pre-cargar klines en paralelo
    log.info("Pre-cargando klines...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(lambda s: get_klines(s,300), symbols))
    log.info("✅ Klines listas")

    # WebSocket en background
    start_ws(symbols)
    log.info("Esperando WS...")
    _ws_connected.wait(timeout=20)

    # Configurar leverage
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        list(ex.map(set_lev, symbols))

    balance = get_balance()

    # Advertencia si balance bajo
    min_bal = MIN_USDT / LEVERAGE
    if balance < min_bal * 3:
        msg = f"⚠️ Balance {balance:.2f} USDT muy bajo. Mínimo recomendado: ${min_bal*3:.0f}"
        log.warning(msg); tg(msg)

    tg_startup(balance, symbols)

    last_report_h = datetime.now(timezone.utc).hour
    errors = 0

    while True:
        t0 = time.time()
        try:
            # Reporte diario 08:00 UTC
            now_h = datetime.now(timezone.utc).hour
            if now_h==8 and last_report_h!=8:
                tg_stats(); last_report_h=8
            elif now_h!=8:
                last_report_h=now_h

            rm.reset()
            balance   = get_balance()
            positions = get_positions()
            open_c    = len(positions)

            # Limpiar pos_risk de posiciones cerradas
            with pos_lock:
                for s in [k for k in pos_risk if k not in positions]:
                    del pos_risk[s]

            if positions:
                update_positions(positions)

            log.info(f"── [{TIMEFRAME}] {balance:.2f} USDT | "
                     f"{open_c}/{MAX_OPEN} | {'LIVE' if LIVE_TRADING else 'SIM'} | "
                     f"{len(symbols)} pares ──")

            # Session filter
            if USE_SESSION and SESSION_OFF_START <= now_h < SESSION_OFF_END:
                log.info(f"Session OFF ({now_h}:00 UTC)")
                time.sleep(LOOP_SECS); continue

            can, reason = rm.ok(balance)
            if not can:
                log.warning(f"Risk: {reason}")
                time.sleep(LOOP_SECS); continue

            if balance < MIN_USDT/LEVERAGE:
                log.warning(f"Balance insuficiente: {balance:.2f}")
                time.sleep(LOOP_SECS); continue

            # Scan paralelo de todos los pares
            signals   : list = []
            rejects   : dict = {}
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(scan, s): s for s in symbols}
                for fut in as_completed(futs):
                    sig, reason = fut.result()
                    sym = futs[fut]
                    if sig:
                        signals.append(sig)
                    else:
                        rejects[sym] = reason

            signals.sort(key=lambda x: x["score"], reverse=True)
            log.info(f"Señales Markov: {len(signals)}/{len(symbols)}")

            if SCAN_DEBUG and not signals:
                # Mostrar top rechazos para debug
                common = {}
                for r in rejects.values():
                    k = r.split("(")[0]
                    common[k] = common.get(k,0)+1
                top = sorted(common.items(),key=lambda x:-x[1])[:5]
                log.info(f"Top rechazos: {top}")

            if signals:
                tg_scan(signals, len(symbols), open_c, rejects)
                for s in signals[:5]:
                    log.info(f"  ✓ {s['symbol']} {s['signal']} [{s['markov_state']}] "
                             f"P🐂{s['prob_bull']:.0f}% Vol{s['vol_ratio']:.1f}× "
                             f"Score{s['score']:.0f}")

            size_m = rm.size_mult()
            if size_m < 1:
                log.warning(f"Tamaño al {size_m*100:.0f}% (racha negativa)")

            long_c  = sum(1 for p in positions.values() if p.get("positionSide")=="LONG")
            short_c = sum(1 for p in positions.values() if p.get("positionSide")=="SHORT")
            entered : set = set()

            for sig in signals:
                sym = sig["symbol"]
                if sym in positions: continue
                if sym in entered:   continue
                if open_c >= MAX_OPEN: break
                if balance < 5: break

                if sig["signal"]=="LONG"  and long_c  >= MAX_SAME_DIR: continue
                if sig["signal"]=="SHORT" and short_c >= MAX_SAME_DIR: continue

                spread = get_spread(sym)
                if spread > MAX_SPREAD:
                    log.info(f"Skip {sym}: spread {spread:.3f}%"); continue

                try:
                    set_lev(sym)
                    live = get_price(sym)

                    drift = abs(live-sig["entry"])/sig["entry"]*100
                    if drift > 1.0:
                        log.info(f"Skip {sym}: drift {drift:.2f}%"); continue

                    qty, notional = calc_qty(balance, live, sig["sl"], size_m)
                    if qty <= 0:
                        log.info(f"Skip {sym}: qty=0 (bal={balance:.2f})"); continue

                    side = "BUY" if sig["signal"]=="LONG" else "SELL"
                    log.info(f"{'[LIVE]' if LIVE_TRADING else '[SIM]'} "
                             f"{sym} {side} entry={live:.4f} sl={sig['sl']:.4f} "
                             f"qty={qty:.4f} score={sig['score']:.0f}")

                    if LIVE_TRADING:
                        place_market(sym, side, qty, sig["sl"], sig["tp"])

                    sig["entry"] = live
                    risk_data = {
                        "entry":     live,
                        "sl":        sig["sl"],
                        "trail_sl":  sig["trail_sl"],
                        "side":      sig["signal"],
                        "risk_dist": sig["risk_dist"],
                        "partial":   False,
                    }
                    with pos_lock:
                        pos_risk[sym] = risk_data

                    db_open(sig, qty, notional)
                    tg_signal(sig, qty, notional, balance)

                    entered.add(sym)
                    open_c += 1
                    if sig["signal"]=="LONG": long_c  += 1
                    else:                     short_c += 1
                    time.sleep(0.3)

                except Exception as e:
                    log.error(f"Order {sym}: {e}")
                    if any(w in str(e).lower() for w in ("stop","liquidat")):
                        sl_cd[sym] = datetime.now(timezone.utc)
                    tg(f"⚠️ <b>Error {sym}</b>: <code>{str(e)[:150]}</code>")

            errors = 0

        except KeyboardInterrupt:
            tg("🛑 <b>Bot detenido</b>"); tg_stats(); break
        except Exception as e:
            errors += 1
            log.exception(f"Cycle #{errors}: {e}")
            if errors<=3: tg(f"⚠️ <b>Error #{errors}</b>: <code>{str(e)[:200]}</code>")
            if errors>=10: tg("🔴 <b>10 errores. Detenido.</b>"); break

        time.sleep(max(0, LOOP_SECS-(time.time()-t0)))

if __name__ == "__main__":
    main()
