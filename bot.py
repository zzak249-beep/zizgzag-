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
    "cycles":      0,
    "last_signal": "—",
    "last_cycle":  "—",
    "position":    "none",
    "errors":      0,
}


# ─────────────────────────────────────────────
# MAIN CYCLE
# ─────────────────────────────────────────────
def run_cycle() -> None:
    state["cycles"] += 1

    # 1. Fetch candles (drop last — may be incomplete)
    df = client.get_klines(SYMBOL, INTERVAL, MAX_BARS)
    if df.empty or len(df) < 80:
        log.warning(f"Insufficient data: {len(df)} bars")
        return
    df = df.iloc[:-1].reset_index(drop=True)

    # 2. Compute strategy on closed bars
    sig: SignalResult = compute(df, PARAMS)

    trend = {1: "▲ Bull", -1: "▼ Bear", 0: "─ Neutral"}[sig.structure_bias]
    zone  = "PREMIUM" if sig.in_premium else ("DISCOUNT" if sig.in_discount else "MID")
    fdir  = {1: "Long↑", -1: "Short↓", 0: "—"}[sig.fib_direction]
    state["last_cycle"] = time.strftime("%H:%M:%S")

    log.info(
        f"[{SYMBOL}] {trend} | Fib:{fdir} | Zone:{zone} | "
        f"Conf:{sig.conf_score:.0f} | ATR:{sig.atr:.5f} | "
        f"BOS:{sig.is_bos} CHoCH:{sig.is_choch}"
    )

    if not sig.confirmed_buy and not sig.confirmed_sell:
        return

    direction = "LONG" if sig.confirmed_buy else "SHORT"
    trigger   = sig.buy_trigger if sig.confirmed_buy else sig.sell_trigger
    entry     = sig.close
    atr       = sig.atr
    rr        = ATR_TP_MULT / ATR_SL_MULT

    if sig.confirmed_buy:
        sl_price = round(entry - atr * ATR_SL_MULT, PRICE_PREC)
        tp_price = round(entry + atr * ATR_TP_MULT, PRICE_PREC)
        order_side = "BUY";  close_side = "SELL"
    else:
        sl_price = round(entry + atr * ATR_SL_MULT, PRICE_PREC)
        tp_price = round(entry - atr * ATR_TP_MULT, PRICE_PREC)
        order_side = "SELL"; close_side = "BUY"

    qty = round((USDT_PER_TRADE * LEVERAGE) / entry, QTY_PREC)

    log.info(
        f"SIGNAL {direction} [{trigger}] | "
        f"Entry:{entry} SL:{sl_price} TP:{tp_price} RR:{rr:.1f}x Qty:{qty}"
    )
    state["last_signal"] = f"{direction}@{entry:.4f}"

    # ── DRY RUN ──
    if DRY_RUN:
        msg = "\n".join([
            f"🔵 <b>[DRY] {direction} {SYMBOL}</b>",
            f"Trigger: <code>{trigger}</code>",
            f"Entry: <code>{entry}</code>",
            f"SL: <code>{sl_price}</code>  TP: <code>{tp_price}</code>",
            f"RR: {rr:.1f}x  |  Conf: {sig.conf_score:.0f}",
            f"Zone: {zone}  |  Fib dir: {fdir}  |  ATR: {atr:.5f}",
        ])
        tg(msg)
        log.info("DRY_RUN — no order placed")
        return

    # ── LIVE ──
    # Check existing position first
    pos = client.get_position(SYMBOL)
    if pos:
        log.info(f"Position already open ({pos.get('positionAmt')}) — skip")
        return

    try:
        resp = client.market_order(SYMBOL, order_side, qty)
        log.info(f"Entry order: {resp}")
        time.sleep(0.8)

        try:
            client.stop_market(SYMBOL, close_side, sl_price, order_side)
            log.info(f"SL placed @ {sl_price}")
        except Exception as exc:
            log.error(f"SL placement failed: {exc}")

        try:
            client.take_profit_market(SYMBOL, close_side, tp_price, order_side)
            log.info(f"TP placed @ {tp_price}")
        except Exception as exc:
            log.error(f"TP placement failed: {exc}")

        state["position"] = direction
        emoji = "🟢" if sig.confirmed_buy else "🔴"
        msg = "\n".join([
            f"{emoji} <b>{direction} {SYMBOL}</b>",
            f"Trigger: <code>{trigger}</code>",
            f"Entry: <code>{entry}</code>",
            f"SL: <code>{sl_price}</code>  TP: <code>{tp_price}</code>",
            f"RR: {rr:.1f}x  |  Conf: {sig.conf_score:.0f}",
            f"Zone: {zone}  |  ATR: {atr:.5f}",
        ])
        tg(msg)

    except Exception as exc:
        state["errors"] += 1
        log.error(f"Order execution failed: {exc}", exc_info=True)
        tg(f"⚠️ <b>FibStruct Order Error</b>\n{SYMBOL} | {exc}")


# ─────────────────────────────────────────────
# HEALTH SERVER
# ─────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({
            "status":      "ok",
            "version":     VERSION,
            "symbol":      SYMBOL,
            "interval":    INTERVAL,
            "dry_run":     DRY_RUN,
            "cycles":      state["cycles"],
            "errors":      state["errors"],
            "last_signal": state["last_signal"],
            "last_cycle":  state["last_cycle"],
            "position":    state["position"],
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
    log.info(f"CODE_VERSION={VERSION} | {SYMBOL} | {INTERVAL}")
    log.info("╔══════════════════════════════╗")
    log.info(f"║  FibStruct Bot  v{VERSION}       ║")
    log.info("╚══════════════════════════════╝")
    log.info(f"Symbol   : {SYMBOL}  |  Interval : {INTERVAL}")
    log.info(f"Leverage : {LEVERAGE}x  |  USDT/trade: {USDT_PER_TRADE}")
    log.info(f"SL mult  : {ATR_SL_MULT}  |  TP mult: {ATR_TP_MULT}")
    log.info(f"DRY_RUN  : {DRY_RUN}")
    log.info(f"Params   : {PARAMS}")

    start_health()

    if not DRY_RUN:
        if not API_KEY or not SECRET:
            raise RuntimeError("BINGX_API_KEY / BINGX_SECRET_KEY not set")
        client.set_leverage(SYMBOL, LEVERAGE)
        client.cancel_all(SYMBOL)
        tg(
            f"🚀 <b>FibStruct Bot v{VERSION} — LIVE</b>\n"
            f"{SYMBOL} | {INTERVAL} | {LEVERAGE}x | SL:{ATR_SL_MULT}× TP:{ATR_TP_MULT}×"
        )
    else:
        tg(
            f"🔵 <b>FibStruct Bot v{VERSION} — DRY RUN</b>\n"
            f"{SYMBOL} | {INTERVAL}"
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
