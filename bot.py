"""
FibStruct Bot — BingX Perpetual Futures
Runs FibStruct strategy on live candles and executes orders via BingX API.
Deploy on Railway. All config via environment variables.
"""

import os
import time
import hmac
import hashlib
import logging
import threading
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlencode
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import pandas as pd

from strategy import StrategyParams, SignalResult, compute

VERSION = "1.0.0"
BASE_URL = "https://open-api.bingx.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fibstruct")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
def _bool(key: str, default: str = "true") -> bool:
    return os.getenv(key, default).strip().lower() not in ("false", "0", "no")

API_KEY        = os.getenv("BINGX_API_KEY", "")
SECRET         = os.getenv("BINGX_SECRET_KEY", "")
SYMBOL         = os.getenv("SYMBOL", "BTC-USDT")
INTERVAL       = os.getenv("INTERVAL", "15m")
LEVERAGE       = int(os.getenv("LEVERAGE", "10"))
USDT_PER_TRADE = float(os.getenv("USDT_PER_TRADE", "50"))
ATR_SL_MULT    = float(os.getenv("ATR_SL_MULT", "1.5"))
ATR_TP_MULT    = float(os.getenv("ATR_TP_MULT", "2.5"))
DRY_RUN        = _bool("DRY_RUN", "true")
MAX_BARS       = int(os.getenv("MAX_BARS", "300"))
PRICE_PREC     = int(os.getenv("PRICE_PRECISION", "2"))
QTY_PREC       = int(os.getenv("QTY_PRECISION", "3"))
TG_TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT        = os.getenv("TELEGRAM_CHAT_ID", "")

# HEDGE o ONEWAY -- consulta tu cuenta: app BingX > Futuros > Preferencias >
# Modo de posicion. El bot original asumia siempre ONEWAY (positionSide=BOTH).
# Si tu cuenta esta en HEDGE (como en tu otro bot), las ordenes fallaban.
POSITION_MODE  = os.getenv("POSITION_MODE", "ONEWAY").upper()

# ── Escaneo de todo el exchange ──
# false (por defecto) = solo SYMBOL, comportamiento original sin cambios.
# true = ignora SYMBOL, escanea todos los perpetuos USDT-M activos.
SCAN_ALL_SYMBOLS      = _bool("SCAN_ALL_SYMBOLS", "false")
QUOTE_ASSET           = os.getenv("QUOTE_ASSET", "USDT")
SYMBOL_BLACKLIST      = {s.strip().upper() for s in os.getenv("SYMBOL_BLACKLIST", "").split(",") if s.strip()}
MIN_24H_VOLUME_USDT   = float(os.getenv("MIN_24H_VOLUME_USDT", "0"))
MAX_WORKERS           = int(os.getenv("MAX_WORKERS", "15"))
SYMBOL_REFRESH_CYCLES = int(os.getenv("SYMBOL_REFRESH_CYCLES", "20"))

# Frenos obligatorios en cuanto se pasa de 1 simbolo a "todos". Sin esto,
# cualquier ciclo con varias señales a la vez abriria una posicion por
# cada una, sin limite.
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "5"))
MAX_TRADES_PER_DAY       = int(os.getenv("MAX_TRADES_PER_DAY", "8"))

PARAMS = StrategyParams(
    swing_len     = int(os.getenv("SWING_LEN", "10")),
    atr_filter    = _bool("ATR_FILTER", "true"),
    atr_mult      = float(os.getenv("ATR_MULT", "0.5")),
    cooldown      = int(os.getenv("COOLDOWN", "5")),
    eq_tol        = float(os.getenv("EQ_TOL", "0.1")),
    conf_tol      = float(os.getenv("CONF_TOL", "0.3")),
    strict_engulf = _bool("STRICT_ENGULF", "true"),
    sweep_boost   = _bool("SWEEP_BOOST", "true"),
)

INTERVAL_SECS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400,
}


