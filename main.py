"""main.py — Sniper Bot V50 Ultimate
V49 + Funding Rate + Liquidation Map
BingX Perpetual Futures · Railway · Telegram
"""
import time
import logging
import os
from datetime import datetime, date
from colorlog import ColoredFormatter
from config import (
    SYMBOL, TIMEFRAME, LOOP_INTERVAL, MODE,
    ATR_MULT_TP, ATR_MULT_SL, MAX_BARS_HOLD,
    MAX_TRADES_DAY, FUNDING_THRESHOLD,
)
from indicators import compute, MarkovEngine
from exchange import (
    build_exchange, fetch_ohlcv, fetch_funding_rate,
    get_balance, calc_qty, open_long, open_short,
    close_position, get_open_position,
)
import telegram_bot as tg


# ── Logger ────────────────────────────────────────────────────
def setup_logger():
    os.makedirs("logs", exist_ok=True)
    fmt_c = ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={"DEBUG":"cyan","INFO":"green","WARNING":"yellow","ERROR":"red"},
    )
    fmt_f = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(); sh.setFormatter(fmt_c)
    fh = logging.FileHandler("logs/bot.log"); fh.setFormatter(fmt_f)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(sh); logger.addHandler(fh)
    return logger

log = setup_logger()


# ── Estado ────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.position     = None     # "long" | "short" | None
        self.entry_price  = 0.0
        self.entry_qty    = 0.0
        self.entry_bar    = 0
        self.bar_count    = 0
        self.trades_today = 0
        self.pnl_day      = 0.0
        self.last_signal  = None
        self.last_hb_date = None
        self.last_fund_alert = None  # evita spam de alertas funding

state  = State()
markov = MarkovEngine()


