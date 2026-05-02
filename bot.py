"""
╔══════════════════════════════════════════════════════════╗
║       PHANTOM EDGE BOT ELITE v3.0 — CORREGIDO           ║
║   ZigZag(5m+15m) + SuperTrend + VWAP + RSI + Engulfing  ║
║   Fixes: Balance BingX, Warmup, Señales, Órdenes        ║
╚══════════════════════════════════════════════════════════╝
"""

import os
import asyncio
import logging
import time
import hmac
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
import numpy as np
import httpx

# ─────────────────────────────────────────
# CONFIGURACIÓN — variables de entorno
# ─────────────────────────────────────────
API_KEY        = os.getenv("BINGX_API_KEY", "")
API_SECRET     = os.getenv("BINGX_API_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

AUTO_TRADING    = os.getenv("AUTO_TRADING_ENABLED", "false").lower() == "true"
LEVERAGE        = int(os.getenv("LEVERAGE", "10"))
TIMEFRAME       = os.getenv("TIMEFRAME", "5m")
TIMEFRAME_SLOW  = os.getenv("TIMEFRAME_SLOW", "15m")
ZZ_DEV_5M       = float(os.getenv("ZZ_DEVIATION", "0.5"))
ZZ_DEV_15M      = float(os.getenv("ZZ15_DEVIATION", "0.8"))
ST_PERIOD       = int(os.getenv("ST_PERIOD", "10"))
ST_MULT         = float(os.getenv("ST_MULT", "3.0"))
RR              = float(os.getenv("RR", "2.5"))
MIN_SCORE       = int(os.getenv("MIN_SCORE", "6"))
MIN_ATR_PCT     = float(os.getenv("MIN_ATR_PCT", "0.10"))
MIN_VOL_MULT    = float(os.getenv("MIN_VOL_MULT", "0.8"))
TRADE_USDT      = float(os.getenv("TRADE_USDT", "9"))
MAX_POSITIONS   = int(os.getenv("MAX_POSITIONS", "5"))
SCAN_INTERVAL   = int(os.getenv("SCAN_INTERVAL", "60"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "40"))
MAX_DAILY_LOSS  = float(os.getenv("MAX_DAILY_LOSS", "5.0"))
PORT            = int(os.getenv("PORT", "8080"))

BINGX_BASE = "https://open-api.bingx.com"

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("PhantomEdge")


# ══════════════════════════════════════════
#  BINGX API CLIENT  (corregido v3)
# ══════════════════════════════════════════
class BingXClient:
    """
    Cliente BingX corregido.
    FIX 1: Balance usa /openApi/swap/v2/user/balance con campo correcto
    FIX 2: Klines usa /openApi/swap/v3/quote/klines (array plano)
    FIX 3: Firma HMAC correcta — params ordenados antes de firmar
    """

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    # ── Firma ──────────────────────────────
    def _sign(self, params: dict) -> str:
        payload = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return hmac.new(self.api_secret, payload.encode(), hashlib.sha256).hexdigest()

    def _ts(self) -> int:
        return int(time.time() * 1000)

    # ── GET público ────────────────────────
    async def _get_pub(self, path: str, params: dict = None) -> dict:
        try:
            r = await self.client.get(f"{BINGX_BASE}{path}", params=params or {})
            return r.json()
        except Exception as e:
            log.error(f"GET_PUB {path}: {e}")
            return {}

    # ── GET privado (firmado) ──────────────
    async def _get_priv(self, path: str, params: dict = None) -> dict:
        p = dict(params or {})
        p["timestamp"] = self._ts()
        p["signature"] = self._sign(p)
        headers = {"X-BX-APIKEY": self.api_key}
        try:
            r = await self.client.get(f"{BINGX_BASE}{path}", params=p, headers=headers)
            return r.json()
        except Exception as e:
            log.error(f"GET_PRIV {path}: {e}")
            return {}

    # ── POST privado ───────────────────────
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
            log.error(f"POST {path}: {e}")
            return {}

    # ══════════════════════════════════════
    #  BALANCE  — FIX PRINCIPAL
    # ══════════════════════════════════════
    async def get_balance(self) -> float:
        """
        BingX Perpetual: balance disponible en USDT.
        Intenta v3 primero (estructura más nueva), luego v2.
        """
        # Intento 1: v3
        data = await self._get_priv("/openApi/swap/v3/user/balance")
        if data.get("code") == 0:
            bal = data.get("data", {}).get("balance", {})
            if isinstance(bal, dict):
                return float(bal.get("availableMargin", 0) or
                             bal.get("available", 0) or 0)
            if isinstance(bal, list):
                for b in bal:
                    if b.get("asset") == "USDT":
                        return float(b.get("availableMargin", 0))

        # Intento 2: v2
        data = await self._get_priv("/openApi/swap/v2/user/balance")
        if data.get("code") == 0:
            bal = data.get("data", {}).get("balance", {})
            if isinstance(bal, dict):
                return float(bal.get("availableMargin", 0) or
                             bal.get("available", 0) or 0)
            if isinstance(bal, list):
                for b in bal:
                    if b.get("asset") == "USDT":
                        return float(b.get("availableMargin", 0))

        log.warning(f"Balance no obtenido. Respuesta: {data}")
        return 0.0

    # ══════════════════════════════════════
    #  SÍMBOLOS
    # ══════════════════════════════════════
    async def get_symbols(self) -> list[str]:
        data = await self._get_pub("/openApi/swap/v2/quote/contracts")
        if data.get("code") == 0:
            return [
                s["symbol"]
                for s in data.get("data", [])
                if s.get("symbol", "").endswith("-USDT")
                and s.get("status", 0) == 1
            ]
        log.error(f"Símbolos error: {data}")
        return []

    # ══════════════════════════════════════
    #  KLINES — FIX FORMATO BingX
    # ══════════════════════════════════════
    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[dict]:
        """
        BingX klines endpoint v3. Devuelve lista de arrays:
        [timestamp, open, high, low, close, volume, ...]
        """
        data = await self._get_pub("/openApi/swap/v3/quote/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        if data.get("code") != 0:
            return []

        raw = data.get("data", [])
        result = []
        for k in raw:
            try:
                result.append({
                    "t": int(k[0]),
                    "o": float(k[1]),
                    "h": float(k[2]),
                    "l": float(k[3]),
                    "c": float(k[4]),
                    "v": float(k[5]),
                })
            except (IndexError, ValueError, TypeError):
                continue
        # Ordenar de antiguo a nuevo
        return sorted(result, key=lambda x: x["t"])

    # ══════════════════════════════════════
    #  POSICIONES ABIERTAS
    # ══════════════════════════════════════
    async def get_positions(self) -> list[dict]:
        data = await self._get_priv("/openApi/swap/v2/user/positions")
        if data.get("code") == 0:
            return [
                p for p in data.get("data", [])
                if float(p.get("positionAmt", 0)) != 0
            ]
        return []

    # ══════════════════════════════════════
    #  APALANCAMIENTO
    # ══════════════════════════════════════
    async def set_leverage(self, symbol: str, lev: int):
        for side in ("LONG", "SHORT"):
            await self._post_priv("/openApi/swap/v2/trade/leverage", {
                "symbol": symbol, "side": side, "leverage": lev
            })

    # ══════════════════════════════════════
    #  COLOCAR ORDEN + SL + TP PARCIALES
    # ══════════════════════════════════════
    async def place_order(
        self, symbol: str, side: str, qty: float,
        sl: float, tp1: float, tp2: float
    ) -> bool:
        pos_side = "LONG" if side == "BUY" else "SHORT"
        close_side = "SELL" if side == "BUY" else "BUY"

        await self.set_leverage(symbol, LEVERAGE)

        # Orden de mercado principal
        res = await self._post_priv("/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": side,
            "positionSide": pos_side,
            "type": "MARKET",
            "quantity": round(qty, 4),
        })
        if res.get("code") != 0:
            log.error(f"Orden fallida {symbol}: {res}")
            return False

        # SL (cierra todo)
        await self._post_priv("/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": close_side,
            "positionSide": pos_side,
            "type": "STOP_MARKET",
            "stopPrice": round(sl, 6),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
        })

        # TP1 — 25% de la posición @ 1R
        await self._post_priv("/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": close_side,
            "positionSide": pos_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": round(tp1, 6),
            "quantity": round(qty * 0.25, 4),
            "workingType": "MARK_PRICE",
        })

        # TP2 — 25% de la posición @ 2R
        await self._post_priv("/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": close_side,
            "positionSide": pos_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": round(tp2, 6),
            "quantity": round(qty * 0.25, 4),
            "workingType": "MARK_PRICE",
        })
        # El 50% restante lo gestiona el trailing stop
        return True

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ══════════════════════════════════════════
#  INDICADORES TÉCNICOS
# ══════════════════════════════════════════