def _position_side(entry_side: str) -> str:
    """entry_side = 'BUY' o 'SELL', la direccion de la posicion (no de la
    orden concreta -- para cerrar, entry_side sigue siendo el de apertura)."""
    if POSITION_MODE == "HEDGE":
        return "LONG" if entry_side == "BUY" else "SHORT"
    return "BOTH"

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def tg(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        log.warning(f"Telegram: {exc}")


# ─────────────────────────────────────────────
# BINGX CLIENT
# ─────────────────────────────────────────────
class BingXClient:
    def __init__(self, api_key: str, secret: str):
        self.api_key = api_key
        self.secret  = secret
        self.session = requests.Session()
        self.session.headers.update({"X-BX-APIKEY": self.api_key})

    # ── signing ──────────────────────────────
    def _sign(self, params: dict) -> str:
        params["timestamp"]  = int(time.time() * 1000)
        params["recvWindow"] = 5000
        qs  = urlencode(sorted(params.items()))
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        p  = params or {}
        qs = self._sign(p)
        r  = self.session.get(f"{BASE_URL}{path}?{qs}", timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, params: Optional[dict] = None) -> dict:
        # FIX: BingX espera la query firmada en el BODY para POST
        # (application/x-www-form-urlencoded), no en la URL como GET/DELETE.
        # Confirmado contra la referencia oficial de BingX para agentes de
        # IA (github.com/BingX-API/api-ai-skills). Con params= (como estaba)
        # `requests` la manda como query string de la URL y el body llega
        # vacío -- no fallaba porque DRY_RUN nunca llega a llamar a esto.
        p  = params or {}
        qs = self._sign(p)
        r  = self.session.post(
            f"{BASE_URL}{path}", data=qs,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str, params: Optional[dict] = None) -> dict:
        p  = params or {}
        qs = self._sign(p)
        r  = self.session.delete(f"{BASE_URL}{path}?{qs}", timeout=15)
        r.raise_for_status()
        return r.json()

    # ── market data ──────────────────────────
    def get_symbols(self) -> list:
        """Lista de simbolos USDT-M activos, filtrados por blacklist y
        volumen minimo. Sin firma (endpoint publico)."""
        try:
            data = self._get("/openApi/swap/v2/quote/contracts")
        except Exception as exc:
            log.error(f"get_symbols: {exc}")
            return []
        rows = data.get("data", [])
        out = []
        for c in rows:
            sym = c.get("symbol", "")
            if not sym.upper().endswith("-" + QUOTE_ASSET):
                continue
            if sym.upper() in SYMBOL_BLACKLIST:
                continue
            status = c.get("status", c.get("apiStateOpen"))
            if status is not None and str(status).upper() in ("0", "FALSE", "OFFLINE", "DELISTED", "PAUSED"):
                continue
            if MIN_24H_VOLUME_USDT > 0:
                vol = float(c.get("quoteVolume24h", c.get("volume24h", 0)) or 0)
                if vol < MIN_24H_VOLUME_USDT:
                    continue
            out.append(sym)
        return out

    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        data = self._get("/openApi/swap/v2/quote/klines", {
            "symbol": symbol, "interval": interval, "limit": limit,
        })
        rows = data.get("data", [])
        if not rows:
            return pd.DataFrame()
        # BingX returns list of dicts: {open, high, low, close, volume, time}
        df = pd.DataFrame(rows)
        # normalise column names (handle both camel and snake cases)
        df.columns = [c.lower() for c in df.columns]
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        time_col = next((c for c in df.columns if "time" in c), None)
        if time_col:
            df.sort_values(time_col, inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    # ── account ──────────────────────────────
    def get_balance(self) -> float:
        # FIX: la respuesta real de BingX trae "balance" como un DICT
        # único {"asset": "USDT", "balance": "50.0000", ...}, no como
        # lista. Confirmado contra una respuesta real de BingX en esta
        # misma conversación. Iterar el dict como si fuera una lista de
        # dicts fallaba en silencio (0.0 siempre) o reventaba con
        # AttributeError al primer .get() sobre una clave-string.
        try:
            data = self._get("/openApi/swap/v2/user/balance")
            bal = data.get("data", {}).get("balance", {})
            if isinstance(bal, dict) and bal.get("asset") == "USDT":
                return float(bal.get("balance", 0))
            if isinstance(bal, list):  # por si BingX cambia el formato
                for b in bal:
                    if isinstance(b, dict) and b.get("asset") == "USDT":
                        return float(b.get("balance", 0))
        except Exception as exc:
            log.warning(f"get_balance: {exc}")
        return 0.0

    def get_all_positions(self) -> list:
        """Todas las posiciones abiertas de la cuenta, sin filtrar por
        simbolo -- para reconciliar el estado real al empezar cada ciclo
        en vez de fiarse solo de lo que el bot recuerda en memoria."""
        try:
            data = self._get("/openApi/swap/v2/user/positions")
            return [p for p in data.get("data", []) if abs(float(p.get("positionAmt", 0) or 0)) > 0]
        except Exception as exc:
            log.warning(f"get_all_positions: {exc}")
            return []

    def get_position(self, symbol: str) -> Optional[dict]:
        try:
            data = self._get("/openApi/swap/v2/user/positions", {"symbol": symbol})
            for pos in data.get("data", []):
                if abs(float(pos.get("positionAmt", 0))) > 0:
                    return pos
        except Exception as exc:
            log.warning(f"get_position: {exc}")
        return None

    # ── setup ────────────────────────────────
    def set_leverage(self, symbol: str, leverage: int) -> None:
        for side in ("LONG", "SHORT"):
            try:
                self._post("/openApi/swap/v2/trade/leverage", {
                    "symbol": symbol, "side": side, "leverage": leverage,
                })
            except Exception as exc:
                log.warning(f"set_leverage {side}: {exc}")

    def cancel_all(self, symbol: str) -> None:
        try:
            self._delete("/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol})
            log.info(f"Cancelled all open orders — {symbol}")
        except Exception as exc:
            log.warning(f"cancel_all: {exc}")

    # ── orders ───────────────────────────────
    def market_order(self, symbol: str, side: str, qty: float) -> dict:
        return self._post("/openApi/swap/v2/trade/order", {
            "symbol":       symbol,
            "side":         side,       # BUY | SELL
            "positionSide": _position_side(side),
            "type":         "MARKET",
            "quantity":     round(qty, QTY_PREC),
        })

    def stop_market(self, symbol: str, side: str, stop_price: float, entry_side: str) -> dict:
        """STOP_MARKET para cerrar posicion (SL). entry_side = direccion
        ORIGINAL de la posicion (no `side`, que es el lado de esta orden)."""
        params = {
            "symbol":        symbol,
            "side":          side,
            "positionSide":  _position_side(entry_side),
            "type":          "STOP_MARKET",
            "stopPrice":     round(stop_price, PRICE_PREC),
        }
        # closePosition/reduceOnly no son compatibles con HEDGE -- ahi el
        # cierre lo define la combinacion side+positionSide por si sola.
        if POSITION_MODE != "HEDGE":
            params["closePosition"] = "true"
        return self._post("/openApi/swap/v2/trade/order", params)

    def take_profit_market(self, symbol: str, side: str, tp_price: float, entry_side: str) -> dict:
        """TAKE_PROFIT_MARKET para cerrar posicion (TP)."""
        params = {
            "symbol":        symbol,
            "side":          side,
            "positionSide":  _position_side(entry_side),
            "type":          "TAKE_PROFIT_MARKET",
            "stopPrice":     round(tp_price, PRICE_PREC),
        }
        if POSITION_MODE != "HEDGE":
            params["closePosition"] = "true"
        return self._post("/openApi/swap/v2/trade/order", params)

    def close_position(self, symbol: str, pos: dict) -> dict:
        amt        = abs(float(pos.get("positionAmt", 0)))
        is_long    = float(pos.get("positionAmt", 0)) > 0
        side       = "SELL" if is_long else "BUY"
        entry_side = "BUY" if is_long else "SELL"
        params = {
            "symbol":       symbol,
            "side":         side,
            "positionSide": _position_side(entry_side),
            "type":         "MARKET",
            "quantity":     round(amt, QTY_PREC),
        }
        if POSITION_MODE != "HEDGE":
            params["reduceOnly"] = "true"
        return self._post("/openApi/swap/v2/trade/order", params)


# ─────────────────────────────────────────────
# BOT STATE
# ─────────────────────────────────────────────
client = BingXClient(API_KEY, SECRET)
state  = {
    "cycles":           0,
    "last_signal":      "—",
    "last_cycle":        "—",
    "open_positions":   0,
    "symbols_scanned":  0,
    "trades_today":     0,
    "errors":           0,
    "paper_open":       0,
    "paper_wins":       0,
    "paper_losses":     0,
}
_trades_today_date: Optional[str] = None
_symbol_cache: list = []
_symbol_cache_cycle: int = -999
_paper_positions: dict = {}  # symbol -> {direction, entry, sl, tp, opened_at, trigger}


def _paper_winrate() -> float:
    total = state["paper_wins"] + state["paper_losses"]
    return (state["paper_wins"] / total * 100.0) if total else 0.0


def _manage_paper_positions() -> None:
    """Sin esto, DRY_RUN manda la señal por Telegram y la olvida --
    nunca se sabe si el SL o el TP se habrian tocado. Mismo problema
    encontrado y arreglado en bingx-ict-scanner (v1.3.0): sin cerrar
    las posiciones de papel, tampoco hay forma de decir si el bot es
    rentable, solo cuantas señales dispara."""
    if not DRY_RUN or not _paper_positions:
        return
    for sym in list(_paper_positions.keys()):
        p = _paper_positions[sym]
        try:
            df = client.get_klines(sym, INTERVAL, 2)
        except Exception as exc:
            log.debug(f"{sym}: fallo trayendo precio para posicion de papel ({exc})")
            continue
        if df.empty:
            continue
        price = float(df["close"].iloc[-1])
        is_long = p["direction"] == "LONG"
        hit_tp = price >= p["tp"] if is_long else price <= p["tp"]
        hit_sl = price <= p["sl"] if is_long else price >= p["sl"]
        if not (hit_tp or hit_sl):
            continue
        win = hit_tp and not hit_sl
        state["paper_wins" if win else "paper_losses"] += 1
        state["paper_open"] = len(_paper_positions) - 1
        elapsed_min = (time.time() * 1000 - p["opened_at"]) / 60000
        icon = "✅" if win else "❌"
        log.info(f"{sym}: posicion de papel cerrada por {'TP' if win else 'SL'} ({elapsed_min:.0f} min) | Racha: {state['paper_wins']}W/{state['paper_losses']}L")
        tg(
            f"{icon} <b>[Papel] Cierre {'TP' if win else 'SL'}</b> — {sym}\n"
            f"{p['direction']} desde {p['entry']} | {elapsed_min:.0f} min | "
            f"Racha: {state['paper_wins']}W/{state['paper_losses']}L ({_paper_winrate():.0f}%)"
        )
        del _paper_positions[sym]


def _reset_daily_counter_if_new_day() -> None:
    global _trades_today_date
    today = time.strftime("%Y-%m-%d")
    if _trades_today_date != today:
        _trades_today_date = today
        state["trades_today"] = 0


def get_symbol_universe() -> list:
    """[SYMBOL] en modo single-simbolo. Lista completa (cacheada, refrescada
    cada SYMBOL_REFRESH_CYCLES ciclos) en modo SCAN_ALL_SYMBOLS."""
    global _symbol_cache, _symbol_cache_cycle
    if not SCAN_ALL_SYMBOLS:
        return [SYMBOL]
    if _symbol_cache and (state["cycles"] - _symbol_cache_cycle) < SYMBOL_REFRESH_CYCLES:
        return _symbol_cache
    syms = client.get_symbols()
    if syms:
        _symbol_cache = syms
        _symbol_cache_cycle = state["cycles"]
        log.info(f"Universo actualizado: {len(syms)} simbolos")
    return _symbol_cache or [SYMBOL]


# ─────────────────────────────────────────────
# EVALUACIÓN POR SÍMBOLO (funcion pura, segura para correr en threads --
# no toca `state`, no llama a Telegram, no coloca ordenes)
# ─────────────────────────────────────────────
def evaluate_symbol(sym: str) -> Optional[dict]:
    try:
        df = client.get_klines(sym, INTERVAL, MAX_BARS)
    except Exception as exc:
        log.debug(f"{sym}: fallo trayendo velas ({exc})")
        return None
    if df.empty or len(df) < 80:
        return None
    df = df.iloc[:-1].reset_index(drop=True)  # descarta la vela en curso

    try:
        sig: SignalResult = compute(df, PARAMS)
    except Exception as exc:
        log.warning(f"{sym}: fallo evaluando la estrategia ({exc})")
        return None

    if not SCAN_ALL_SYMBOLS:
        trend = {1: "▲ Bull", -1: "▼ Bear", 0: "─ Neutral"}[sig.structure_bias]
        zone  = "PREMIUM" if sig.in_premium else ("DISCOUNT" if sig.in_discount else "MID")
        fdir  = {1: "Long↑", -1: "Short↓", 0: "—"}[sig.fib_direction]
        log.info(
            f"[{sym}] {trend} | Fib:{fdir} | Zone:{zone} | "
            f"Conf:{sig.conf_score:.0f} | ATR:{sig.atr:.5f} | "
            f"BOS:{sig.is_bos} CHoCH:{sig.is_choch}"
        )

    if not sig.confirmed_buy and not sig.confirmed_sell:
        return None

    direction = "LONG" if sig.confirmed_buy else "SHORT"
    trigger   = sig.buy_trigger if sig.confirmed_buy else sig.sell_trigger
    entry     = sig.close
    atr       = sig.atr
    if atr <= 0 or entry <= 0:
        return None
    rr = ATR_TP_MULT / ATR_SL_MULT

    if sig.confirmed_buy:
        sl_price = round(entry - atr * ATR_SL_MULT, PRICE_PREC)
        tp_price = round(entry + atr * ATR_TP_MULT, PRICE_PREC)
        order_side, close_side = "BUY", "SELL"
    else:
        sl_price = round(entry + atr * ATR_SL_MULT, PRICE_PREC)
        tp_price = round(entry - atr * ATR_TP_MULT, PRICE_PREC)
        order_side, close_side = "SELL", "BUY"

    qty = round((USDT_PER_TRADE * LEVERAGE) / entry, QTY_PREC)

    return {
        "symbol": sym, "direction": direction, "trigger": trigger,
        "entry": entry, "sl_price": sl_price, "tp_price": tp_price,
        "rr": rr, "qty": qty, "order_side": order_side, "close_side": close_side,
        "conf_score": sig.conf_score, "atr": atr,
        "zone": "PREMIUM" if sig.in_premium else ("DISCOUNT" if sig.in_discount else "MID"),
        "fdir": {1: "Long↑", -1: "Short↓", 0: "—"}[sig.fib_direction],
    }


def _dispatch_signal(s: dict, open_symbols: set) -> None:
    """Notifica y (si no es DRY_RUN) ejecuta una señal ya evaluada.
    Aplica los limites de posiciones concurrentes y trades/dia."""
    sym = s["symbol"]

    if sym in open_symbols or sym in _paper_positions:
        log.info(f"{sym}: ya hay posicion abierta, se omite")
        return
    open_count = len(_paper_positions) if DRY_RUN else state["open_positions"]
    if open_count >= MAX_CONCURRENT_POSITIONS:
        log.info(f"{sym}: limite de posiciones concurrentes alcanzado, se omite")
        return
    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        log.info(f"{sym}: limite de trades/dia alcanzado, se omite")
        return

    log.info(
        f"SIGNAL {s['direction']} {sym} [{s['trigger']}] | "
        f"Entry:{s['entry']} SL:{s['sl_price']} TP:{s['tp_price']} "
        f"RR:{s['rr']:.1f}x Qty:{s['qty']}"
    )
    state["last_signal"] = f"{sym} {s['direction']}@{s['entry']:.4f}"

    if DRY_RUN:
        msg = "\n".join([
            f"🔵 <b>[DRY] {s['direction']} {sym}</b>",
            f"Trigger: <code>{s['trigger']}</code>",
            f"Entry: <code>{s['entry']}</code>",
            f"SL: <code>{s['sl_price']}</code>  TP: <code>{s['tp_price']}</code>",
            f"RR: {s['rr']:.1f}x  |  Conf: {s['conf_score']:.0f}",
            f"Zone: {s['zone']}  |  Fib dir: {s['fdir']}  |  ATR: {s['atr']:.5f}",
        ])
        tg(msg)
        state["trades_today"] += 1  # cuenta igual en DRY_RUN, para probar el limite tal cual se comportara en real
        _paper_positions[sym] = {
            "direction": s["direction"], "entry": s["entry"],
            "sl": s["sl_price"], "tp": s["tp_price"],
            "trigger": s["trigger"], "opened_at": time.time() * 1000,
        }
        state["paper_open"] = len(_paper_positions)
        return

    try:
        if not SCAN_ALL_SYMBOLS:
            pass  # leverage ya fijado una vez al arrancar para el simbolo unico
        else:
            client.set_leverage(sym, LEVERAGE)
        resp = client.market_order(sym, s["order_side"], s["qty"])
        log.info(f"Entry order: {resp}")
        time.sleep(0.8)

        try:
            client.stop_market(sym, s["close_side"], s["sl_price"], s["order_side"])
            log.info(f"SL placed @ {s['sl_price']}")
        except Exception as exc:
            log.error(f"SL placement failed: {exc}")

        try:
            client.take_profit_market(sym, s["close_side"], s["tp_price"], s["order_side"])
            log.info(f"TP placed @ {s['tp_price']}")
        except Exception as exc:
            log.error(f"TP placement failed: {exc}")

        state["trades_today"] += 1
        state["open_positions"] += 1
        open_symbols.add(sym)
        emoji = "🟢" if s["direction"] == "LONG" else "🔴"
        msg = "\n".join([
            f"{emoji} <b>{s['direction']} {sym}</b>",
            f"Trigger: <code>{s['trigger']}</code>",
            f"Entry: <code>{s['entry']}</code>",
            f"SL: <code>{s['sl_price']}</code>  TP: <code>{s['tp_price']}</code>",
            f"RR: {s['rr']:.1f}x  |  Conf: {s['conf_score']:.0f}",
            f"Zone: {s['zone']}  |  ATR: {s['atr']:.5f}",
        ])
        tg(msg)
    except Exception as exc:
        state["errors"] += 1
        log.error(f"Order execution failed for {sym}: {exc}", exc_info=True)
        tg(f"⚠️ <b>FibStruct Order Error</b>\n{sym} | {exc}")

    time.sleep(1.2)  # espacia los envios a Telegram/BingX si hay varias señales seguidas


# ─────────────────────────────────────────────
# MAIN CYCLE
# ─────────────────────────────────────────────
def run_cycle() -> None:
    state["cycles"] += 1
    _reset_daily_counter_if_new_day()
    t0 = time.time()

    symbols = get_symbol_universe()
    state["symbols_scanned"] = len(symbols)

    # Reconcilia contra BingX real en vez de fiarse solo de la memoria del bot.
    open_symbols: set = set()
    if not DRY_RUN:
        positions = client.get_all_positions()
        open_symbols = {p.get("symbol") for p in positions}
        state["open_positions"] = len(open_symbols)
    else:
        _manage_paper_positions()

    signals: list = []
    if len(symbols) == 1:
        r = evaluate_symbol(symbols[0])
        if r:
            signals.append(r)
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(evaluate_symbol, s): s for s in symbols}
            for fut in as_completed(futures):
                try:
                    r = fut.result()
                except Exception as exc:
                    log.warning(f"{futures[fut]}: error en el hilo ({exc})")
                    continue
                if r:
                    signals.append(r)

    state["last_cycle"] = time.strftime("%H:%M:%S")
    elapsed = time.time() - t0
    open_now = state['paper_open'] if DRY_RUN else state['open_positions']
    log.info(
        f"Ciclo completo: {len(symbols)} simbolos, {len(signals)} señales, "
        f"{open_now} posiciones abiertas, {elapsed:.1f}s"
    )
    if DRY_RUN:
        wl_total = state["paper_wins"] + state["paper_losses"]
        log.info(
            f"Papel: {state['paper_open']} abiertas | "
            f"{state['paper_wins']}W/{state['paper_losses']}L "
            f"({_paper_winrate():.0f}% de {wl_total} cerradas)"
        )

    for s in signals:
        _dispatch_signal(s, open_symbols)


# ─────────────────────────────────────────────
# HEALTH SERVER
# ─────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({
            "status":           "ok",
            "version":          VERSION,
            "scan_all_symbols": SCAN_ALL_SYMBOLS,
            "symbol":           SYMBOL if not SCAN_ALL_SYMBOLS else None,
            "interval":         INTERVAL,
            "dry_run":          DRY_RUN,
            "cycles":           state["cycles"],
            "errors":           state["errors"],
            "last_signal":      state["last_signal"],
            "last_cycle":       state["last_cycle"],
            "symbols_scanned":  state["symbols_scanned"],
            "open_positions":   state["open_positions"],
            "trades_today":     state["trades_today"],
            "paper_open":       state["paper_open"],
            "paper_wins":       state["paper_wins"],
            "paper_losses":     state["paper_losses"],
            "paper_winrate":    round(_paper_winrate(), 1),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_) -> None:
        pass


def start_health() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("", port), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"Health server → port {port}")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    scope = "TODOS los USDT-M activos" if SCAN_ALL_SYMBOLS else SYMBOL
    log.info(f"CODE_VERSION={VERSION} | {scope} | {INTERVAL}")
    log.info("╔══════════════════════════════╗")
    log.info(f"║  FibStruct Bot  v{VERSION}       ║")
    log.info("╚══════════════════════════════╝")
    log.info(f"Alcance  : {scope}  |  Interval : {INTERVAL}")
    log.info(f"Leverage : {LEVERAGE}x  |  USDT/trade: {USDT_PER_TRADE}")
    log.info(f"SL mult  : {ATR_SL_MULT}  |  TP mult: {ATR_TP_MULT}")
    log.info(f"MaxPos   : {MAX_CONCURRENT_POSITIONS}  |  MaxTrades/dia: {MAX_TRADES_PER_DAY}")
    log.info(f"DRY_RUN  : {DRY_RUN}  |  POSITION_MODE: {POSITION_MODE}")
    log.info(f"Params   : {PARAMS}")

    start_health()

    if not DRY_RUN:
        if not API_KEY or not SECRET:
            raise RuntimeError("BINGX_API_KEY / BINGX_SECRET_KEY not set")
        if not SCAN_ALL_SYMBOLS:
            # En escaneo total, el leverage se fija por simbolo justo antes
            # de cada orden -- no tiene sentido fijarlo para 500+ pares aqui.
            client.set_leverage(SYMBOL, LEVERAGE)
            client.cancel_all(SYMBOL)
        tg(
            f"🚀 <b>FibStruct Bot v{VERSION} — LIVE</b>\n"
            f"{scope} | {INTERVAL} | {LEVERAGE}x | SL:{ATR_SL_MULT}× TP:{ATR_TP_MULT}× | "
            f"Max {MAX_CONCURRENT_POSITIONS} pos · {MAX_TRADES_PER_DAY} trades/día"
        )
    else:
        tg(
            f"🔵 <b>FibStruct Bot v{VERSION} — DRY RUN</b>\n"
            f"{scope} | {INTERVAL}"
        )

    interval_secs = INTERVAL_SECS.get(INTERVAL, 900)

    while True:
        try:
            run_cycle()
        except Exception as exc:
            state["errors"] += 1
            log.error(f"Cycle error: {exc}", exc_info=True)
            tg(f"⚠️ <b>FibStruct Cycle Error</b>\n{exc}")

        # align sleep to next candle close + 5s buffer
        now        = time.time()
        next_close = ((now // interval_secs) + 1) * interval_secs + 5
        sleep_secs = max(next_close - time.time(), 15)
        log.info(f"Next cycle in {sleep_secs:.0f}s")
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
