"""
main.py — Punto de entrada del Sniper Bot V49 Híbrido.

Flujo por ciclo:
  1. Obtener velas de Binance Futures
  2. Analizar con HybridStrategy (Sniper V49 + Kotegawa)
  3. Gestión de riesgo (can_trade, sizing, barreras)
  4. Ejecutar órdenes en Binance
  5. Notificar resultados a Telegram
  6. Heartbeat horario con resumen de estado
"""
import asyncio
import logging
import sys
from datetime import datetime, timezone

from config import Config
from bot.strategy import HybridStrategy, SignalResult
from bot.binance_client import BinanceClient
from bot.risk_manager import RiskManager, PositionState
from bot.telegram_notifier import TelegramNotifier
from bot.utils import setup_logging, timeframe_to_seconds

setup_logging("INFO")
logger = logging.getLogger("main")


class SniperBot:

    def __init__(self):
        self.cfg      = Config()
        self.client   = BinanceClient(
            self.cfg.BINANCE_API_KEY,
            self.cfg.BINANCE_SECRET_KEY,
            testnet=self.cfg.TESTNET
        )
        self.strategy  = HybridStrategy(self.cfg)
        self.risk      = RiskManager(self.cfg)
        self.notifier  = TelegramNotifier(
            self.cfg.TELEGRAM_TOKEN, self.cfg.TELEGRAM_CHAT_ID
        )
        # Estado de posiciones abiertas por símbolo
        self._pos_state: dict[str, PositionState] = {}
        self._last_signals: dict[str, SignalResult] = {}
        self._last_heartbeat: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._current_bar: int = 0
        self._daily_pnl: float = 0.0

    # ─────────────────────────────────────────
    # ARRANQUE
    # ─────────────────────────────────────────

    async def start(self) -> None:
        logger.info(f"Iniciando Sniper Bot — {self.cfg}")
        await self.client.connect()

        for symbol in self.cfg.SYMBOLS:
            await self.client.setup_symbol(symbol, self.cfg.LEVERAGE)

        await self.notifier.send_startup(self.cfg)
        await self._main_loop()

    # ─────────────────────────────────────────
    # BUCLE PRINCIPAL
    # ─────────────────────────────────────────

    async def _main_loop(self) -> None:
        while True:
            try:
                self._current_bar += 1
                balance = await self.client.get_balance()

                for symbol in self.cfg.SYMBOLS:
                    await self._process_symbol(symbol, balance)

                await self._maybe_heartbeat(balance)
                await asyncio.sleep(self.cfg.LOOP_INTERVAL)

            except KeyboardInterrupt:
                logger.info("Apagado por usuario")
                await self.notifier.send_paused("Apagado manual")
                break
            except Exception as e:
                logger.exception(f"Error en bucle principal: {e}")
                await self.notifier.send_error(str(e))
                await asyncio.sleep(30)

    # ─────────────────────────────────────────
    # PROCESADO POR SÍMBOLO
    # ─────────────────────────────────────────

    async def _process_symbol(self, symbol: str, balance: float) -> None:
        try:
            # ── 1. Obtener datos ──
            df = await self.client.get_klines(
                symbol, self.cfg.TIMEFRAME, limit=300
            )
            if df is None or len(df) < 150:
                logger.warning(f"{symbol}: datos insuficientes")
                return

            # ── 2. Analizar ──
            signal = self.strategy.analyze(df, symbol)
            self._last_signals[symbol] = signal

            # ── 3. Verificar posición existente ──
            position = await self.client.get_position(symbol)
            has_position = position and abs(position["size"]) > 0

            if has_position:
                await self._manage_open_position(symbol, position, signal, balance, df)
                return

            # ── 4. Buscar nueva entrada ──
            if not self.risk.can_trade(symbol):
                return
            if balance <= 0:
                logger.warning(f"{symbol}: balance cero")
                return

            if signal.long:
                await self._enter_long(symbol, signal, balance)
            elif signal.short:
                await self._enter_short(symbol, signal, balance)

        except Exception as e:
            logger.error(f"_process_symbol {symbol}: {e}", exc_info=True)

    # ─────────────────────────────────────────
    # GESTIÓN DE POSICIÓN ABIERTA
    # ─────────────────────────────────────────

    async def _manage_open_position(self, symbol: str, position: dict,
                                    signal: SignalResult, balance: float,
                                    df) -> None:
        """
        Revisa la barrera de tiempo (Binance gestiona TP/SL automáticamente).
        """
        state = self._pos_state.get(symbol)
        if state is None:
            # Posición abierta externamente — registrar
            tp, sl = self.risk.compute_barriers(
                position["entry_price"], signal.atr14, position["side"]
            )
            self._pos_state[symbol] = PositionState(
                symbol=symbol, side=position["side"],
                entry_price=position["entry_price"],
                quantity=abs(position["size"]),
                tp_price=tp, sl_price=sl,
                entry_bar=self._current_bar
            )
            return

        # Barrera de tiempo
        if self.risk.check_time_exit(state, self._current_bar):
            logger.info(f"{symbol}: barrera de tiempo — cerrando")
            result = await self.client.close_position(symbol, position)
            if result:
                pnl = await self.client.get_last_trade_pnl(symbol)
                pnl_pct = (pnl / balance * 100) if balance > 0 else 0.0
                self._daily_pnl += pnl
                self.risk.register_close(symbol, pnl_pct)
                del self._pos_state[symbol]
                await self.notifier.send_exit(
                    symbol, "TIME", pnl, pnl_pct, balance
                )

    # ─────────────────────────────────────────
    # ENTRADAS
    # ─────────────────────────────────────────

    async def _enter_long(self, symbol: str, signal: SignalResult,
                          balance: float) -> None:
        qty = self.risk.calculate_position_size(signal, balance)
        tp, sl = self.risk.compute_barriers(
            signal.entry_price, signal.atr14, "LONG"
        )
        order = await self.client.open_long(symbol, qty, tp, sl)
        if order:
            self.risk.register_open(symbol)
            self._pos_state[symbol] = PositionState(
                symbol=symbol, side="LONG",
                entry_price=signal.entry_price, quantity=qty,
                tp_price=tp, sl_price=sl,
                entry_bar=self._current_bar
            )
            await self.notifier.send_entry(symbol, "LONG", order, signal, balance)

    async def _enter_short(self, symbol: str, signal: SignalResult,
                           balance: float) -> None:
        qty = self.risk.calculate_position_size(signal, balance)
        tp, sl = self.risk.compute_barriers(
            signal.entry_price, signal.atr14, "SHORT"
        )
        order = await self.client.open_short(symbol, qty, tp, sl)
        if order:
            self.risk.register_open(symbol)
            self._pos_state[symbol] = PositionState(
                symbol=symbol, side="SHORT",
                entry_price=signal.entry_price, quantity=qty,
                tp_price=tp, sl_price=sl,
                entry_bar=self._current_bar
            )
            await self.notifier.send_entry(symbol, "SHORT", order, signal, balance)

    # ─────────────────────────────────────────
    # HEARTBEAT HORARIO
    # ─────────────────────────────────────────

    async def _maybe_heartbeat(self, balance: float) -> None:
        now = datetime.now(timezone.utc)
        diff = (now - self._last_heartbeat).total_seconds()
        if diff >= 3600:     # cada hora
            self._last_heartbeat = now
            await self.notifier.send_heartbeat(
                balance        = balance,
                daily_pnl      = self._daily_pnl,
                open_pos       = self.risk.open_positions,
                daily_loss_pct = self.risk.daily_loss_pct,
                symbols_status = self._last_signals
            )


# ──────────────────────────────────────────────
# ARRANQUE
# ──────────────────────────────────────────────

if __name__ == "__main__":
    bot = SniperBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Bot detenido")
        sys.exit(0)
