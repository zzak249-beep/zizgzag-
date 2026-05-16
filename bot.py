"""
Bot Orchestrator — Sniper Bot V50.6
Main async loop: scans symbols, manages positions,
handles trailing SL to breakeven, and daily reports.
"""
import asyncio
import logging
import uuid
from datetime import datetime, time
from typing import Dict, Optional
from config.settings import Settings
from src.strategy import SniperStrategy
from src.exchange import ExchangeClient
from src.telegram import TelegramNotifier
from src.analytics import TradeJournal

logger = logging.getLogger(__name__)


class ActivePosition:
    """Tracks an open position in memory for trailing SL management."""
    def __init__(self, trade_id, symbol, direction, entry, tp, sl, qty, sig):
        self.trade_id   = trade_id
        self.symbol     = symbol
        self.direction  = direction
        self.entry      = entry
        self.tp         = tp
        self.sl         = sl
        self.qty        = qty
        self.sig        = sig              # original Signal
        self.be_moved   = False            # SL-to-breakeven already done?
        self.bars_open  = 0


class SniperBot:
    def __init__(self, settings: Settings):
        self.s        = settings
        self.strategy = SniperStrategy(settings)
        self.exchange = ExchangeClient(settings)
        self.telegram = TelegramNotifier(settings)
        self.journal  = TradeJournal(settings.TRADE_JOURNAL)
        self._open_positions: Dict[str, ActivePosition] = {}  # symbol → pos
        self._running  = False
        self._last_daily_report = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def run(self):
        logger.info("🚀 Sniper Bot V50.6 starting…")
        await self.exchange.connect()
        await self.telegram.start()
        await self.telegram.send_startup(self.s.SYMBOLS)
        self.journal.print_summary()
        self._running = True

        try:
            while self._running:
                await self._tick()
                await asyncio.sleep(self.s.SCAN_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Bot cancelled — shutting down.")
        finally:
            await self.exchange.close()
            await self.telegram.stop()
            logger.info("Bot stopped.")

    async def stop(self):
        self._running = False

    # ── Main tick ─────────────────────────────────────────────────────────────

    async def _tick(self):
        now = datetime.utcnow()
        logger.info(f"⏱  Tick {now.strftime('%Y-%m-%d %H:%M')} UTC | "
                    f"open pos={len(self._open_positions)}")

        # 1. Manage existing positions (trailing SL, forced exits)
        await self._manage_open_positions()

        # 2. Daily report at 00:05 UTC
        await self._maybe_send_daily_report(now)

        # 3. Scan for new signals
        if len(self._open_positions) >= self.s.MAX_OPEN_POSITIONS:
            logger.info("Max positions reached — skipping scan.")
            return

        balance = await self.exchange.get_balance()
        if balance <= 0:
            logger.warning("Zero balance — skipping scan.")
            return

        # Scan in parallel
        tasks = [self._scan_symbol(sym, balance, now.hour) for sym in self.s.SYMBOLS
                 if sym not in self._open_positions]
        await asyncio.gather(*tasks)

    # ── Symbol scan ───────────────────────────────────────────────────────────

    async def _scan_symbol(self, symbol: str, balance: float, utc_hour: int):
        try:
            df_primary = await self.exchange.fetch_ohlcv(symbol, self.s.PRIMARY_TF, self.s.CANDLES_NEEDED)
            df_confirm = await self.exchange.fetch_ohlcv(symbol, self.s.CONFIRM_TF, 250)

            if df_primary is None:
                return

            sig = self.strategy.analyze(
                df_primary  = df_primary,
                df_confirm  = df_confirm,
                symbol      = symbol,
                current_hour_utc = utc_hour,
                available_capital = balance,
            )

            if sig.direction == "NONE":
                logger.debug(f"[{symbol}] No signal: {sig.reason}")
                return

            logger.info(f"[{symbol}] 🎯 Signal: {sig.direction} | {sig.reason}")

            # Send Telegram alert BEFORE executing
            await self.telegram.send_signal(sig)

            # Execute on exchange
            order = await self.exchange.open_position(
                symbol      = symbol,
                direction   = sig.direction,
                size_pct    = sig.size_pct,
                entry_price = sig.entry_price,
                tp_price    = sig.tp_price,
                sl_price    = sig.sl_price,
                capital     = balance,
            )

            if order is None:
                logger.error(f"[{symbol}] Order failed — not tracking position")
                return

            # Estimate qty
            qty = (balance * sig.size_pct / 100 * self.s.LEVERAGE) / sig.entry_price

            trade_id = str(uuid.uuid4())[:8]
            self.journal.open_trade(
                trade_id   = trade_id,
                symbol     = symbol,
                direction  = sig.direction,
                entry      = sig.entry_price,
                tp         = sig.tp_price,
                sl         = sig.sl_price,
                size_pct   = sig.size_pct,
                indicators = {"rvol": sig.rvol, "adx": sig.adx,
                              "rsi": sig.rsi, "slope": sig.slope},
            )

            self._open_positions[symbol] = ActivePosition(
                trade_id  = trade_id,
                symbol    = symbol,
                direction = sig.direction,
                entry     = sig.entry_price,
                tp        = sig.tp_price,
                sl        = sig.sl_price,
                qty       = qty,
                sig       = sig,
            )

            await self.telegram.send_trade_opened(sig, order.get("id", "?"), round(qty, 4))

        except Exception as exc:
            logger.exception(f"[{symbol}] Scan error: {exc}")
            await self.telegram.send_error(f"[{symbol}] {exc}")

    # ── Position management ───────────────────────────────────────────────────

    async def _manage_open_positions(self):
        """
        For each tracked position:
          • Check if TP/SL was hit (no open orders left)
          • Move SL to breakeven once 50% of TP range is reached
          • Force-close after MAX_HOLD_BARS
        """
        to_remove = []
        live_positions = await self.exchange.get_positions()
        live_symbols   = {p["symbol"] for p in live_positions}

        for symbol, pos in list(self._open_positions.items()):
            pos.bars_open += 1
            ticker = await self.exchange.fetch_ticker(symbol)
            if not ticker:
                continue

            current_price = ticker.get("last", pos.entry)

            # Check if position still open on exchange
            if symbol not in live_symbols:
                # Position was closed by TP or SL
                pnl = self._estimate_pnl(pos, current_price)
                reason = "TP/SL hit (exchange)"
                self.journal.close_trade(pos.trade_id, current_price, pnl, reason)
                self.strategy.record_trade_result(pnl > 0, abs(pnl / (pos.entry * 0.01 + 1e-9)))
                await self.telegram.send_trade_closed(
                    symbol, pos.direction, pos.entry, current_price, pnl, reason)
                to_remove.append(symbol)
                logger.info(f"[{symbol}] Closed: {reason} | PnL={pnl:.2f}")
                continue

            # Trailing SL — move to breakeven at 50% TP
            if not pos.be_moved:
                half_tp = pos.entry + 0.5 * (pos.tp - pos.entry) if pos.direction == "LONG" \
                          else pos.entry - 0.5 * (pos.entry - pos.tp)
                at_half = (pos.direction == "LONG"  and current_price >= half_tp) or \
                          (pos.direction == "SHORT" and current_price <= half_tp)
                if at_half:
                    await self.exchange.move_sl_to_breakeven(
                        symbol, pos.direction, pos.entry, pos.qty)
                    pos.be_moved = True
                    await self.telegram.send_breakeven_moved(symbol)

            # Force exit after MAX_HOLD_BARS
            if pos.bars_open >= self.s.MAX_HOLD_BARS:
                ok = await self.exchange.close_position(symbol, pos.direction, pos.qty)
                if ok:
                    await self.exchange.cancel_all_orders(symbol)
                    pnl = self._estimate_pnl(pos, current_price)
                    reason = f"Force close (max hold {self.s.MAX_HOLD_BARS} bars)"
                    self.journal.close_trade(pos.trade_id, current_price, pnl, reason)
                    self.strategy.record_trade_result(pnl > 0, 0)
                    await self.telegram.send_trade_closed(
                        symbol, pos.direction, pos.entry, current_price, pnl, reason)
                    to_remove.append(symbol)

        for sym in to_remove:
            self._open_positions.pop(sym, None)

    @staticmethod
    def _estimate_pnl(pos: ActivePosition, current_price: float) -> float:
        """Rough PnL estimate in USDT (leverage considered via qty)."""
        if pos.direction == "LONG":
            return (current_price - pos.entry) * pos.qty
        else:
            return (pos.entry - current_price) * pos.qty

    # ── Daily report ──────────────────────────────────────────────────────────

    async def _maybe_send_daily_report(self, now: datetime):
        report_hour = 0  # midnight UTC
        today_str = now.strftime("%Y-%m-%d")
        if now.hour == report_hour and self._last_daily_report != today_str:
            self._last_daily_report = today_str
            balance = await self.exchange.get_balance()
            ds = self.journal.daily_summary()
            await self.telegram.send_daily_summary(
                total_pnl   = ds.get("total_pnl", 0),
                win_rate    = ds.get("win_rate", 0),
                total_trades= ds.get("total", 0),
                balance     = balance,
                best_trade  = ds.get("best_str", "—"),
                worst_trade = ds.get("worst_str", "—"),
            )
            self.journal.print_summary()
