# -*- coding: utf-8 -*-
"""pos_manager.py -- Phantom Edge Bot ELITE: Institutional Position Manager.

Exit cascade (in order):
  Phase 1 — First partial: 25% close @ +1.0R (lock fast profit)
  Phase 2 — Second partial: 25% close @ +2.0R (lock more)
  Phase 3 — Trail rest: ST5 flip / ZZ swing break / RSI div / ST15 flip
  Phase 4 — Dynamic tight trail: peak_R > 2.5, ST mult halved
  Phase 5 — Time exit: >6h open + R < 0.5 (no bag holding)

Batch API optimization:
  - get_all_tickers() + get_all_positions() = 2 calls for ALL trades
  - No per-trade price requests during manage loop
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from loguru import logger

import client as ex
import notifier


@dataclass
class Trade:
    symbol:       str
    side:         str
    entry:        float
    sl:           float
    tp:           float
    atr:          float
    size_usdt:    float
    leverage:     int   = 10
    qty:          float = 0.0
    score:        int   = 1
    vol_ratio:    float = 1.0
    delta1:       float = 0.0
    delta2:       float = 0.0
    partial1_done: bool = False   # 25% at +1R
    partial2_done: bool = False   # 25% at +2R
    be_done:       bool = False   # SL moved to entry
    closed:        bool = False
    order_id:      str  = ""
    peak_r:        float = 0.0
    bot_opened:    bool  = True
    opened_at:     datetime = field(default_factory=datetime.utcnow)
    opened_str:    str     = field(default_factory=lambda: datetime.utcnow().strftime("%H:%M UTC"))

    # Legacy aliases
    @property
    def partial_done(self): return self.partial1_done


_trades:          dict[str, Trade] = {}
_daily_pnl:       float = 0.0
_daily_trades:    int   = 0
_daily_wins:      int   = 0
_daily_losses:    int   = 0
_consec_losses:   int   = 0
_day_started:     date  = date.today()
_initial_balance: float = 0.0
_halted:          bool  = False


def add_trade(t: Trade) -> None:
    global _daily_trades
    _trades[t.symbol] = t
    _daily_trades += 1

def remove_trade(sym: str) -> None:
    _trades.pop(sym, None)

def open_symbols() -> set[str]:
    return set(_trades.keys())

def trade_count() -> int:
    return sum(1 for t in _trades.values() if t.bot_opened and not t.closed)

def is_halted() -> bool:
    return _halted

def consecutive_losses() -> int:
    return _consec_losses

def get_stats() -> dict:
    return {
        "open":          trade_count(),
        "daily_trades":  _daily_trades,
        "daily_pnl":     round(_daily_pnl, 4),
        "daily_wins":    _daily_wins,
        "daily_losses":  _daily_losses,
        "consec_losses": _consec_losses,
        "halted":        _halted,
    }


def _reset_daily() -> None:
    global _daily_pnl, _daily_trades, _daily_wins, _daily_losses
    global _day_started, _halted, _consec_losses
    if date.today() != _day_started:
        logger.info(f"[DAILY RESET] PnL={_daily_pnl:+.4f} W={_daily_wins} L={_daily_losses}")
        _daily_pnl = _daily_trades = _daily_wins = _daily_losses = _consec_losses = 0
        _day_started = date.today()
        _halted = False


def _record_exit(pnl: float) -> None:
    global _daily_pnl, _daily_wins, _daily_losses, _consec_losses
    _daily_pnl += pnl
    if pnl >= 0:
        _daily_wins   += 1; _consec_losses = 0
    else:
        _daily_losses += 1; _consec_losses += 1


def _calc_pnl(trade: Trade, exit_price: float) -> tuple[float, float]:
    pct  = ((exit_price - trade.entry) / trade.entry * 100) if trade.side == "BUY" \
           else ((trade.entry - exit_price) / trade.entry * 100)
    return round(pct, 4), round(pct / 100 * trade.size_usdt * trade.leverage, 4)


def _r_dist(trade: Trade) -> float:
    from config import cfg
    d = trade.atr * cfg.atr_mult if trade.atr > 0 else abs(trade.entry - trade.sl)
    return max(d, 1e-9)


async def _circuit_breaker() -> bool:
    global _halted
    from config import cfg
    if _daily_trades >= cfg.max_daily_trades:
        if not _halted:
            _halted = True
            await notifier.notify(f"HALT: max trades {cfg.max_daily_trades} | PnL={_daily_pnl:+.4f}")
        return True
    if _initial_balance > 0:
        loss_pct = -_daily_pnl / _initial_balance * 100
        if loss_pct >= cfg.max_daily_loss_pct:
            if not _halted:
                _halted = True
                await notifier.notify(f"HALT: perdida {loss_pct:.2f}% | PnL={_daily_pnl:+.4f}")
            return True
    return False


async def sync_from_exchange() -> None:
    global _initial_balance
    live = await ex.get_all_positions()
    bal  = await ex.get_balance()
    _initial_balance = bal
    logger.info(f"[INIT] Balance={bal:.2f} | Externas={len(live)}")
    for sym, pos in live.items():
        if sym in _trades: continue
        amt = float(pos.get("positionAmt", 0))
        ep  = float(pos.get("avgPrice", 0))
        if abs(amt) < 1e-9 or ep <= 0: continue
        _trades[sym] = Trade(
            symbol=sym, side="BUY" if amt > 0 else "SELL",
            entry=ep, sl=0., tp=0., atr=0., size_usdt=0.,
            qty=abs(amt), partial1_done=True, partial2_done=True,
            be_done=True, bot_opened=False,
        )
        logger.info(f"[SYNC] {sym} {'BUY' if amt>0 else 'SELL'} @ {ep:.6f} externo")
    await notifier.notify(f"Bot iniciado\nBalance: {bal:.2f} USDT | Externas: {len(live)}")


async def _partial_close(trade: Trade, pct: float, price: float, label: str) -> bool:
    qty = round(trade.qty * pct, 6)
    if qty <= 0: return False
    side = "SELL" if trade.side == "BUY" else "BUY"
    resp = await ex.place_reduce_order(trade.symbol, side, qty)
    if resp.get("code", -1) in (0, 200):
        _, pnl = _calc_pnl(trade, price)
        partial_pnl = round(pnl * pct, 4)
        trade.qty -= qty
        logger.info(f"[{label}] {trade.symbol} -{qty:.6f} @ {price:.6f} PnL≈{partial_pnl:+.4f}")
        await notifier.notify_partial(
            symbol=trade.symbol, qty_closed=qty,
            qty_remaining=trade.qty, price=price, pnl_usdt=partial_pnl,
        )
        return True
    return False


async def _do_exit(trade: Trade, live_pos: dict, price: float,
                   r: float, reason: str) -> bool:
    resp = await ex.close_position(trade.symbol, live_pos)
    if resp.get("code", -1) not in (0, 200):
        logger.warning(f"[CLOSE FAIL] {trade.symbol}: {resp}")
        return False
    _, pnl = _calc_pnl(trade, price)
    _record_exit(pnl)
    trade.closed = True
    logger.info(f"[EXIT:{reason}] {trade.symbol} @ {price:.6f} R={r:.2f} PnL={pnl:+.4f}")
    await notifier.notify_exit(
        symbol=trade.symbol, side=trade.side,
        entry=trade.entry, exit_price=price,
        qty=trade.qty, size_usdt=trade.size_usdt, leverage=trade.leverage,
        r_achieved=r, peak_r=trade.peak_r, exit_reason=reason,
    )
    total = _daily_wins + _daily_losses
    if total > 0 and total % 5 == 0:
        bal = await ex.get_balance()
        await notifier.notify_daily_summary(
            _daily_trades, _daily_wins, _daily_losses, _daily_pnl, bal
        )
    return True


async def manage_positions(ohlcv_map: dict[str, dict]) -> None:
    from config import cfg
    from strategy import check_trail_exit
    _reset_daily()
    await _circuit_breaker()

    active = [t for t in _trades.values() if t.bot_opened and not t.closed]
    if not active:
        for sym in [s for s,t in list(_trades.items()) if t.closed]:
            remove_trade(sym)
        return

    # ── 2 API calls for ALL trades ────────────────────────────
    all_prices, all_live = await asyncio.gather(
        ex.get_all_tickers(),
        ex.get_all_positions(),
    )
    closed_syms: list[str] = []

    for trade in active:
        sym   = trade.symbol
        price = all_prices.get(sym, 0.0)
        if price <= 0:
            price = await ex.get_price(sym)
        if price <= 0:
            continue

        rd    = _r_dist(trade)
        pnl_p = (price - trade.entry) if trade.side == "BUY" else (trade.entry - price)
        r_now = pnl_p / rd
        if r_now > trade.peak_r:
            trade.peak_r = r_now

        logger.debug(
            f"[POS] {sym} {trade.side} p={price:.6f} "
            f"R={r_now:.2f} pk={trade.peak_r:.2f} "
            f"p1={trade.partial1_done} p2={trade.partial2_done} be={trade.be_done}"
        )

        # ── Time exit (bag holder) ────────────────────────────
        age_h = (datetime.utcnow() - trade.opened_at).total_seconds() / 3600
        if age_h > cfg.max_trade_hours and r_now < cfg.min_r_time_exit and trade.be_done:
            if sym in all_live:
                ok = await _do_exit(trade, all_live[sym], price, r_now, "TIME_EXIT")
                if ok: closed_syms.append(sym)
            continue

        # ── Phase 1: 25% partial at +1R ──────────────────────
        if not trade.partial1_done and r_now >= 1.0:
            ok = await _partial_close(trade, 0.25, price, "PARTIAL_1R")
            if ok: trade.partial1_done = True

        # ── Phase 2: 25% partial at +2R ──────────────────────
        if trade.partial1_done and not trade.partial2_done and r_now >= 2.0:
            ok = await _partial_close(trade, 0.25 / 0.75, price, "PARTIAL_2R")  # 25% of remaining
            if ok: trade.partial2_done = True

        # ── Move SL to breakeven at +1R ───────────────────────
        if not trade.be_done and r_now >= 1.0:
            await ex.cancel_all_orders(sym)
            trade.be_done = True
            logger.info(f"[BE] {sym} SL→entry {trade.entry:.6f} R={r_now:.2f}")
            await notifier.notify_breakeven(sym, trade.side, trade.entry, r_now)

        # ── Check if exchange closed it ───────────────────────
        if trade.be_done and sym not in all_live:
            reason = "TP" if r_now >= cfg.rr * 0.85 else "MANUAL"
            _, pnl = _calc_pnl(trade, price)
            _record_exit(pnl)
            trade.closed = True
            closed_syms.append(sym)
            await notifier.notify_exit(
                symbol=sym, side=trade.side, entry=trade.entry, exit_price=price,
                qty=trade.qty, size_usdt=trade.size_usdt, leverage=trade.leverage,
                r_achieved=r_now, peak_r=trade.peak_r, exit_reason=reason,
            )
            continue

        # ── Trail exit (post-BE only) ─────────────────────────
        if trade.be_done and sym in all_live:
            data = ohlcv_map.get(sym, {})
            if data:
                reason = check_trail_exit(
                    ohlcv_5m   = data.get(cfg.timeframe, {}),
                    ohlcv_15m  = data.get(cfg.timeframe_slow, None),
                    trade_side = trade.side,
                    st_period  = cfg.st_period,
                    st_mult    = cfg.st_mult,
                    zz_deviation = cfg.zz_deviation,
                    peak_r     = trade.peak_r,
                )
                if reason:
                    ok = await _do_exit(trade, all_live[sym], price, r_now, reason)
                    if ok: closed_syms.append(sym)

        # ── SL hit before BE (exchange closed it) ─────────────
        if not trade.be_done and sym not in all_live:
            _, pnl = _calc_pnl(trade, price)
            _record_exit(pnl)
            trade.closed = True
            closed_syms.append(sym)
            await notifier.notify_exit(
                symbol=sym, side=trade.side, entry=trade.entry, exit_price=price,
                qty=trade.qty, size_usdt=trade.size_usdt, leverage=trade.leverage,
                r_achieved=r_now, peak_r=trade.peak_r, exit_reason="SL",
            )

    for sym in closed_syms:
        remove_trade(sym)