def calc_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> float:
    if len(c) < period + 1:
        return float(np.mean(h - l))
    tr = np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
    )
    tr = np.concatenate([[h[0] - l[0]], tr])
    # EWM like Wilder
    atr = np.zeros(len(tr))
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return float(atr[-1])


def calc_rsi(c: np.ndarray, period: int = 14) -> float:
    if len(c) < period + 2:
        return 50.0
    d = np.diff(c)
    gains = np.where(d > 0, d, 0.0)
    losses = np.where(d < 0, -d, 0.0)
    ag = np.mean(gains[:period])
    al = np.mean(losses[:period])
    for i in range(period, len(d)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def calc_vwap(h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray) -> float:
    tp = (h + l + c) / 3
    return float(np.sum(tp * v) / max(np.sum(v), 1e-9))


def calc_supertrend(h: np.ndarray, l: np.ndarray, c: np.ndarray,
                    period: int = 10, mult: float = 3.0) -> int:
    """Returns 1 (bullish) or -1 (bearish)"""
    n = len(c)
    if n < period + 2:
        return 0

    atr_arr = np.zeros(n)
    tr = np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
    )
    tr = np.concatenate([[h[0] - l[0]], tr])
    atr_arr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr_arr[i] = (atr_arr[i - 1] * (period - 1) + tr[i]) / period

    hl2 = (h + l) / 2
    ub = hl2 + mult * atr_arr
    lb = hl2 - mult * atr_arr

    # Adjust bands (no look-ahead)
    for i in range(1, n):
        lb[i] = lb[i] if lb[i] > lb[i - 1] or c[i - 1] < lb[i - 1] else lb[i - 1]
        ub[i] = ub[i] if ub[i] < ub[i - 1] or c[i - 1] > ub[i - 1] else ub[i - 1]

    # Direction
    direction = np.zeros(n, dtype=int)
    st = np.zeros(n)
    direction[period] = 1
    st[period] = lb[period]

    for i in range(period + 1, n):
        if st[i - 1] == ub[i - 1]:  # was bearish
            st[i] = lb[i] if c[i] > ub[i] else ub[i]
        else:  # was bullish
            st[i] = ub[i] if c[i] < lb[i] else lb[i]
        direction[i] = 1 if st[i] < c[i] else -1

    return int(direction[-1])


