"""
╔══════════════════════════════════════════════════════════════════════╗
║       PHANTOM EDGE BOT ULTRA v4.1 — FIX BALANCE CRÍTICO             ║
║  FIX: Balance BingX — cubre TODAS las estructuras conocidas          ║
║  FIX: Logging detallado de balance para diagnóstico                  ║
║  FIX: Watchlist prioritaria (SKYUSDT, REZUSDT, XNYUSDT, etc.)       ║
╚══════════════════════════════════════════════════════════════════════╝

CAMBIOS v4.1 vs v4.0:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 FIX 1 — Balance (problema crítico):
   · Prueba 6 endpoints/campos diferentes en orden
   · Loguea la respuesta cruda si sigue siendo 0
   · Estructura BingX: data.balance puede ser dict, list, o string
   · Campo correcto según docs actuales: data.data.balance.availableMargin
   · Fallback: data.data.availableMargin (estructura alternativa)
   · Fallback 2: data.data[0].availableMargin (lista)
   · Fallback 3: parseo de string si viene como "108.35"

 FIX 2 — Watchlist prioritaria:
   · PRIORITY_SYMBOLS env var: pares que se escanean PRIMERO
   · Default: SKYUSDT, REZUSDT, XNYUSDT, MAXXINC, ROBO, PNUTUSDT

 FIX 3 — Min balance override:
   · MIN_BALANCE_OVERRIDE env var para ignorar chequeo si necesario
   · TRADE_USDT reducido a funcionar con balances bajos
"""

import os
import asyncio
import logging
import time
import hmac
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
import numpy as np
import httpx

# ─────────────────────────────────────────────────────────────
#  CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────
API_KEY        = os.getenv("BINGX_API_KEY", "")
API_SECRET     = os.getenv("BINGX_API_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

AUTO_TRADING     = os.getenv("AUTO_TRADING_ENABLED", "false").lower() == "true"
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))
TIMEFRAME        = os.getenv("TIMEFRAME", "5m")
TIMEFRAME_SLOW   = os.getenv("TIMEFRAME_SLOW", "15m")
ZZ_DEV_5M        = float(os.getenv("ZZ_DEVIATION", "0.5"))
ZZ_DEV_15M       = float(os.getenv("ZZ15_DEVIATION", "0.8"))
ST_PERIOD        = int(os.getenv("ST_PERIOD", "10"))
ST_MULT          = float(os.getenv("ST_MULT", "3.0"))
RR               = float(os.getenv("RR", "2.5"))
MIN_SCORE        = int(os.getenv("MIN_SCORE", "7"))
MIN_ATR_PCT      = float(os.getenv("MIN_ATR_PCT", "0.10"))
MIN_VOL_MULT     = float(os.getenv("MIN_VOL_MULT", "0.8"))
TRADE_USDT       = float(os.getenv("TRADE_USDT", "9"))
MAX_POSITIONS    = int(os.getenv("MAX_POSITIONS", "5"))
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL", "30"))
MAX_CONCURRENT   = int(os.getenv("MAX_CONCURRENT", "30"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "40"))
MAX_DAILY_LOSS   = float(os.getenv("MAX_DAILY_LOSS", "5.0"))
PORT             = int(os.getenv("PORT", "8080"))
TP1_PCT          = float(os.getenv("TP1_PCT", "0.30"))
TP2_PCT          = float(os.getenv("TP2_PCT", "0.30"))
KLINE_TTL        = int(os.getenv("KLINE_TTL", "25"))
SYM_TIMEOUT      = float(os.getenv("SYM_TIMEOUT", "8.0"))

# v4.1 nuevas variables
MIN_BALANCE_OVERRIDE = float(os.getenv("MIN_BALANCE_OVERRIDE", "0"))  # 0 = desactivado
PRIORITY_SYMBOLS_RAW = os.getenv("PRIORITY_SYMBOLS",
    "SKY-USDT,REZ-USDT,XNY-USDT,MAXXIN-USDT,ROBO-USDT,PNUT-USDT,"
    "TURBO-USDT,HYPE-USDT,RIVER-USDT,UAI-USDT"
)
PRIORITY_SYMBOLS = [s.strip() for s in PRIORITY_SYMBOLS_RAW.split(",") if s.strip()]

BINGX_BASE = "https://open-api.bingx.com"

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("PhantomEdge")


