"""
QF×JP Bot v6.3 — Position Manager
Monitorea posiciones abiertas en BingX:
- Detecta SL/TP alcanzados
- Mueve SL a breakeven
- Trailing stop
- Cierre de emergencia
- Sincroniza estado con RiskManager
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import config as C
from bingx_client import BingXClient
from risk_manager import RiskManager
import telegram_client as tg

log = logging.getLogger("position_mgr")


@dataclass
class OpenTrade:
    symbol: str
    direction: str       # LONG | SHORT
    entry: float
    sl: float
    tp1: float
    tp2: float
    qty: float
    atr: float
    order_id: str
    be_moved: bool = False
    tp1_hit: bool = False


class PositionManager:
    def __init__(self, client: BingXClient, risk: RiskManager):
        self.client = client
        self.risk   = risk
        self._trades: dict[str, OpenTrade] = {}   # symbol → OpenTrade
        self._lock = asyncio.Lock()

    async def reconcile_on_startup(self):
        """
        Al arrancar, consulta BingX y registra las posiciones ya abiertas
        para que el RiskManager no permita abrir más de las permitidas.
        Reconstruye un OpenTrade mínimo (sin ATR/SL/TP exactos) suficiente
        para el monitor y el contador de riesgo.
        """
        try:
            real_positions = await self.client.get_open_positions()
        except Exception as e:
            log.warning("reconcile_on_startup: no se pudo obtener posiciones: %s", e)
            return

        if not real_positions:
            log.info("reconcile_on_startup: sin posiciones abiertas en BingX")
            return

        count = 0
        for pos in real_positions:
            sym = pos.get("symbol", "")
            if not sym:
                continue

            amt = float(pos.get("positionAmt", 0) or 0)
            if amt == 0:
                continue

            direction = "LONG" if amt > 0 else "SHORT"
            entry     = float(pos.get("avgPrice",      pos.get("entryPrice", 0)) or 0)
            mark      = float(pos.get("markPrice",     entry) or entry)
            qty       = abs(amt)

            # SL/TP aproximados (1% como placeholder — el monitor los gestionará)
            sl  = entry * 0.99 if direction == "LONG" else entry * 1.01
            tp1 = entry * 1.015 if direction == "LONG" else entry * 0.985
            tp2 = entry * 1.03  if direction == "LONG" else entry * 0.97

            trade = OpenTrade(
                symbol=sym,
                direction=direction,
                entry=entry,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                qty=qty,
                atr=entry * 0.005,   # ATR estimado 0.5%
                order_id="reconciled",
                be_moved=False,
            )

            async with self._lock:
                self._trades[sym] = trade

            # NO incrementar aquí — update_open_count lo hará en el primer ciclo
            # con la cifra real de BingX, evitando doble conteo

            count += 1
            log.info(
                "[%s] Reconciliado: %s qty=%.4f entry=%.6f",
                sym, direction, qty, entry,
            )

        if count:
            log.info("reconcile_on_startup: %d posiciones reconciliadas", count)
            await tg.notify_error(
                "reconcile_startup",
                f"{count} posición(es) reconciliada(s) desde BingX tras redeploy",
            )

    async def register_trade(self, trade: OpenTrade):
        async with self._lock:
            self._trades[trade.symbol] = trade
        await self.risk.on_trade_opened()
        log.info("[%s] Trade registrado %s entry=%.6f", trade.symbol, trade.direction, trade.entry)

    async def remove_trade(self, symbol: str, pnl: float = 0.0):
        async with self._lock:
            self._trades.pop(symbol, None)
        await self.risk.on_trade_closed(pnl)

    # ── Loop principal ────────────────────────────────────────────────────────

    async def monitor_loop(self):
        """Corre en background. Verifica posiciones cada POSITION_CHECK_INTERVAL seg."""
        log.info("Position monitor iniciado (intervalo=%ds)", C.POSITION_CHECK_INTERVAL)
        while True:
            try:
                await self._check_all_positions()
            except Exception as e:
                log.error("monitor_loop error: %s", e)
                await tg.notify_error("position_monitor", str(e))
            await asyncio.sleep(C.POSITION_CHECK_INTERVAL)

    async def _check_all_positions(self):
        # Obtener posiciones reales de BingX
        try:
            real_positions = await self.client.get_open_positions()
        except Exception as e:
            log.warning("get_open_positions failed: %s", e)
            return

        # Construir mapa symbol → posición real
        real_map: dict[str, dict] = {}
        for pos in real_positions:
            sym = pos.get("symbol", "")
            if sym:
                real_map[sym] = pos

        # Sincronizar contador con la realidad de BingX
        # Usar max(len(real_map), len(tracked)) para no perder tracks locales
        await self.risk.update_open_count(len(real_map))

        async with self._lock:
            tracked = dict(self._trades)

        for symbol, trade in tracked.items():
            if symbol not in real_map:
                # La posición ya no existe en BingX (cerrada por SL/TP automático)
                try:
                    ticker = await self.client.get_ticker(symbol)
                    close_price = float(ticker.get("lastPrice", trade.entry))
                except Exception:
                    close_price = trade.entry

                pnl = self._calc_pnl(trade, close_price)
                reason = "sl_tp_auto"

                log.info("[%s] Posición cerrada externamente. PnL≈%.2f USDT", symbol, pnl)
                await tg.notify_trade_closed(
                    symbol, trade.direction, trade.entry, close_price, trade.qty, reason, pnl
                )
                await self.remove_trade(symbol, pnl)
                continue

            # Posición sigue abierta — analizar precio actual
            pos = real_map[symbol]
            try:
                mark_price = float(pos.get("markPrice", 0) or 0)
                if mark_price == 0:
                    ticker = await self.client.get_ticker(symbol)
                    mark_price = float(ticker.get("lastPrice", trade.entry))
            except Exception:
                continue

            if mark_price <= 0:
                continue

            # ── Breakeven ─────────────────────────────────────────────────────
            if not trade.be_moved:
                be_trigger = trade.entry + trade.atr * C.BREAKEVEN_ATR_MULT \
                    if trade.direction == "LONG" \
                    else trade.entry - trade.atr * C.BREAKEVEN_ATR_MULT

                be_reached = (trade.direction == "LONG" and mark_price >= be_trigger) or \
                             (trade.direction == "SHORT" and mark_price <= be_trigger)

                if be_reached:
                    await self._move_to_breakeven(trade, mark_price)

            # ── TP1 alcanzado manualmente (si las órdenes parciales fallan) ───
            if not trade.tp1_hit:
                tp1_hit = (trade.direction == "LONG" and mark_price >= trade.tp1) or \
                          (trade.direction == "SHORT" and mark_price <= trade.tp1)
                if tp1_hit:
                    trade.tp1_hit = True
                    log.info("[%s] TP1 alcanzado @ %.6f", symbol, mark_price)

    async def _move_to_breakeven(self, trade: OpenTrade, current_price: float):
        """Cancela SL original y coloca nuevo SL en entry (breakeven).
        Verifica que la posición sigue abierta antes de actuar."""
        try:
            # Verificar que la posición aún existe en BingX
            positions = await self.client.get_open_positions()
            symbols_open = {p.get("symbol","") for p in positions}
            if trade.symbol not in symbols_open:
                log.info("[%s] BE skip — posición ya cerrada en BingX", trade.symbol)
                await self.remove_trade(trade.symbol, 0.0)
                return

            await self.client.cancel_all_orders(trade.symbol)
            await asyncio.sleep(0.3)

            side_close = "SELL" if trade.direction == "LONG" else "BUY"
            resp = await self.client.place_stop_market_order(
                trade.symbol,
                side_close,
                trade.qty,
                trade.entry,
                trade.direction,
                close_position=True,
                order_type="STOP_MARKET",
            )
            if resp.get("code", -1) == 0:
                trade.be_moved = True
                log.info("[%s] SL movido a breakeven @ %.6f", trade.symbol, trade.entry)
            else:
                log.warning("[%s] Fallo al mover SL a BE: %s", trade.symbol, resp)
        except Exception as e:
            log.error("[%s] _move_to_breakeven error: %s", trade.symbol, e)

    async def close_position_emergency(self, symbol: str, reason: str = "emergency"):
        """Cierre forzado por mercado."""
        async with self._lock:
            trade = self._trades.get(symbol)

        if not trade:
            log.warning("[%s] close_emergency: trade no registrado", symbol)
            return

        try:
            await self.client.cancel_all_orders(symbol)
            await asyncio.sleep(0.2)
            resp = await self.client.close_position_market(symbol, trade.qty, trade.direction)

            ticker = await self.client.get_ticker(symbol)
            close_price = float(ticker.get("lastPrice", trade.entry))
            pnl = self._calc_pnl(trade, close_price)

            log.info("[%s] Cierre emergencia. PnL=%.2f USDT", symbol, pnl)
            await tg.notify_trade_closed(symbol, trade.direction, trade.entry, close_price, trade.qty, reason, pnl)
            await self.remove_trade(symbol, pnl)
        except Exception as e:
            log.error("[%s] close_emergency error: %s", symbol, e)
            await tg.notify_error(f"close_emergency({symbol})", str(e))

    def _calc_pnl(self, trade: OpenTrade, close_price: float) -> float:
        if trade.direction == "LONG":
            raw_pnl = (close_price - trade.entry) * trade.qty
        else:
            raw_pnl = (trade.entry - close_price) * trade.qty
        return round(raw_pnl * C.LEVERAGE, 4)

    def get_tracked(self) -> dict[str, OpenTrade]:
        return dict(self._trades)

    def is_trading(self, symbol: str) -> bool:
        return symbol in self._trades