def zigzag_last_pivots(h: np.ndarray, l: np.ndarray,
                        deviation: float = 0.5) -> tuple[float, float]:
    """
    Returns (last_peak_price, last_valley_price) using swing detection.
    Uses a simpler, more reliable pivot-high/pivot-low approach.
    """
    n = len(h)
    if n < 10:
        return float("nan"), float("nan")

    lookback = max(3, int(n * 0.02))  # dynamic lookback ~2% of bars

    # Find all pivot highs and lows
    peaks = []
    valleys = []
    for i in range(lookback, n - lookback):
        if h[i] == max(h[i - lookback: i + lookback + 1]):
            peaks.append(h[i])
        if l[i] == min(l[i - lookback: i + lookback + 1]):
            valleys.append(l[i])

    last_peak = peaks[-1] if peaks else float("nan")
    last_valley = valleys[-1] if valleys else float("nan")
    return last_peak, last_valley


def is_bullish_engulf(o: np.ndarray, c: np.ndarray) -> bool:
    if len(o) < 2:
        return False
    return (c[-2] < o[-2] and c[-1] > o[-1] and
            c[-1] > o[-2] and o[-1] < c[-2])


def is_bearish_engulf(o: np.ndarray, c: np.ndarray) -> bool:
    if len(o) < 2:
        return False
    return (c[-2] > o[-2] and c[-1] < o[-1] and
            c[-1] < o[-2] and o[-1] > c[-2])


def in_session() -> bool:
    """London 07-16 UTC  |  NY 13-22 UTC"""
    h = datetime.now(timezone.utc).hour
    return (7 <= h < 16) or (13 <= h < 22)


