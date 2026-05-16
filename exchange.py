"""exchange.py — Cliente BingX Perpetual Futures (CCXT)"""
import logging
import ccxt
import pandas as pd
from config import (
    BINGX_API_KEY, BINGX_API_SECRET,
    SYMBOL, TIMEFRAME, CANDLES_LIMIT,
    LEVERAGE, MODE, RISK_PCT,
    ATR_MULT_TP, ATR_MULT_SL,
)

log = logging.getLogger("exchange")


def build_exchange() -> ccxt.bingx:
    ex = ccxt.bingx({
        "apiKey": BINGX_API_KEY,
        "secret": BINGX_API_SECRET,
        "options": {"defaultType": "swap"},
    })
    ex.load_markets()
    if LEVERAGE > 1 and MODE == "live":
        try:
            ex.set_leverage(LEVERAGE, SYMBOL)
            log.info(f"Apalancamiento establecido: {LEVERAGE}x")
        except Exception as e:
            log.warning(f"No se pudo setear apalancamiento: {e}")
    return ex


def fetch_ohlcv(ex: ccxt.bingx, symbol=SYMBOL,
                timeframe=TIMEFRAME, limit=CANDLES_LIMIT) -> pd.DataFrame:
    raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df.astype(float)


def fetch_funding_rate(ex: ccxt.bingx, symbol=SYMBOL) -> float:
    """Devuelve el funding rate actual como decimal (ej: 0.0001 = 0.01%)."""
    try:
        info = ex.fetch_funding_rate(symbol)
        rate = info.get("fundingRate", 0.0)
        return float(rate) if rate is not None else 0.0
    except Exception as e:
        log.warning(f"No se pudo obtener funding rate: {e}")
        return 0.0


def get_balance(ex: ccxt.bingx) -> float:
    try:
        bal = ex.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0.0))
    except Exception as e:
        log.error(f"Error obteniendo balance: {e}")
        return 0.0


def calc_qty(balance: float, price: float, atr: float) -> float:
    risk_usdt = balance * (RISK_PCT / 100) * LEVERAGE
    sl_dist   = atr * ATR_MULT_SL
    if sl_dist <= 0 or price <= 0:
        return 0.0
    return round(risk_usdt / sl_dist, 4)


def open_long(ex, symbol, qty, price, atr) -> dict | None:
    tp = round(price + atr * ATR_MULT_TP, 4)
    sl = round(price - atr * ATR_MULT_SL, 4)
    if MODE == "paper":
        log.info(f"[PAPER] LONG {qty} {symbol} @ {price:.4f} | TP {tp} | SL {sl}")
        return {"id": "paper_long", "price": price, "qty": qty, "tp": tp, "sl": sl}
    try:
        order = ex.create_order(symbol, "market", "buy", qty, params={
            "takeProfit": {"triggerPrice": tp},
            "stopLoss":   {"triggerPrice": sl},
        })
        log.info(f"LONG abierto: {order['id']} | TP {tp} | SL {sl}")
        return order
    except Exception as e:
        log.error(f"Error abriendo LONG: {e}")
        return None


def open_short(ex, symbol, qty, price, atr) -> dict | None:
    tp = round(price - atr * ATR_MULT_TP, 4)
    sl = round(price + atr * ATR_MULT_SL, 4)
    if MODE == "paper":
        log.info(f"[PAPER] SHORT {qty} {symbol} @ {price:.4f} | TP {tp} | SL {sl}")
        return {"id": "paper_short", "price": price, "qty": qty, "tp": tp, "sl": sl}
    try:
        order = ex.create_order(symbol, "market", "sell", qty, params={
            "takeProfit": {"triggerPrice": tp},
            "stopLoss":   {"triggerPrice": sl},
        })
        log.info(f"SHORT abierto: {order['id']} | TP {tp} | SL {sl}")
        return order
    except Exception as e:
        log.error(f"Error abriendo SHORT: {e}")
        return None


def close_position(ex, symbol) -> bool:
    if MODE == "paper":
        log.info(f"[PAPER] Cierre posición {symbol}")
        return True
    try:
        pos = ex.fetch_positions([symbol])
        for p in pos:
            if p.get("contracts") and float(p["contracts"]) > 0:
                side = "sell" if p["side"] == "long" else "buy"
                ex.create_order(symbol, "market", side,
                                abs(float(p["contracts"])),
                                params={"reduceOnly": True})
        return True
    except Exception as e:
        log.error(f"Error cerrando posición: {e}")
        return False


def get_open_position(ex, symbol) -> dict | None:
    if MODE == "paper":
        return None
    try:
        pos = ex.fetch_positions([symbol])
        for p in pos:
            if p.get("contracts") and float(p["contracts"]) > 0:
                return p
        return None
    except Exception as e:
        log.error(f"Error obteniendo posición: {e}")
        return None