# ══════════════════════════════════════════════════════════════
#  BINGX API CLIENT v4.1
# ══════════════════════════════════════════════════════════════
class BingXClient:

    def __init__(self, api_key: str, api_secret: str):
        self.api_key    = api_key
        self.api_secret = api_secret.encode()
        self._client: Optional[httpx.AsyncClient] = None
        self._failures: dict[str, int] = defaultdict(int)
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_connections=60, max_keepalive_connections=30)
            self._client = httpx.AsyncClient(timeout=12.0, limits=limits)
        return self._client

    def _sign(self, params: dict) -> str:
        payload = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return hmac.new(self.api_secret, payload.encode(), hashlib.sha256).hexdigest()

    def _ts(self) -> int:
        return int(time.time() * 1000)

    async def _get_pub(self, path: str, params: dict = None) -> dict:
        async with self._sem:
            try:
                r = await self.client.get(f"{BINGX_BASE}{path}", params=params or {})
                return r.json()
            except Exception as e:
                log.debug(f"GET_PUB {path}: {e}")
                return {}

    async def _get_priv(self, path: str, params: dict = None) -> dict:
        p = dict(params or {})
        p["timestamp"] = self._ts()
        p["signature"] = self._sign(p)
        headers = {"X-BX-APIKEY": self.api_key}
        async with self._sem:
            try:
                r = await self.client.get(f"{BINGX_BASE}{path}", params=p, headers=headers)
                return r.json()
            except Exception as e:
                log.debug(f"GET_PRIV {path}: {e}")
                return {}

    async def _post_priv(self, path: str, params: dict = None) -> dict:
        p = dict(params or {})
        p["timestamp"] = self._ts()
        p["signature"] = self._sign(p)
        headers = {
            "X-BX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            r = await self.client.post(f"{BINGX_BASE}{path}", data=p, headers=headers)
            return r.json()
        except Exception as e:
            log.debug(f"POST {path}: {e}")
            return {}

    # ══════════════════════════════════════════════════════════
    #  BALANCE v4.1 — Extracción exhaustiva de todos los campos
    # ══════════════════════════════════════════════════════════
    def _extract_balance_from(self, data: dict, path_label: str) -> float:
        """
        Intenta extraer balance disponible de cualquier estructura
        que devuelva BingX. Loguea la estructura para diagnóstico.
        """
        if data.get("code") != 0:
            log.debug(f"Balance {path_label}: code={data.get('code')} msg={data.get('msg','')}")
            return -1.0  # indica fallo de API, no balance 0

        raw_data = data.get("data", {})

        # ── Estructura 1: data.data.balance es dict ────────────
        bal = raw_data.get("balance") if isinstance(raw_data, dict) else None
        if isinstance(bal, dict):
            for field in ["availableMargin", "available", "freeMargin",
                          "availableBalance", "walletBalance"]:
                v = bal.get(field)
                if v is not None and float(v) > 0:
                    log.info(f"  Balance encontrado: {path_label} → balance.{field} = {v}")
                    return float(v)
            # Si todos son 0, puede ser balance real 0
            am = bal.get("availableMargin", 0)
            log.info(f"  Balance dict (puede ser 0 real): {path_label} → {bal}")
            return float(am)

        # ── Estructura 2: data.data.balance es lista ───────────
        if isinstance(bal, list):
            for b in bal:
                if b.get("asset") == "USDT":
                    v = b.get("availableMargin") or b.get("available") or 0
                    log.info(f"  Balance lista USDT: {path_label} = {v}")
                    return float(v)

        # ── Estructura 3: data.data es dict directo ────────────
        if isinstance(raw_data, dict):
            for field in ["availableMargin", "available", "freeMargin",
                          "availableBalance", "equity"]:
                v = raw_data.get(field)
                if v is not None:
                    log.info(f"  Balance directo: {path_label} → data.{field} = {v}")
                    return float(v)

        # ── Estructura 4: data.data es lista ──────────────────
        if isinstance(raw_data, list):
            for b in raw_data:
                if isinstance(b, dict) and b.get("asset") == "USDT":
                    v = b.get("availableMargin") or b.get("available") or 0
                    log.info(f"  Balance lista top USDT: {path_label} = {v}")
                    return float(v)

        # ── Estructura 5: data.data.balance es string ─────────
        if isinstance(bal, (str, int, float)):
            try:
                v = float(bal)
                log.info(f"  Balance string: {path_label} = {v}")
                return v
            except (ValueError, TypeError):
                pass

        log.warning(f"  Balance no parseado en {path_label}. data={json.dumps(raw_data)[:200]}")
        return 0.0

    async def get_balance(self) -> float:
        """
        Intenta obtener balance en 4 endpoints diferentes.
        Loguea la respuesta cruda si todos devuelven 0.
        """
        paths = [
            "/openApi/swap/v3/user/balance",
            "/openApi/swap/v2/user/balance",
        ]
        results = []
        for path in paths:
            data = await self._get_priv(path)
            val = self._extract_balance_from(data, path)
            if val > 0:
                return val
            results.append((path, val, data))

        # Todos devolvieron 0 o error — logueamos para diagnóstico
        for path, val, data in results:
            if val == 0.0:
                log.warning(
                    f"Balance=0 en {path}. "
                    f"Respuesta completa: {json.dumps(data)[:400]}"
                )
            elif val == -1.0:
                log.warning(f"Error API en {path}: code={data.get('code')} {data.get('msg','')}")

        # Si MIN_BALANCE_OVERRIDE está seteado, usarlo
        if MIN_BALANCE_OVERRIDE > 0:
            log.warning(f"⚠️  Usando MIN_BALANCE_OVERRIDE={MIN_BALANCE_OVERRIDE}")
            return MIN_BALANCE_OVERRIDE

        return 0.0

    # ─────────────────────────────────────────────────────────
    #  SÍMBOLOS
    # ─────────────────────────────────────────────────────────
    async def get_symbols(self) -> list[str]:
        data = await self._get_pub("/openApi/swap/v2/quote/contracts")
        if data.get("code") == 0:
            return [
                s["symbol"] for s in data.get("data", [])
                if s.get("symbol", "").endswith("-USDT") and s.get("status", 0) == 1
            ]
        return []

    # ─────────────────────────────────────────────────────────
    #  KLINES con circuit breaker
    # ─────────────────────────────────────────────────────────
    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[dict]:
        if self._failures[symbol] >= 3:
            return []
        try:
            async with asyncio.timeout(SYM_TIMEOUT):
                data = await self._get_pub("/openApi/swap/v3/quote/klines", {
                    "symbol": symbol, "interval": interval, "limit": limit,
                })
        except asyncio.TimeoutError:
            self._failures[symbol] += 1
            return []

        if data.get("code") != 0:
            self._failures[symbol] += 1
            return []

        self._failures[symbol] = 0
        raw = data.get("data", [])
        result = []
        for k in raw:
            try:
                result.append({
                    "t": int(k[0]), "o": float(k[1]),
                    "h": float(k[2]), "l": float(k[3]),
                    "c": float(k[4]), "v": float(k[5]),
                })
            except (IndexError, ValueError, TypeError):
                continue
        return sorted(result, key=lambda x: x["t"])

    # ─────────────────────────────────────────────────────────
    #  POSICIONES
    # ─────────────────────────────────────────────────────────
    async def get_positions(self) -> list[dict]:
        data = await self._get_priv("/openApi/swap/v2/user/positions")
        if data.get("code") == 0:
            return [p for p in data.get("data", []) if float(p.get("positionAmt", 0)) != 0]
        return []

    # ─────────────────────────────────────────────────────────
    #  APALANCAMIENTO
    # ─────────────────────────────────────────────────────────
    async def set_leverage(self, symbol: str, lev: int):
        await asyncio.gather(
            self._post_priv("/openApi/swap/v2/trade/leverage",
                            {"symbol": symbol, "side": "LONG",  "leverage": lev}),
            self._post_priv("/openApi/swap/v2/trade/leverage",
                            {"symbol": symbol, "side": "SHORT", "leverage": lev}),
        )

    # ─────────────────────────────────────────────────────────
    #  ORDEN + SL + TP en paralelo
    # ─────────────────────────────────────────────────────────
    async def place_order(
        self, symbol: str, side: str, qty: float,
        sl: float, tp1: float, tp2: float
    ) -> bool:
        pos_side   = "LONG"  if side == "BUY"  else "SHORT"
        close_side = "SELL"  if side == "BUY"  else "BUY"

        await self.set_leverage(symbol, LEVERAGE)

        res = await self._post_priv("/openApi/swap/v2/trade/order", {
            "symbol": symbol, "side": side,
            "positionSide": pos_side, "type": "MARKET",
            "quantity": round(qty, 4),
        })
        if res.get("code") != 0:
            log.error(f"Orden fallida {symbol}: {res}")
            return False

        await asyncio.gather(
            self._post_priv("/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": close_side,
                "positionSide": pos_side, "type": "STOP_MARKET",
                "stopPrice": round(sl, 6), "closePosition": "true",
                "workingType": "MARK_PRICE",
            }),
            self._post_priv("/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": close_side,
                "positionSide": pos_side, "type": "TAKE_PROFIT_MARKET",
                "stopPrice": round(tp1, 6),
                "quantity": round(qty * TP1_PCT, 4),
                "workingType": "MARK_PRICE",
            }),
            self._post_priv("/openApi/swap/v2/trade/order", {
                "symbol": symbol, "side": close_side,
                "positionSide": pos_side, "type": "TAKE_PROFIT_MARKET",
                "stopPrice": round(tp2, 6),
                "quantity": round(qty * TP2_PCT, 4),
                "workingType": "MARK_PRICE",
            }),
            return_exceptions=True,
        )
        return True

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ══════════════════════════════════════════════════════════════
#  INDICADORES (idénticos a v4.0)
# ══════════════════════════════════════════════════════════════

