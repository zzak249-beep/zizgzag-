"""
EMA9 × VWAP bot — main loop.

Loop cadence:
  Every TRAILING_CHECK_SEC : update ATR trail stop → close on hit
  Every SIGNAL_CHECK_SEC   : fetch candles → detect crossover → open trade
"""

import logging
import time
import traceback

import config
from bingx_client      import BingXClient
from position_manager  import PositionManager
from risk_manager      import RiskManager
from strategy          import get_signal
from telegram_client   import TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("main")

SIDES = {
    "BOTH":  ["LONG", "SHORT"],
    "LONG":  ["LONG"],
    "SHORT": ["SHORT"],
}


def main():
    log.info(f"=== {config.BOT_NAME} starting ===")

    client  = BingXClient(config.API_KEY, config.SECRET_KEY, config.BASE_URL)
    pos_mgr = PositionManager(client, config)
    risk    = RiskManager(config)
    tg      = TelegramClient(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT)

    active_sides = SIDES.get(config.DIRECTION, ["LONG", "SHORT"])

    tg.startup(config.BOT_NAME, config.SYMBOL, config.TIMEFRAME, config.LEVERAGE)

    last_signal_t  = 0.0
    last_trail_notif: dict[str, float] = {}   # side → last stop notified

    while True:
        try:
            now    = time.time()
            equity = client.get_equity()
            price  = client.get_mark_price(config.SYMBOL)

            # ── 1. Manage trailing stops (every loop tick) ────────
            for side in active_sides:
                pos = pos_mgr.get_position(config.SYMBOL, side)
                if pos is None:
                    continue

                # Fetch candles only for ATR
                candles = client.get_klines(config.SYMBOL, config.TIMEFRAME, 50)
                sig_data = get_signal(candles, config.EMA_PERIOD, config.ATR_LENGTH)
                atr = sig_data.get("atr")
                if not atr:
                    continue

                new_stop, hit = pos_mgr.tick_trail(config.SYMBOL, side, price, atr)

                # Notify when stop moves >0.3%
                prev = last_trail_notif.get(side)
                if prev is None or abs(new_stop - prev) / prev > 0.003:
                    tg.trail_update(config.BOT_NAME, config.SYMBOL, side, price, new_stop)
                    last_trail_notif[side] = new_stop

                if hit:
                    log.info(f"Trail stop hit — closing {side}  price={price}  stop={new_stop}")
                    pnl = pos["unrealizedPnl"]
                    if side == "LONG":
                        pos_mgr.close_long(config.SYMBOL, pos["size"], "trail_stop")
                    else:
                        pos_mgr.close_short(config.SYMBOL, pos["size"], "trail_stop")
                    risk.record_trade(pnl)
                    tg.exit_trade(config.BOT_NAME, config.SYMBOL, side, price, "Trail Stop", pnl)
                    last_trail_notif.pop(side, None)

            # ── 2. Signal check (every SIGNAL_CHECK_SEC) ──────────
            if now - last_signal_t < config.SIGNAL_CHECK_SEC:
                time.sleep(config.TRAILING_CHECK_SEC)
                continue

            last_signal_t = now

            allowed, reason = risk.can_trade(equity)
            if not allowed:
                log.warning(f"Trading blocked: {reason}")
                tg.blocked(config.BOT_NAME, reason)
                time.sleep(config.TRAILING_CHECK_SEC)
                continue

            candles  = client.get_klines(config.SYMBOL, config.TIMEFRAME, config.CANDLES)
            sig_data = get_signal(candles, config.EMA_PERIOD, config.ATR_LENGTH)
            signal   = sig_data["signal"]
            atr      = sig_data["atr"]

            log.info(
                f"Signal={signal}  EMA9={sig_data['ema9']:.6g}  "
                f"VWAP={sig_data['vwap']:.6g}  ATR={atr}  price={price:.6g}"
            )

            if signal == "LONG" and "LONG" in active_sides:
                _handle_long(signal, price, atr, equity, pos_mgr, risk, tg)

            elif signal == "SHORT" and "SHORT" in active_sides:
                _handle_short(signal, price, atr, equity, pos_mgr, risk, tg)

        except KeyboardInterrupt:
            log.info("Stopping.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}\n{traceback.format_exc()}")
            tg.error(config.BOT_NAME, str(e)[:400])
            time.sleep(30)

        time.sleep(config.TRAILING_CHECK_SEC)


# ── Entry helpers ──────────────────────────────────────────────────────────────

def _handle_long(signal, price, atr, equity, pos_mgr: PositionManager,
                 risk: RiskManager, tg: TelegramClient):
    sym = config.SYMBOL

    # Close opposing SHORT first (reversal)
    short_pos = pos_mgr.get_position(sym, "SHORT")
    if short_pos:
        pnl = short_pos["unrealizedPnl"]
        pos_mgr.close_short(sym, short_pos["size"], "reversal")
        risk.record_trade(pnl)
        tg.exit_trade(config.BOT_NAME, sym, "SHORT", price, "Reversal LONG", pnl)

    if pos_mgr.has_position(sym, "LONG"):
        log.info("Already LONG — skip entry")
        return

    qty = pos_mgr.calc_qty(sym, price, atr, equity)
    pos_mgr.open_long(sym, qty)
    stop, _ = pos_mgr.tick_trail(sym, "LONG", price, atr)
    tg.entry(config.BOT_NAME, sym, "LONG", price, qty, stop, equity)


def _handle_short(signal, price, atr, equity, pos_mgr: PositionManager,
                  risk: RiskManager, tg: TelegramClient):
    sym = config.SYMBOL

    # Close opposing LONG first (reversal)
    long_pos = pos_mgr.get_position(sym, "LONG")
    if long_pos:
        pnl = long_pos["unrealizedPnl"]
        pos_mgr.close_long(sym, long_pos["size"], "reversal")
        risk.record_trade(pnl)
        tg.exit_trade(config.BOT_NAME, sym, "LONG", price, "Reversal SHORT", pnl)

    if pos_mgr.has_position(sym, "SHORT"):
        log.info("Already SHORT — skip entry")
        return

    qty = pos_mgr.calc_qty(sym, price, atr, equity)
    pos_mgr.open_short(sym, qty)
    stop, _ = pos_mgr.tick_trail(sym, "SHORT", price, atr)
    tg.entry(config.BOT_NAME, sym, "SHORT", price, qty, stop, equity)


if __name__ == "__main__":
    main()
