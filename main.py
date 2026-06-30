"""
daring-spontaneity — Triple Strategy Bot
==========================================
STRATEGY=EMA9_VWAP  → cruce EMA9 × VWAP + ATR trail
STRATEGY=PDH_BOS    → PDH/PDL Break of Structure + retest + EMA8 exit
STRATEGY=FIB        → Fibonacci Golden Pocket (0.5-0.618) + HTF trend
STRATEGY=BOTH       → PDH_BOS → FIB → EMA9_VWAP (por prioridad)
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
import strategy_fib     as fib
import strategy_unicorn as unicorn                  # Fibonacci Golden Pocket
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

# Tracks strategy + TP per side
_entry_reason: dict[str, str]   = {}
_fib_tp:       dict[str, float] = {}
_uni_tp:       dict[str, float] = {}


def main():
    log.info(f"=== {config.BOT_NAME} starting  strategy={config.STRATEGY} ===")

    client  = BingXClient(config.API_KEY, config.SECRET_KEY, config.BASE_URL)
    pos_mgr = PositionManager(client, config)
    risk    = RiskManager(config)
    tg      = TelegramClient(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT)

    active_sides  = SIDES.get(config.DIRECTION, ["LONG", "SHORT"])
    last_signal_t = 0.0
    last_trail_notif: dict[str, float] = {}

    tg.startup(config.BOT_NAME, config.SYMBOL, config.TIMEFRAME, config.LEVERAGE)

    while True:
        try:
            now    = time.time()
            equity = client.get_equity()
            price  = client.get_mark_price(config.SYMBOL)

            # ── 1. Manage open positions every tick ───────────────────────────
            for side in active_sides:
                pos = pos_mgr.get_position(config.SYMBOL, side)
                if pos is None:
                    continue

                candles  = client.get_klines(config.SYMBOL, config.TIMEFRAME, 60)
                sig_data = get_signal(candles, config.EMA_PERIOD, config.ATR_LENGTH)
                atr      = sig_data.get("atr") or 0

                reason = _entry_reason.get(side, "")

                # ATR trail stop
                if atr:
                    new_stop, hit = pos_mgr.tick_trail(config.SYMBOL, side, price, atr)

                    prev = last_trail_notif.get(side)
                    if new_stop and (prev is None or
                                     abs(new_stop - prev) / (prev or 1) > 0.003):
                        tg.trail_update(config.BOT_NAME, config.SYMBOL,
                                        side, price, new_stop)
                        last_trail_notif[side] = new_stop

                    if hit:
                        log.info(f"Trail stop hit — {side}  price={price:.6g}  stop={new_stop:.6g}")
                        _close(side, pos, config.SYMBOL, price, "Trail Stop",
                               pos_mgr, risk, tg)
                        last_trail_notif.pop(side, None)
                        _entry_reason.pop(side, None)
                        _fib_tp.pop(side, None)
                        continue

                # EMA8 exit — posiciones PDH_BOS
                if config.EMA8_EXIT and reason == "pdh_bos":
                    if pdh_bos.check_ema8_exit(client, config.SYMBOL, side):
                        log.info(f"EMA8 exit — {side}")
                        _close(side, pos, config.SYMBOL, price, "EMA8 Exit",
                               pos_mgr, risk, tg)
                        _entry_reason.pop(side, None)
                        continue

                # Fibonacci TP exit
                if reason == "fib":
                    tp = _fib_tp.get(side)
                    if tp and fib.check_tp_exit(candles, side, tp):
                        log.info(f"Fib TP reached — {side}  tp={tp:.6g}")
                        _close(side, pos, config.SYMBOL, price, "Fib TP",
                               pos_mgr, risk, tg)
                        _entry_reason.pop(side, None)
                        _fib_tp.pop(side, None)
                        continue

                # Unicorn TP exit
                if reason == "unicorn":
                    tp = _uni_tp.get(side)
                    if tp and unicorn.check_tp_exit(candles, side, tp):
                        log.info(f"Unicorn TP reached — {side}  tp={tp:.6g}")
                        _close(side, pos, config.SYMBOL, price, "Unicorn TP",
                               pos_mgr, risk, tg)
                        _entry_reason.pop(side, None)
                        _uni_tp.pop(side, None)
                        continue

            # ── 2. Signal check ───────────────────────────────────────────────
            if now - last_signal_t < config.SIGNAL_CHECK_SEC:
                time.sleep(config.TRAILING_CHECK_SEC)
                continue

            last_signal_t = now

            allowed, block_reason = risk.can_trade(equity)
            if not allowed:
                log.warning(f"Trading blocked: {block_reason}")
                tg.blocked(config.BOT_NAME, block_reason)
                time.sleep(config.TRAILING_CHECK_SEC)
                continue

            signal = None
            atr    = 0.0
            source = ""
            fib_tp_price = 0.0
            uni_tp_price = 0.0

            # ── UNICORN — prioridad 1 (Sweep+Breaker+FVG) ────────────────────
            if config.STRATEGY in ("UNICORN", "BOTH"):
                try:
                    c5m = client.get_klines(config.SYMBOL, "5m",  200)
                    c1h = client.get_klines(config.SYMBOL, "1h",   60)
                    u_sig = unicorn.get_signal(c5m, c1h, config)
                    if u_sig["signal"]:
                        signal      = u_sig["signal"]
                        atr         = u_sig["atr"]
                        uni_tp_price = u_sig["tp_price"]
                        source      = "unicorn"
                        fvg_tag = "+FVG" if u_sig["has_fvg"] else ""
                        log.info(
                            f"UNICORN{fvg_tag}  signal={signal}  "
                            f"swept={u_sig['swept_level']:.6g}  "
                            f"breaker={u_sig['breaker_bottom']:.6g}-{u_sig['breaker_top']:.6g}  "
                            f"tp={uni_tp_price:.6g}  atr={atr:.4g}"
                        )
                    else:
                        log.info(
                            f"UNICORN  None  "
                            f"atr={u_sig['atr']:.4g}  price={price:.6g}"
                        )
                except Exception as e:
                    log.warning(f"UNICORN check error: {e}")

            # ── PDH BOS — prioridad 2 ─────────────────────────────────────────
            if config.STRATEGY in ("PDH_BOS", "BOTH"):
                pdh_sig = pdh_bos.get_signal(client, config.SYMBOL, config)
                if pdh_sig["signal"]:
                    signal = pdh_sig["signal"]
                    atr    = pdh_sig["atr"]
                    source = "pdh_bos"
                    log.info(
                        f"PDH_BOS  signal={signal}  "
                        f"bos={pdh_sig['bos_level']:.6g}  "
                        f"ema8={pdh_sig['ema8']:.6g}  atr={atr:.4g}"
                    )
                else:
                    bos_st = (f"BOS_ACTIVE level={pdh_sig['bos_level']:.6g}"
                              if pdh_sig.get("bos_active") else "sin BOS")
                    log.info(
                        f"PDH_BOS  None  {bos_st}  "
                        f"pdh={pdh_sig['pdh']:.6g}  pdl={pdh_sig['pdl']:.6g}  "
                        f"ema8={pdh_sig['ema8']:.6g}  price={price:.6g}"
                    )

            # ── Fibonacci — prioridad 3 ───────────────────────────────────────
            if not signal and config.STRATEGY in ("FIB", "BOTH"):
                try:
                    c5m = client.get_klines(config.SYMBOL, "5m",  120)
                    c1h = client.get_klines(config.SYMBOL, "1h",  60)
                    fib_sig = fib.get_signal(c5m, c1h, config)
                    if fib_sig["signal"]:
                        signal      = fib_sig["signal"]
                        atr         = fib_sig["atr"]
                        fib_tp_price = fib_sig["tp_price"]
                        source      = "fib"
                        log.info(
                            f"FIB  signal={signal}  "
                            f"pocket={fib_sig['fib_618']:.6g}-{fib_sig['fib_50']:.6g}  "
                            f"rsi={fib_sig['rsi']:.1f}  trend={fib_sig['trend']}  "
                            f"tp={fib_tp_price:.6g}  atr={atr:.4g}"
                        )
                    else:
                        log.info(
                            f"FIB  None  "
                            f"swing={fib_sig['swing_low']:.6g}-{fib_sig['swing_high']:.6g}  "
                            f"pocket={fib_sig['fib_618']:.6g}-{fib_sig['fib_50']:.6g}  "
                            f"rsi={fib_sig['rsi']:.1f}  trend={fib_sig['trend']}"
                        )
                except Exception as e:
                    log.warning(f"FIB check error: {e}")

            # ── EMA9×VWAP — fallback (prioridad 4) ─────────────────────────────
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
                        f"vwap={sig_data['vwap']:.6g}  atr={atr:.4g}"
                    )
                else:
                    log.info(
                        f"EMA9_VWAP  None  "
                        f"ema9={sig_data['ema9']:.6g}  "
                        f"vwap={sig_data['vwap']:.6g}  price={price:.6g}"
                    )

            if not signal or not atr:
                time.sleep(config.TRAILING_CHECK_SEC)
                continue

            # ── Execute signal ────────────────────────────────────────────────
            if signal == "LONG" and "LONG" in active_sides:
                opened = _handle_entry("LONG", "SHORT", price, atr, equity,
                                       pos_mgr, risk, tg)
                if opened:
                    _entry_reason["LONG"] = source
                    if source == "fib"     and fib_tp_price:  _fib_tp["LONG"] = fib_tp_price
                    if source == "unicorn" and uni_tp_price:  _uni_tp["LONG"] = uni_tp_price

            elif signal == "SHORT" and "SHORT" in active_sides:
                opened = _handle_entry("SHORT", "LONG", price, atr, equity,
                                       pos_mgr, risk, tg)
                if opened:
                    _entry_reason["SHORT"] = source
                    if source == "fib"     and fib_tp_price:  _fib_tp["SHORT"] = fib_tp_price
                    if source == "unicorn" and uni_tp_price:  _uni_tp["SHORT"] = uni_tp_price

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

    opp = pos_mgr.get_position(sym, exit_side)
    if opp:
        _close(exit_side, opp, sym, price, f"Reversal→{enter_side}",
               pos_mgr, risk, tg)
        _entry_reason.pop(exit_side, None)
        _fib_tp.pop(exit_side, None)
        _uni_tp.pop(exit_side, None)

    if pos_mgr.has_position(sym, enter_side):
        log.info(f"Already {enter_side} — skip")
        return False

    qty = pos_mgr.calc_qty(sym, price, atr, equity)
    if not qty:
        return False

    if enter_side == "LONG":
        ok = pos_mgr.open_long(sym, qty)
    else:
        ok = pos_mgr.open_short(sym, qty)

    if ok:
        stop, _ = pos_mgr.tick_trail(sym, enter_side, price, atr)
        tg.entry(config.BOT_NAME, sym, enter_side, price, qty, stop, equity)

    return ok


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
