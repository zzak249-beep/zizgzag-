"""
daring-spontaneity — Dual Strategy Bot
========================================
STRATEGY=EMA9_VWAP  → cruce EMA9 × VWAP + ATR trail
STRATEGY=PDH_BOS    → PDH/PDL Break of Structure + retest EMA8 exit
STRATEGY=BOTH       → PDH_BOS prioritario, EMA9_VWAP como fallback
"""

import logging
import time
import traceback

import config
from bingx_client     import BingXClient
from position_manager import PositionManager
from risk_manager     import RiskManager
from strategy         import get_signal          # EMA9×VWAP
import strategy_pdh_bos as pdh_bos              # PDH BOS Retest
from telegram_client  import TelegramClient

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

# Tracks which strategy opened each side: "pdh_bos" | "ema9_vwap"
_entry_reason: dict[str, str] = {}


def main():
    log.info(f"=== {config.BOT_NAME} starting  strategy={config.STRATEGY} ===")

    client  = BingXClient(config.API_KEY, config.SECRET_KEY, config.BASE_URL)
    pos_mgr = PositionManager(client, config)
    risk    = RiskManager(config)
    tg      = TelegramClient(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT)

    active_sides   = SIDES.get(config.DIRECTION, ["LONG", "SHORT"])
    last_signal_t  = 0.0
    last_trail_notif: dict[str, float] = {}

    tg.startup(config.BOT_NAME, config.SYMBOL, config.TIMEFRAME, config.LEVERAGE)

    while True:
        try:
            now    = time.time()
            equity = client.get_equity()
            price  = client.get_mark_price(config.SYMBOL)

            # ── 1. Trail stop + EMA8 exit (every tick) ───────────────────────
            for side in active_sides:
                pos = pos_mgr.get_position(config.SYMBOL, side)
                if pos is None:
                    continue

                candles = client.get_klines(config.SYMBOL, config.TIMEFRAME, 50)
                sig_data = get_signal(candles, config.EMA_PERIOD, config.ATR_LENGTH)
                atr = sig_data.get("atr") or 0

                # ATR trail stop
                if atr:
                    new_stop, hit = pos_mgr.tick_trail(config.SYMBOL, side, price, atr)

                    prev = last_trail_notif.get(side)
                    if new_stop and (prev is None or abs(new_stop - prev) / prev > 0.003):
                        tg.trail_update(config.BOT_NAME, config.SYMBOL, side, price, new_stop)
                        last_trail_notif[side] = new_stop

                    if hit:
                        log.info(f"Trail stop hit — {side}  price={price}  stop={new_stop}")
                        pnl = pos["unrealizedPnl"]
                        _close(side, pos, config.SYMBOL, price, "Trail Stop",
                               pos_mgr, risk, tg)
                        last_trail_notif.pop(side, None)
                        _entry_reason.pop(side, None)
                        continue

                # EMA8 exit — solo si la posición fue abierta por PDH_BOS
                reason = _entry_reason.get(side, "")
                if config.EMA8_EXIT and reason == "pdh_bos":
                    if pdh_bos.check_ema8_exit(client, config.SYMBOL, side):
                        log.info(f"EMA8 break exit — {side}")
                        pnl = pos["unrealizedPnl"]
                        _close(side, pos, config.SYMBOL, price, "EMA8 Exit",
                               pos_mgr, risk, tg)
                        _entry_reason.pop(side, None)

            # ── 2. Signal check ───────────────────────────────────────────────
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

            signal = None
            atr    = 0.0
            source = ""

            # ── PDH BOS (mayor prioridad) ─────────────────────────────────────
            if config.STRATEGY in ("PDH_BOS", "BOTH"):
                pdh_sig = pdh_bos.get_signal(client, config.SYMBOL, config)
                if pdh_sig["signal"]:
                    signal = pdh_sig["signal"]
                    atr    = pdh_sig["atr"]
                    source = "pdh_bos"
                    log.info(
                        f"PDH_BOS  signal={signal}  "
                        f"bos={pdh_sig['bos_level']:.6g}  "
                        f"ema8={pdh_sig['ema8']:.6g}  "
                        f"atr={atr:.6g}"
                    )

            # ── EMA9×VWAP (fallback) ──────────────────────────────────────────
            if not signal and config.STRATEGY in ("EMA9_VWAP", "BOTH"):
                candles  = client.get_klines(config.SYMBOL, config.TIMEFRAME, config.CANDLES)
                sig_data = get_signal(candles, config.EMA_PERIOD, config.ATR_LENGTH)
                if sig_data["signal"]:
                    signal = sig_data["signal"]
                    atr    = sig_data["atr"]
                    source = "ema9_vwap"
                    log.info(
                        f"EMA9_VWAP  signal={signal}  "
                        f"ema9={sig_data['ema9']:.6g}  "
                        f"vwap={sig_data['vwap']:.6g}  "
                        f"atr={atr:.6g}"
                    )
                else:
                    log.info(
                        f"Signal=None  EMA9={sig_data['ema9']:.6g}  "
                        f"VWAP={sig_data['vwap']:.6g}  "
                        f"ATR={sig_data['atr']}  price={price:.6g}"
                    )

            if not signal or not atr:
                time.sleep(config.TRAILING_CHECK_SEC)
                continue

            # ── Ejecutar señal ────────────────────────────────────────────────
            if signal == "LONG" and "LONG" in active_sides:
                opened = _handle_entry("LONG", "SHORT", price, atr, equity,
                                       pos_mgr, risk, tg)
                if opened:
                    _entry_reason["LONG"] = source

            elif signal == "SHORT" and "SHORT" in active_sides:
                opened = _handle_entry("SHORT", "LONG", price, atr, equity,
                                       pos_mgr, risk, tg)
                if opened:
                    _entry_reason["SHORT"] = source

        except KeyboardInterrupt:
            log.info("Stopping.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}\n{traceback.format_exc()}")
            tg.error(config.BOT_NAME, str(e)[:400])
            time.sleep(30)

        time.sleep(config.TRAILING_CHECK_SEC)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _handle_entry(enter_side: str, exit_side: str, price: float, atr: float,
                  equity: float, pos_mgr: PositionManager,
                  risk: RiskManager, tg: TelegramClient) -> bool:
    sym = config.SYMBOL

    # Cierra lado contrario si existe (reversal)
    opp = pos_mgr.get_position(sym, exit_side)
    if opp:
        pnl = opp["unrealizedPnl"]
        _close(exit_side, opp, sym, price, f"Reversal→{enter_side}", pos_mgr, risk, tg)
        _entry_reason.pop(exit_side, None)

    if pos_mgr.has_position(sym, enter_side):
        log.info(f"Already {enter_side} — skip")
        return False

    qty = pos_mgr.calc_qty(sym, price, atr, equity)
    if enter_side == "LONG":
        pos_mgr.open_long(sym, qty)
    else:
        pos_mgr.open_short(sym, qty)

    stop, _ = pos_mgr.tick_trail(sym, enter_side, price, atr)
    tg.entry(config.BOT_NAME, sym, enter_side, price, qty, stop, equity)
    return True


def _close(side: str, pos: dict, sym: str, price: float, reason: str,
           pos_mgr: PositionManager, risk: RiskManager, tg: TelegramClient):
    pnl = pos["unrealizedPnl"]
    if side == "LONG":
        pos_mgr.close_long(sym, pos["size"], reason)
    else:
        pos_mgr.close_short(sym, pos["size"], reason)
    risk.record_trade(pnl)
    tg.exit_trade(config.BOT_NAME, sym, side, price, reason, pnl)


if __name__ == "__main__":
    main()