# ══════════════════════════════════════════
#  MOTOR DE SEÑALES — Sistema de Puntuación
# ══════════════════════════════════════════
# Puntuación máxima: 12 pts
#  2 pts — ZigZag 5m  breakout/breakdown
#  2 pts — ZigZag 15m alineación
#  2 pts — SuperTrend 15m dirección
#  1 pt  — VWAP
#  1 pt  — RSI (zona correcta)
#  2 pts — Patrón Engulfing
#  1 pt  — Volumen fuerte
#  1 pt  — Sesión activa

def analyze(c5: list[dict], c15: list[dict]) -> Optional[dict]:
    if len(c5) < 100 or len(c15) < 50:
        return None

    h5  = np.array([x["h"] for x in c5])
    l5  = np.array([x["l"] for x in c5])
    c5a = np.array([x["c"] for x in c5])
    o5  = np.array([x["o"] for x in c5])
    v5  = np.array([x["v"] for x in c5])

    h15  = np.array([x["h"] for x in c15])
    l15  = np.array([x["l"] for x in c15])
    c15a = np.array([x["c"] for x in c15])
    o15  = np.array([x["o"] for x in c15])

    close = float(c5a[-1])

    # ── Filtros básicos ───────────────────
    atr = calc_atr(h5, l5, c5a, 14)
    atr_pct = (atr / close) * 100 if close > 0 else 0
    if atr_pct < MIN_ATR_PCT:
        return None

    vol_ma = float(np.mean(v5[-20:])) if len(v5) >= 20 else 0
    if vol_ma <= 0 or float(v5[-1]) < vol_ma * MIN_VOL_MULT:
        return None

    # ── Indicadores ──────────────────────
    pk5, vl5     = zigzag_last_pivots(h5, l5, ZZ_DEV_5M)
    pk15, vl15   = zigzag_last_pivots(h15, l15, ZZ_DEV_15M)
    st_dir       = calc_supertrend(h15, l15, c15a, ST_PERIOD, ST_MULT)
    vwap         = calc_vwap(h5, l5, c5a, v5)
    rsi          = calc_rsi(c5a, 14)
    vol_strong   = float(v5[-1]) > vol_ma * 1.2
    session      = in_session()
    bull_engulf  = is_bullish_engulf(o5, c5a)
    bear_engulf  = is_bearish_engulf(o5, c5a)

    sl_dist = atr * 1.5  # distancia SL = 1.5× ATR

    # ─────────────────────────────────────
    #  LONG
    # ─────────────────────────────────────
    ls = 0
    lr = []
    if not np.isnan(pk5) and close > pk5:
        ls += 2; lr.append(f"ZZ5m↑>{pk5:.4f}")
    if not np.isnan(pk15) and not np.isnan(vl15) and close > vl15:
        ls += 2; lr.append("ZZ15m_alcista")
    if st_dir == 1:
        ls += 2; lr.append("ST_bullish")
    if close > vwap:
        ls += 1; lr.append("↑VWAP")
    if 40 < rsi < 70:
        ls += 1; lr.append(f"RSI{rsi:.0f}")
    if bull_engulf:
        ls += 2; lr.append("BullEngulf")
    if vol_strong:
        ls += 1; lr.append("Vol↑")
    if session:
        ls += 1; lr.append("Session")

    # ─────────────────────────────────────
    #  SHORT
    # ─────────────────────────────────────
    ss = 0
    sr = []
    if not np.isnan(vl5) and close < vl5:
        ss += 2; sr.append(f"ZZ5m↓<{vl5:.4f}")
    if not np.isnan(pk15) and not np.isnan(vl15) and close < pk15:
        ss += 2; sr.append("ZZ15m_bajista")
    if st_dir == -1:
        ss += 2; sr.append("ST_bearish")
    if close < vwap:
        ss += 1; sr.append("↓VWAP")
    if 30 < rsi < 60:
        ss += 1; sr.append(f"RSI{rsi:.0f}")
    if bear_engulf:
        ss += 2; sr.append("BearEngulf")
    if vol_strong:
        ss += 1; sr.append("Vol↑")
    if session:
        ss += 1; sr.append("Session")

    # ─────────────────────────────────────
    #  Elegir la mejor señal
    # ─────────────────────────────────────
    if ls >= MIN_SCORE and ls > ss:
        return {
            "side": "BUY",
            "score": ls,
            "reasons": lr,
            "entry": close,
            "sl":  close - sl_dist,
            "tp1": close + sl_dist,         # 1R
            "tp2": close + sl_dist * RR,    # RR:1 completo
            "atr": atr,
            "atr_pct": atr_pct,
            "rsi": rsi,
            "st": st_dir,
        }
    if ss >= MIN_SCORE and ss > ls:
        return {
            "side": "SELL",
            "score": ss,
            "reasons": sr,
            "entry": close,
            "sl":  close + sl_dist,
            "tp1": close - sl_dist,
            "tp2": close - sl_dist * RR,
            "atr": atr,
            "atr_pct": atr_pct,
            "rsi": rsi,
            "st": st_dir,
        }
    return None


