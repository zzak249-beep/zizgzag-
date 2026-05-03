import ccxt
import pandas as pd
import pandas_ta as ta
import time
import logging
import telegram_utils as tg
from config import (
    API_KEY, SECRET_KEY,
    TIMEFRAME, HMA_LENGTH, ZIGZAG_WINDOW,
    SCAN_INTERVAL, TOP_PAIRS_COUNT,
    ORDER_AMOUNT, LEVERAGE, SL_PCT, TP_PCT,
    LIVE_TRADING,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Exchange ───────────────────────────────────────────────────────────────────
exchange = ccxt.bingx({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'options': {'defaultType': 'swap'},
})


# ── Utilidades ─────────────────────────────────────────────────────────────────
def set_leverage(symbol: str) -> None:
    try:
        exchange.set_leverage(LEVERAGE, symbol)
    except Exception as e:
        log.warning(f"No se pudo ajustar leverage en {symbol}: {e}")


def get_top_pairs() -> list[str]:
    """Top N pares USDT perpetuo por volumen 24 h."""
    tickers = exchange.fetch_tickers()
    df = pd.DataFrame.from_dict(tickers, orient='index')
    df['quoteVolume'] = pd.to_numeric(df['quoteVolume'], errors='coerce')
    usdt = df[df.index.str.contains('/USDT:USDT')]
    pairs = usdt.nlargest(TOP_PAIRS_COUNT, 'quoteVolume').index.tolist()
    log.info(f"Pares seleccionados: {pairs}")
    return pairs


# ── Estrategia HMA + ZigZag ────────────────────────────────────────────────────
def strategy(symbol: str) -> tuple[str | None, float]:
    """Retorna (señal, precio_actual) o (None, precio)."""
    bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
    if not bars:
        return None, 0.0

    df = pd.DataFrame(bars, columns=['ts', 'o', 'h', 'l', 'c', 'v'])

    df['hma']    = ta.hma(df['c'], length=HMA_LENGTH)
    df['peak']   = df['h'].rolling(window=ZIGZAG_WINDOW, center=True).max()
    df['valley'] = df['l'].rolling(window=ZIGZAG_WINDOW, center=True).min()

    last_c      = df['c'].iloc[-1]
    last_hma    = df['hma'].iloc[-1]
    last_peak   = df['peak'].iloc[-5]
    last_valley = df['valley'].iloc[-5]

    if any(pd.isna(v) for v in [last_hma, last_peak, last_valley]):
        return None, last_c

    if last_c > last_peak and last_c > last_hma:
        return 'buy', last_c
    if last_c < last_valley and last_c < last_hma:
        return 'sell', last_c
    return None, last_c


# ── Ejecución de órdenes ───────────────────────────────────────────────────────
def place_order(symbol: str, side: str, price: float) -> None:
    sl = price * (1 - SL_PCT) if side == 'buy' else price * (1 + SL_PCT)
    tp = price * (1 + TP_PCT) if side == 'buy' else price * (1 - TP_PCT)

    if not LIVE_TRADING:
        log.info(f"[SIMULADO] {side.upper()} {symbol} | SL={sl:.4f} TP={tp:.4f}")
        tg.send(tg.signal_msg(symbol, side, price))
        return

    try:
        set_leverage(symbol)
        order = exchange.create_market_order(symbol, side, ORDER_AMOUNT)
        order_id = order.get('id', 'N/A')
        log.info(f"Orden ejecutada: {order_id}")

        # Stop Loss
        sl_side = 'sell' if side == 'buy' else 'buy'
        exchange.create_order(symbol, 'stop_market', sl_side, ORDER_AMOUNT,
                              params={'stopPrice': sl, 'reduceOnly': True})
        # Take Profit
        tp_side = sl_side
        exchange.create_order(symbol, 'take_profit_market', tp_side, ORDER_AMOUNT,
                              params={'stopPrice': tp, 'reduceOnly': True})

        tg.send(tg.order_msg(symbol, side, ORDER_AMOUNT, price, order_id))
        log.info(f"SL={sl:.4f}  TP={tp:.4f}")

    except ccxt.BaseError as e:
        log.error(f"Error en orden {symbol}: {e}")
        tg.send(tg.error_msg(f"Orden {symbol}", e))


# ── Loop principal ─────────────────────────────────────────────────────────────
def run_bot() -> None:
    mode = "🔴 LIVE" if LIVE_TRADING else "🟡 SIMULADO"
    log.info(f"BingX Bot iniciando — modo {mode}")

    pairs = get_top_pairs()
    tg.send(tg.startup_msg(pairs))

    while True:
        try:
            pairs = get_top_pairs()
            for symbol in pairs:
                try:
                    signal, price = strategy(symbol)
                    if signal:
                        log.info(f"Señal {signal.upper()} en {symbol} @ {price:.4f}")
                        place_order(symbol, signal, price)
                except Exception as e:
                    log.warning(f"Error en {symbol}: {e}")

            log.info(f"Esperando {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            tg.send("🛑 Bot detenido manualmente.")
            log.info("Bot detenido.")
            break
        except Exception as e:
            log.error(f"Error general: {e}")
            tg.send(tg.error_msg("Loop principal", e))
            time.sleep(30)


if __name__ == "__main__":
    run_bot()