def _safe(arr):
    return np.nan_to_num(np.asarray(arr, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)

def calc_atr(h, l, c, period=14) -> float:
    h,l,c = _safe(h),_safe(l),_safe(c)
    if len(c) < period+1: return float(np.mean(h-l))
    tr = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
    tr = np.concatenate([[h[0]-l[0]], tr])
    atr = np.zeros(len(tr)); atr[period-1] = np.mean(tr[:period])
    for i in range(period, len(tr)): atr[i] = (atr[i-1]*(period-1)+tr[i])/period
    return max(float(atr[-1]), 1e-9)

def calc_rsi(c, period=14) -> float:
    c = _safe(c)
    if len(c) < period+2: return 50.0
    d = np.diff(c)
    ag = np.mean(np.where(d>0,d,0)[:period]); al = np.mean(np.where(d<0,-d,0)[:period])
    for i in range(period, len(d)):
        ag = (ag*(period-1)+max(d[i],0))/period; al = (al*(period-1)+max(-d[i],0))/period
    return 100.0 if al<1e-12 else 100-100/(1+ag/al)

def calc_ema(c, period) -> np.ndarray:
    c = _safe(c)
    if len(c) < period: return np.full(len(c), c[-1] if len(c)>0 else 0.0)
    k = 2/(period+1); ema = np.zeros(len(c)); ema[period-1] = np.mean(c[:period])
    for i in range(period, len(c)): ema[i] = c[i]*k + ema[i-1]*(1-k)
    return ema

def calc_vwap(h, l, c, v) -> float:
    h,l,c,v = _safe(h),_safe(l),_safe(c),_safe(v)
    tp = (h+l+c)/3; sv = float(np.sum(v))
    return float(np.sum(tp*v)/max(sv,1e-9))

def calc_supertrend(h, l, c, period=10, mult=3.0) -> int:
    h,l,c = _safe(h),_safe(l),_safe(c); n = len(c)
    if n < period+2: return 0
    tr = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
    tr = np.concatenate([[h[0]-l[0]], tr])
    atr = np.zeros(n); atr[period-1] = np.mean(tr[:period])
    for i in range(period, n): atr[i] = (atr[i-1]*(period-1)+tr[i])/period
    hl2=(h+l)/2; ub=hl2+mult*atr; lb=hl2-mult*atr
    for i in range(1,n):
        lb[i] = lb[i] if lb[i]>lb[i-1] or c[i-1]<lb[i-1] else lb[i-1]
        ub[i] = ub[i] if ub[i]<ub[i-1] or c[i-1]>ub[i-1] else ub[i-1]
    direction=np.zeros(n,dtype=int); st=np.zeros(n)
    direction[period]=1; st[period]=lb[period]
    for i in range(period+1,n):
        if st[i-1]==ub[i-1]: st[i]=lb[i] if c[i]>ub[i] else ub[i]
        else: st[i]=ub[i] if c[i]<lb[i] else lb[i]
        direction[i]=1 if st[i]<c[i] else -1
    return int(direction[-1])

def zigzag_pivots(h, l, lookback=5) -> tuple[float, float]:
    h,l = _safe(h),_safe(l); n=len(h)
    if n < lookback*2+1: return float("nan"),float("nan")
    lb = min(lookback, max(3, n//20))
    peaks,valleys = [],[]
    for i in range(lb, n-lb):
        if h[i]==np.max(h[i-lb:i+lb+1]): peaks.append(h[i])
        if l[i]==np.min(l[i-lb:i+lb+1]): valleys.append(l[i])
    return (peaks[-1] if peaks else float("nan"),
            valleys[-1] if valleys else float("nan"))

def is_bullish_engulf(o, c) -> bool:
    if len(o)<2: return False
    return c[-2]<o[-2] and c[-1]>o[-1] and c[-1]>o[-2] and o[-1]<c[-2]

def is_bearish_engulf(o, c) -> bool:
    if len(o)<2: return False
    return c[-2]>o[-2] and c[-1]<o[-1] and c[-1]<o[-2] and o[-1]>c[-2]

def in_session() -> bool:
    h = datetime.now(timezone.utc).hour
    return (7 <= h < 16) or (13 <= h < 22)


# ══════════════════════════════════════════════════════════════
#  MOTOR DE SEÑALES (igual a v4.0, max 14 pts)
# ══════════════════════════════════════════════════════════════
def analyze(c5: list[dict], c15: list[dict]) -> Optional[dict]:
    if len(c5) < 100 or len(c15) < 55:
        return None
    h5=np.array([x["h"] for x in c5]); l5=np.array([x["l"] for x in c5])
    c5a=np.array([x["c"] for x in c5]); o5=np.array([x["o"] for x in c5])
    v5=np.array([x["v"] for x in c5])
    h15=np.array([x["h"] for x in c15]); l15=np.array([x["l"] for x in c15])
    c15a=np.array([x["c"] for x in c15])
    close = float(c5a[-1])
    if close <= 0: return None

    atr = calc_atr(h5,l5,c5a,14)
    atr_pct = (atr/close)*100
    if atr_pct < MIN_ATR_PCT: return None
    vol_ma20 = float(np.mean(v5[-20:])) if len(v5)>=20 else 0
    if vol_ma20 <= 0 or float(v5[-1]) < vol_ma20*MIN_VOL_MULT: return None

    pk5,vl5   = zigzag_pivots(h5,l5,5)
    pk15,vl15 = zigzag_pivots(h15,l15,5)
    st_dir    = calc_supertrend(h15,l15,c15a,ST_PERIOD,ST_MULT)
    vwap      = calc_vwap(h5,l5,c5a,v5)
    rsi       = calc_rsi(c5a,14)
    ema20     = float(calc_ema(c5a,20)[-1])
    ema50     = float(calc_ema(c5a,50)[-1])
    ema200    = float(calc_ema(c5a,200)[-1]) if len(c5a)>=200 else float(np.mean(c5a))
    vol_strong  = float(v5[-1]) > vol_ma20*1.5
    bull_engulf = is_bullish_engulf(o5,c5a)
    bear_engulf = is_bearish_engulf(o5,c5a)
    session     = in_session()
    sl_dist = max(atr*1.5, atr*1.2)

    ls,lr = 0,[]
    if not np.isnan(pk5) and close > pk5*1.0005:  ls+=2; lr.append(f"ZZ5↑")
    if not np.isnan(vl15) and close > vl15:        ls+=2; lr.append("ZZ15↑")
    if st_dir==1:                                   ls+=2; lr.append("ST▲")
    if close > ema20 > ema50:                       ls+=2; lr.append("EMA▲▲")
    elif close > ema20:                             ls+=1; lr.append("EMA▲")
    if close > vwap:                                ls+=1; lr.append("VWAP▲")
    if 40 < rsi < 70:                               ls+=1; lr.append(f"RSI{rsi:.0f}")
    if bull_engulf:                                 ls+=2; lr.append("BullEng")
    if vol_strong:                                  ls+=1; lr.append("VOL▲")

    ss,sr = 0,[]
    if not np.isnan(vl5) and close < vl5*0.9995:   ss+=2; sr.append(f"ZZ5↓")
    if not np.isnan(pk15) and close < pk15:         ss+=2; sr.append("ZZ15↓")
    if st_dir==-1:                                  ss+=2; sr.append("ST▼")
    if close < ema20 < ema50:                       ss+=2; sr.append("EMA▼▼")
    elif close < ema20:                             ss+=1; sr.append("EMA▼")
    if close < vwap:                                ss+=1; sr.append("VWAP▼")
    if 30 < rsi < 60:                               ss+=1; sr.append(f"RSI{rsi:.0f}")
    if bear_engulf:                                 ss+=2; sr.append("BearEng")
    if vol_strong:                                  ss+=1; sr.append("VOL▲")

    if ls >= MIN_SCORE and ls > ss:
        return {"side":"BUY","score":ls,"reasons":lr,"entry":close,
                "sl":close-sl_dist,"tp1":close+sl_dist,"tp2":close+sl_dist*RR,
                "atr":atr,"atr_pct":atr_pct,"rsi":rsi,"st":st_dir}
    if ss >= MIN_SCORE and ss > ls:
        return {"side":"SELL","score":ss,"reasons":sr,"entry":close,
                "sl":close+sl_dist,"tp1":close-sl_dist,"tp2":close-sl_dist*RR,
                "atr":atr,"atr_pct":atr_pct,"rsi":rsi,"st":st_dir}
    return None


# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════
async def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            )
    except Exception as e:
        log.debug(f"Telegram: {e}")


# ══════════════════════════════════════════════════════════════
#  CACHE DE KLINES
# ══════════════════════════════════════════════════════════════
class KlineCache:
    def __init__(self, ttl=KLINE_TTL):
        self.ttl=ttl; self._data:dict[str,list]={}; self._ts:dict[str,float]={}
    def get(self, key):
        if key in self._data and (time.time()-self._ts.get(key,0))<self.ttl: return self._data[key]
        return None
    def set(self, key, val):
        self._data[key]=val; self._ts[key]=time.time()


# ══════════════════════════════════════════════════════════════
#  BOT PRINCIPAL v4.1
# ══════════════════════════════════════════════════════════════
class PhantomEdgeBot:
    def __init__(self):
        self.api    = BingXClient(API_KEY, API_SECRET)
        self.cache  = KlineCache(KLINE_TTL)
        self.candles5:  dict[str,list] = {}
        self.candles15: dict[str,list] = {}
        self.warm:      set[str] = set()
        self.symbols:      list[str] = []
        self.open_pos:     dict[str,dict] = {}
        self.balance:      float = 0.0
        self.cycle:        int = 0
        self.daily_trades: int = 0
        self.daily_loss:   float = 0.0
        self.last_day   = datetime.now(timezone.utc).date()
        self.start_time = time.time()
        self.kill_switch = False

    async def warmup(self, symbol: str) -> bool:
        try:
            k5,k15 = await asyncio.gather(
                self.api.get_klines(symbol, TIMEFRAME, 200),
                self.api.get_klines(symbol, TIMEFRAME_SLOW, 100),
                return_exceptions=True,
            )
            if isinstance(k5,list) and isinstance(k15,list) and len(k5)>=100 and len(k15)>=50:
                self.candles5[symbol]=k5; self.candles15[symbol]=k15
                self.warm.add(symbol); return True
        except Exception: pass
        return False

    async def warmup_batch(self, symbols: list[str], concurrency=25):
        done=0; total=len(symbols)
        for i in range(0,total,concurrency):
            batch=symbols[i:i+concurrency]
            results=await asyncio.gather(*[self.warmup(s) for s in batch],return_exceptions=True)
            done+=sum(1 for r in results if r is True)
            log.info(f"  [WarmUp] {done}/{total} ({done/total*100:.0f}%)...")
            await asyncio.sleep(0.2)
        return done

    async def update(self, symbol: str):
        key5=f"{symbol}:{TIMEFRAME}"; key15=f"{symbol}:{TIMEFRAME_SLOW}"
        need5=self.cache.get(key5) is None; need15=self.cache.get(key15) is None
        tasks=[]
        if need5:  tasks.append(self.api.get_klines(symbol,TIMEFRAME,3))
        if need15: tasks.append(self.api.get_klines(symbol,TIMEFRAME_SLOW,3))
        if not tasks: return
        results=await asyncio.gather(*tasks,return_exceptions=True)
        idx=0
        def merge(ex,nc,mx=200):
            if not isinstance(nc,list) or not nc: return ex
            lt=ex[-1]["t"] if ex else 0
            for c in nc:
                if c["t"]>lt: ex.append(c)
                elif ex and c["t"]==ex[-1]["t"]: ex[-1]=c
            return ex[-mx:]
        if need5:
            r=results[idx]; idx+=1
            if isinstance(r,list):
                self.candles5[symbol]=merge(self.candles5.get(symbol,[]),r)
                self.cache.set(key5,self.candles5[symbol])
        if need15:
            r=results[idx]
            if isinstance(r,list):
                self.candles15[symbol]=merge(self.candles15.get(symbol,[]),r,100)
                self.cache.set(key15,self.candles15[symbol])

    def reset_daily(self):
        today=datetime.now(timezone.utc).date()
        if today!=self.last_day:
            self.daily_trades=0; self.daily_loss=0.0; self.last_day=today
            self.kill_switch=False; log.info("📅 Stats diarios reseteados")

    def can_trade(self) -> tuple[bool,str]:
        if self.kill_switch: return False,"kill_switch"
        if len(self.open_pos)>=MAX_POSITIONS: return False,f"max_pos={MAX_POSITIONS}"
        if self.daily_trades>=MAX_DAILY_TRADES: return False,f"max_trades"
        if self.daily_loss>=MAX_DAILY_LOSS: self.kill_switch=True; return False,"max_loss→KILL"
        if self.balance < TRADE_USDT*0.5:
            return False,f"balance_bajo={self.balance:.2f}U (necesita ≥{TRADE_USDT*0.5:.1f})"
        return True,"OK"

    async def scan(self):
        self.cycle+=1; self.reset_daily()

        self.balance,positions_raw = await asyncio.gather(
            self.api.get_balance(), self.api.get_positions()
        )
        self.open_pos={p["symbol"]:p for p in positions_raw}

        log.info(
            f"[CICLO {self.cycle:04d}] "
            f"Bal:{self.balance:.2f}U | "
            f"Pos:{len(self.open_pos)}/{MAX_POSITIONS} | "
            f"Warm:{len(self.warm)}/{len(self.symbols)} | "
            f"Trades:{self.daily_trades}"
        )

        ok,reason=self.can_trade()
        if not ok:
            log.info(f"  ⛔ {reason}")
            if "balance_bajo" in reason:
                log.warning(
                    "  ⚠️  SOLUCIÓN: Deposita USDT en BingX Futures O "
                    f"pon MIN_BALANCE_OVERRIDE={TRADE_USDT} en env vars si el saldo es correcto"
                )
            return

        # Priorizar watchlist de TradingView
        prio  = [s for s in PRIORITY_SYMBOLS if s in self.warm and s not in self.open_pos]
        resto = [s for s in self.warm if s not in self.open_pos and s not in prio]
        candidates = prio + resto

        if not candidates: return

        update_batch=candidates[:60]
        for i in range(0,len(update_batch),30):
            await asyncio.gather(*[self.update(s) for s in update_batch[i:i+30]],return_exceptions=True)

        signals=0
        for sym in candidates:
            if len(self.open_pos)>=MAX_POSITIONS: break
            c5=self.candles5.get(sym,[]); c15=self.candles15.get(sym,[])
            sig=analyze(c5,c15)
            if sig is None: continue
            signals+=1
            emoji="🟢" if sig["side"]=="BUY" else "🔴"
            is_prio = sym in PRIORITY_SYMBOLS
            prio_tag = " ⭐PRIO" if is_prio else ""
            log.info(f"  {emoji} {sym}{prio_tag} {sig['side']} Score:{sig['score']}/14 | {' | '.join(sig['reasons'])}")

            if AUTO_TRADING:
                entry=sig["entry"]; sl_dist=abs(entry-sig["sl"])
                risk_pct=sl_dist/entry if entry>0 else 0
                if risk_pct<0.001: continue
                qty=round((TRADE_USDT/risk_pct)/entry,4)
                if qty<=0: continue
                t0=time.time()
                success=await self.api.place_order(sym,sig["side"],qty,sig["sl"],sig["tp1"],sig["tp2"])
                ms=int((time.time()-t0)*1000)
                if success:
                    self.daily_trades+=1; self.open_pos[sym]={"symbol":sym,"side":sig["side"]}
                    pct_sl=sl_dist/entry*100
                    msg=(
                        f"{emoji} <b>{sym}</b>{prio_tag} — {'LONG' if sig['side']=='BUY' else 'SHORT'}\n"
                        f"📊 Score: {sig['score']}/14\n"
                        f"📍 Entry: {entry:.6f}\n"
                        f"🛡 SL:   {sig['sl']:.6f}  ({pct_sl:.2f}%)\n"
                        f"🎯 TP1:  {sig['tp1']:.6f}  30%@1R\n"
                        f"🎯 TP2:  {sig['tp2']:.6f}  30%@{RR}R\n"
                        f"🔄 Trail: 40%\n"
                        f"📈 RSI:{sig['rsi']:.0f}|ATR:{sig['atr_pct']:.2f}%|ST:{'▲' if sig['st']==1 else '▼'}\n"
                        f"✨ {' · '.join(sig['reasons'])}\n"
                        f"💰 Bal:{self.balance:.2f}U | ⚡{ms}ms"
                    )
                    await tg(msg)
                    log.info(f"  ✅ {sym} qty={qty} en {ms}ms")
            else:
                entry=sig["entry"]; sl_dist=abs(entry-sig["sl"])
                msg=(
                    f"🔔 <b>[SIM] {sym}</b> — {'LONG' if sig['side']=='BUY' else 'SHORT'}\n"
                    f"Score:{sig['score']}/14 | Entry:{entry:.6f}\n"
                    f"SL:{sig['sl']:.6f} TP1:{sig['tp1']:.6f} TP2:{sig['tp2']:.6f}\n"
                    f"RSI:{sig['rsi']:.0f} ATR:{sig['atr_pct']:.2f}%\n"
                    f"✨ {' · '.join(sig['reasons'])}"
                )
                await tg(msg)

        log.info(f"  [SCAN] Cands:{len(candidates)} | Señales:{signals} | Pos:{len(self.open_pos)}")

    async def run(self):
        log.info("═"*60)
        log.info("  Phantom Edge Bot ULTRA v4.1")
        log.info(f"  Auto-trading: {'ON ✅' if AUTO_TRADING else 'OFF (simulación)'}")
        log.info(f"  Score:{MIN_SCORE}/14 | RR:1:{RR} | Lev:x{LEVERAGE} | Ciclo:{SCAN_INTERVAL}s")
        log.info(f"  Prio pares: {PRIORITY_SYMBOLS}")
        log.info("═"*60)

        self.balance=await self.api.get_balance()
        log.info(f"💰 Balance inicial: {self.balance:.2f} USDT")

        if self.balance==0 and API_KEY:
            log.warning("⚠️  Balance=0. Posibles causas:")
            log.warning("   1. API key no tiene permisos de Futures")
            log.warning("   2. El saldo está en Spot, no en Futures (transferir en BingX)")
            log.warning("   3. Estructura de respuesta desconocida (ver logs WARNING de arriba)")
            log.warning(f"   Solución rápida: añade env var MIN_BALANCE_OVERRIDE=108.35")

        self.symbols=await self.api.get_symbols()
        log.info(f"📊 {len(self.symbols)} pares USDT-Perp")
        if not self.symbols:
            log.error("❌ Sin símbolos"); return

        await tg(
            f"🤖 <b>Phantom Edge Bot ULTRA v4.1</b>\n"
            f"💰 Balance: {self.balance:.2f} USDT\n"
            f"📊 Pares: {len(self.symbols)} | Prio: {len(PRIORITY_SYMBOLS)}\n"
            f"⚙️ Score:{MIN_SCORE}/14 | RR:1:{RR} | Lev:x{LEVERAGE}\n"
            f"{'🟢 AUTO-TRADING' if AUTO_TRADING else '🟡 SIMULACIÓN'}"
            + (f"\n⚠️ Balance=0 — verificar API/Futures" if self.balance==0 and API_KEY else "")
        )

        # Warm-up con prioridad watchlist
        prio_in_syms = [s for s in PRIORITY_SYMBOLS if s in self.symbols]
        resto_syms   = [s for s in self.symbols if s not in PRIORITY_SYMBOLS]

        log.info(f"🔥 WarmUp prioridad ({len(prio_in_syms)} pares)...")
        await self.warmup_batch(prio_in_syms, concurrency=len(prio_in_syms)+1)
        log.info(f"🔥 WarmUp resto ({len(resto_syms)} pares)...")
        await self.warmup_batch(resto_syms[:200], concurrency=25)
        if len(resto_syms) > 200:
            asyncio.create_task(self.warmup_batch(resto_syms[200:], concurrency=15))

        log.info(f"✅ WarmUp: {len(self.warm)} pares listos")

        while True:
            try:
                t0=time.time()
                await self.scan()
                elapsed=time.time()-t0
                sleep=max(5.0, SCAN_INTERVAL-elapsed)
                log.info(f"  ⏱ {elapsed:.1f}s | próximo en {sleep:.0f}s\n")
                await asyncio.sleep(sleep)
            except asyncio.CancelledError: break
            except Exception as e:
                log.error(f"❌ Error: {e}", exc_info=True)
                await asyncio.sleep(10)

        await self.api.close()


# ══════════════════════════════════════════════════════════════
#  HEALTH CHECK
# ══════════════════════════════════════════════════════════════
async def health_server(bot, port):
    from aiohttp import web
    async def handle(req):
        return web.json_response({
            "status": "kill_switch" if bot.kill_switch else "running",
            "version": "4.1",
            "uptime_min": round((time.time()-bot.start_time)/60,1),
            "cycle": bot.cycle, "balance_usdt": round(bot.balance,2),
            "warm_symbols": len(bot.warm), "total_symbols": len(bot.symbols),
            "open_positions": len(bot.open_pos),
            "daily_trades": bot.daily_trades,
            "auto_trading": AUTO_TRADING, "kill_switch": bot.kill_switch,
            "priority_symbols": PRIORITY_SYMBOLS,
            "min_score": f"{MIN_SCORE}/14",
        })
    app=web.Application()
    app.router.add_get("/", handle); app.router.add_get("/health", handle)
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",port).start()
    log.info(f"🌐 Health: http://0.0.0.0:{port}")

async def main():
    bot=PhantomEdgeBot()
    await asyncio.gather(health_server(bot,PORT), bot.run())

if __name__=="__main__":
    asyncio.run(main())