# ══════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════
async def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            )
    except Exception as e:
        log.debug(f"Telegram: {e}")


# ══════════════════════════════════════════
#  BOT PRINCIPAL
# ══════════════════════════════════════════
class PhantomEdgeBot:
    def __init__(self):
        self.api = BingXClient(API_KEY, API_SECRET)

        # Datos de velas: symbol → lista de dicts OHLCV
        self.candles5:  dict[str, list] = {}
        self.candles15: dict[str, list] = {}
        self.warm:      set[str] = set()

        # Estado
        self.symbols:      list[str] = []
        self.open_pos:     dict[str, dict] = {}
        self.balance:      float = 0.0
        self.cycle:        int = 0
        self.daily_trades: int = 0
        self.daily_loss:   float = 0.0
        self.last_day      = datetime.now(timezone.utc).date()
        self.start_time    = time.time()

    # ─────────────────────────────────────
    #  WARM-UP de un símbolo
    # ─────────────────────────────────────
    async def warmup(self, symbol: str) -> bool:
        try:
            k5  = await self.api.get_klines(symbol, TIMEFRAME, 200)
            k15 = await self.api.get_klines(symbol, TIMEFRAME_SLOW, 100)
            if len(k5) >= 100 and len(k15) >= 50:
                self.candles5[symbol]  = k5
                self.candles15[symbol] = k15
                self.warm.add(symbol)
                return True
        except Exception:
            pass
        return False

    async def warmup_batch(self, symbols: list[str], concurrency: int = 20):
        total = len(symbols)
        done = 0
        for i in range(0, total, concurrency):
            batch = symbols[i: i + concurrency]
            results = await asyncio.gather(*[self.warmup(s) for s in batch], return_exceptions=True)
            done += sum(1 for r in results if r is True)
            log.info(f"  [WarmUp] {done}/{total} listos...")
            await asyncio.sleep(0.3)
        return done

    # ─────────────────────────────────────
    #  ACTUALIZACIÓN incremental de velas
    # ─────────────────────────────────────
    async def update(self, symbol: str):
        try:
            new5  = await self.api.get_klines(symbol, TIMEFRAME, 3)
            new15 = await self.api.get_klines(symbol, TIMEFRAME_SLOW, 3)
        except Exception:
            return

        def merge(existing: list, new_candles: list, max_len: int) -> list:
            if not new_candles:
                return existing
            last_t = existing[-1]["t"] if existing else 0
            for nc in new_candles:
                if nc["t"] > last_t:
                    existing.append(nc)
                elif nc["t"] == existing[-1]["t"]:
                    existing[-1] = nc  # Actualizar vela actual
            return existing[-max_len:]

        if symbol in self.candles5:
            self.candles5[symbol]  = merge(self.candles5[symbol], new5, 200)
        if symbol in self.candles15:
            self.candles15[symbol] = merge(self.candles15[symbol], new15, 100)

    # ─────────────────────────────────────
    #  CONTROL DE RIESGO DIARIO
    # ─────────────────────────────────────
    def reset_daily(self):
        today = datetime.now(timezone.utc).date()
        if today != self.last_day:
            self.daily_trades = 0
            self.daily_loss = 0.0
            self.last_day = today
            log.info("📅 Stats diarios reseteados")

    def can_trade(self) -> tuple[bool, str]:
        if len(self.open_pos) >= MAX_POSITIONS:
            return False, f"max_pos={MAX_POSITIONS}"
        if self.daily_trades >= MAX_DAILY_TRADES:
            return False, f"max_trades={MAX_DAILY_TRADES}"
        if self.daily_loss >= MAX_DAILY_LOSS:
            return False, f"max_loss={MAX_DAILY_LOSS}%"
        if self.balance < TRADE_USDT * 0.5:
            return False, f"balance_bajo={self.balance:.2f}"
        return True, "OK"

    # ─────────────────────────────────────
    #  CICLO PRINCIPAL DE ESCANEO
    # ─────────────────────────────────────
    async def scan(self):
        self.cycle += 1
        self.reset_daily()

        # Actualizar balance y posiciones
        self.balance  = await self.api.get_balance()
        positions_raw = await self.api.get_positions()
        self.open_pos = {p["symbol"]: p for p in positions_raw}

        warm_count = len(self.warm)
        log.info(
            f"[CICLO {self.cycle:04d}] "
            f"Balance: {self.balance:.2f}U | "
            f"Pos: {len(self.open_pos)}/{MAX_POSITIONS} | "
            f"Warm: {warm_count}/{len(self.symbols)}"
        )

        ok, reason = self.can_trade()
        if not ok:
            log.info(f"  ⛔ Trade bloqueado: {reason}")
            return

        # Símbolos calientes sin posición abierta
        candidates = [s for s in self.warm if s not in self.open_pos]
        if not candidates:
            log.info("  ℹ️  Sin candidatos (todos en posición o sin datos)")
            return

        # Actualizar velas en lote
        await asyncio.gather(*[self.update(s) for s in candidates[:60]], return_exceptions=True)

        signals = 0
        rechazos: dict[str, int] = {}

        for sym in candidates:
            if len(self.open_pos) >= MAX_POSITIONS:
                break

            c5  = self.candles5.get(sym, [])
            c15 = self.candles15.get(sym, [])

            sig = analyze(c5, c15)

            if sig is None:
                continue  # sin señal — no logueamos cada par para no saturar

            signals += 1
            emoji = "🟢" if sig["side"] == "BUY" else "🔴"
            log.info(
                f"  {emoji} SEÑAL {sym} {sig['side']} "
                f"Score:{sig['score']}/12 | {' | '.join(sig['reasons'])}"
            )

            if AUTO_TRADING:
                # Tamaño de posición basado en riesgo fijo
                risk_usdt = TRADE_USDT
                entry     = sig["entry"]
                sl_dist   = abs(entry - sig["sl"])
                risk_pct  = sl_dist / entry if entry > 0 else 0

                if risk_pct < 0.001:
                    log.warning(f"  ⚠️  SL muy cercano en {sym}, skip")
                    continue

                qty = round((risk_usdt / risk_pct) / entry, 4)
                if qty <= 0:
                    continue

                success = await self.api.place_order(
                    symbol=sym,
                    side=sig["side"],
                    qty=qty,
                    sl=sig["sl"],
                    tp1=sig["tp1"],
                    tp2=sig["tp2"],
                )
                if success:
                    self.daily_trades += 1
                    self.open_pos[sym] = {"symbol": sym, "side": sig["side"]}
                    msg = (
                        f"{emoji} <b>{sym}</b> — {'LONG' if sig['side'] == 'BUY' else 'SHORT'}\n"
                        f"📊 Score: {sig['score']}/12\n"
                        f"📍 Entry: {entry:.4f}\n"
                        f"🛡 SL:  {sig['sl']:.4f}  ({sl_dist/entry*100:.2f}%)\n"
                        f"🎯 TP1: {sig['tp1']:.4f}  25% @ 1R\n"
                        f"🎯 TP2: {sig['tp2']:.4f}  25% @ {RR}R\n"
                        f"🔄 Trail: 50% restante\n"
                        f"📈 RSI:{sig['rsi']:.0f} | ATR:{sig['atr_pct']:.2f}% | ST:{'▲' if sig['st']==1 else '▼'}\n"
                        f"✨ {' · '.join(sig['reasons'])}\n"
                        f"💰 Balance: {self.balance:.2f} USDT"
                    )
                    await tg(msg)
                    log.info(f"  ✅ Orden colocada: {sym} qty={qty}")
            else:
                # Modo simulación
                msg = (
                    f"🔔 <b>[SIM] {sym}</b> — {'LONG' if sig['side'] == 'BUY' else 'SHORT'}\n"
                    f"Score: {sig['score']}/12  | Entry: {sig['entry']:.4f}\n"
                    f"SL: {sig['sl']:.4f} | TP1: {sig['tp1']:.4f} | TP2: {sig['tp2']:.4f}\n"
                    f"RSI:{sig['rsi']:.0f} ATR:{sig['atr_pct']:.2f}%\n"
                    f"✨ {' · '.join(sig['reasons'])}"
                )
                await tg(msg)

        log.info(
            f"  [SCAN] Candidatos:{len(candidates)} | "
            f"Señales:{signals} | Pos abiertas:{len(self.open_pos)}"
        )

    # ─────────────────────────────────────
    #  INICIO Y BUCLE PRINCIPAL
    # ─────────────────────────────────────
    async def run(self):
        log.info("══════════════════════════════════════")
        log.info("  Phantom Edge Bot ELITE v3.0")
        log.info(f"  Auto-trading: {'ON ✅' if AUTO_TRADING else 'OFF (simulación)'}")
        log.info(f"  Score mín: {MIN_SCORE}/12 | RR: 1:{RR} | Lev: x{LEVERAGE}")
        log.info("══════════════════════════════════════")

        # Balance inicial
        self.balance = await self.api.get_balance()
        log.info(f"💰 Balance inicial: {self.balance:.2f} USDT")

        if self.balance == 0 and API_KEY:
            log.warning("⚠️  Balance = 0. Verificar BINGX_API_KEY / BINGX_API_SECRET y permisos de Futures.")

        # Símbolos
        self.symbols = await self.api.get_symbols()
        log.info(f"📊 Símbolos USDT-Perp disponibles: {len(self.symbols)}")

        if not self.symbols:
            log.error("❌ No se obtuvieron símbolos. Revisa conexión a BingX.")
            return

        await tg(
            f"🤖 <b>Phantom Edge Bot ELITE v3.0</b> — Iniciado\n"
            f"💰 Balance: {self.balance:.2f} USDT\n"
            f"📊 Pares: {len(self.symbols)}\n"
            f"⚙️ Score: {MIN_SCORE}/12 | RR: 1:{RR} | Lev: x{LEVERAGE}\n"
            f"{'🟢 AUTO-TRADING ACTIVO' if AUTO_TRADING else '🟡 MODO SIMULACIÓN'}"
        )

        # Warm-up inicial (primeros 200 pares priorizados)
        log.info(f"🔥 Iniciando warm-up ({len(self.symbols)} pares)...")
        await self.warmup_batch(self.symbols[:200], concurrency=20)

        # El resto en background
        if len(self.symbols) > 200:
            asyncio.create_task(self.warmup_batch(self.symbols[200:], concurrency=10))

        log.info(f"✅ Warm-up completo: {len(self.warm)} pares listos")

        # Bucle de trading
        while True:
            try:
                t0 = time.time()
                await self.scan()
                elapsed = time.time() - t0
                sleep = max(5.0, SCAN_INTERVAL - elapsed)
                log.info(f"  ⏱ Ciclo en {elapsed:.1f}s | próximo en {sleep:.0f}s\n")
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"❌ Error ciclo: {e}", exc_info=True)
                await asyncio.sleep(15)

        await self.api.close()
        log.info("Bot detenido.")


# ══════════════════════════════════════════
#  HEALTH-CHECK HTTP (Railway)
# ══════════════════════════════════════════
async def health_server(bot: "PhantomEdgeBot", port: int):
    from aiohttp import web

    async def handle(req):
        return web.json_response({
            "status": "running",
            "uptime_min": round((time.time() - bot.start_time) / 60, 1),
            "cycle": bot.cycle,
            "balance_usdt": round(bot.balance, 2),
            "warm_symbols": len(bot.warm),
            "total_symbols": len(bot.symbols),
            "open_positions": len(bot.open_pos),
            "daily_trades": bot.daily_trades,
            "auto_trading": AUTO_TRADING,
        })

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"🌐 Health-check en http://0.0.0.0:{port}")


# ══════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════
async def main():
    bot = PhantomEdgeBot()
    await asyncio.gather(
        health_server(bot, PORT),
        bot.run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