# ── Tick principal ────────────────────────────────────────────
def tick(ex):
    state.bar_count += 1

    # 1. Datos OHLCV
    df = fetch_ohlcv(ex, SYMBOL, TIMEFRAME)
    if df.empty or len(df) < 210:
        log.warning("Pocas velas. Esperando historial...")
        return

    # 2. Funding rate
    funding = fetch_funding_rate(ex, SYMBOL)

    # 3. Indicadores V50
    ind = compute(df, markov, funding_rate=funding)

    log.info(
        f"[{SYMBOL}] {ind['close']:.4f} | "
        f"slope={ind['slope']:.1f} adx={ind['adx']:.1f} | "
        f"bull={ind['prob_bull']:.1f}% bear={ind['prob_bear']:.1f}% | "
        f"rvol={ind['rvol']:.2f}x stc={ind['stc']:.1f} | "
        f"funding={funding*100:+.4f}% | "
        f"liq_zones={ind['liq']['zone_count']}"
    )

    balance = get_balance(ex)

    # 4. Alerta funding extremo (una vez por evento)
    if ind["funding_extreme"] and state.last_fund_alert != date.today():
        dir_ = "SHORT" if funding > 0 else "LONG"
        tg.send(tg.msg_funding_alert(SYMBOL, funding, dir_))
        state.last_fund_alert = date.today()

    # 5. Heartbeat diario (08:00 UTC)
    now = datetime.utcnow()
    if now.hour == 8 and state.last_hb_date != date.today():
        tg.send(tg.msg_heartbeat(ind, balance, state.trades_today, state.pnl_day))
        state.last_hb_date = date.today()
        state.trades_today  = 0
        state.pnl_day       = 0.0

    # 6. Gestión posición abierta
    if state.position:
        price_now = ind["close"]
        atr_now   = ind["atr14"]
        tp = (state.entry_price + atr_now * ATR_MULT_TP
              if state.position == "long"
              else state.entry_price - atr_now * ATR_MULT_TP)
        sl = (state.entry_price - atr_now * ATR_MULT_SL
              if state.position == "long"
              else state.entry_price + atr_now * ATR_MULT_SL)

        hit_tp   = price_now >= tp if state.position == "long" else price_now <= tp
        hit_sl   = price_now <= sl if state.position == "long" else price_now >= sl
        hit_time = state.bar_count - state.entry_bar >= MAX_BARS_HOLD

        reason = None
        if hit_tp:   reason = "TP alcanzado"
        elif hit_sl: reason = "SL alcanzado"
        elif hit_time: reason = f"Tiempo max ({MAX_BARS_HOLD} velas)"

        if reason:
            close_position(ex, SYMBOL)
            pnl = ((price_now - state.entry_price) * state.entry_qty
                   if state.position == "long"
                   else (state.entry_price - price_now) * state.entry_qty)
            state.pnl_day      += pnl
            state.trades_today += 1
            state.position      = None
            state.last_signal   = None
            tg.send(tg.msg_close(
                state.position or ("long" if pnl > 0 else "short"),
                state.entry_price, price_now,
                state.entry_qty, reason, get_balance(ex)
            ))
            log.info(f"Posición cerrada: {reason} @ {price_now:.4f} | PnL {pnl:+.4f}")
        return

    # 7. Límite diario
    if state.trades_today >= MAX_TRADES_DAY:
        log.info(f"Límite diario alcanzado ({MAX_TRADES_DAY} trades). Esperando reset.")
        return

    # 8. Señales de entrada
    if ind["long"] and state.last_signal != "long":
        qty = calc_qty(balance, ind["close"], ind["atr14"])
        if qty <= 0:
            log.warning("Qty = 0. Revisa balance.")
            return
        tp  = round(ind["close"] + ind["atr14"] * ATR_MULT_TP, 4)
        sl  = round(ind["close"] - ind["atr14"] * ATR_MULT_SL, 4)

        order = open_long(ex, SYMBOL, qty, ind["close"], ind["atr14"])
        if order:
            state.position    = "long"
            state.entry_price = ind["close"]
            state.entry_qty   = qty
            state.entry_bar   = state.bar_count
            state.last_signal = "long"
            tg.send(tg.msg_signal(
                "long", ind, qty, tp, sl, balance, ind["long_score"]
            ))
            log.info(f"LONG @ {ind['close']:.4f} | score={ind['long_score']} | TP {tp} SL {sl}")

    elif ind["short"] and state.last_signal != "short":
        qty = calc_qty(balance, ind["close"], ind["atr14"])
        if qty <= 0:
            log.warning("Qty = 0. Revisa balance.")
            return
        tp  = round(ind["close"] - ind["atr14"] * ATR_MULT_TP, 4)
        sl  = round(ind["close"] + ind["atr14"] * ATR_MULT_SL, 4)

        order = open_short(ex, SYMBOL, qty, ind["close"], ind["atr14"])
        if order:
            state.position    = "short"
            state.entry_price = ind["close"]
            state.entry_qty   = qty
            state.entry_bar   = state.bar_count
            state.last_signal = "short"
            tg.send(tg.msg_signal(
                "short", ind, qty, tp, sl, balance, ind["short_score"]
            ))
            log.info(f"SHORT @ {ind['close']:.4f} | score={ind['short_score']} | TP {tp} SL {sl}")

    else:
        state.last_signal = None


# ── Arranque ──────────────────────────────────────────────────
def main():
    log.info("═"*52)
    log.info("  Sniper Bot V50 Ultimate — Arrancando")
    log.info("═"*52)

    ex  = build_exchange()
    bal = get_balance(ex)
    log.info(f"BingX Perpetual | {SYMBOL} | {TIMEFRAME} | {MODE.upper()} | Balance: ${bal:.2f}")
    tg.send(tg.msg_start(SYMBOL, TIMEFRAME, MODE))

    while True:
        try:
            tick(ex)
        except KeyboardInterrupt:
            log.info("Bot detenido manualmente.")
            tg.send("🔴 <b>Bot detenido manualmente.</b>")
            break
        except Exception as e:
            log.error(f"Error en tick: {e}")
            tg.send(tg.msg_error("tick principal", str(e)))
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    main()
