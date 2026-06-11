"""
position_manager.py — Gestor de posiciones abiertas (reescritura limpia)

Responsabilidades:
  1. Sincronizar el contador open_trades con el exchange en cada ciclo.
  2. Mover SL a breakeven cuando price >= entry + BREAKEVEN_ATR_MULT * ATR.
  3. Aplicar trailing stop (CB) si CB_ENABLED.
  4. Registrar cierre externo de posiciones (PnL final).

Principios de diseño:
  - ground_truth_first: el estado siempre viene del exchange, nunca de memoria local.
  - safe_amend: antes de cualquier amend, verificar que la posición sigue abierta.
  - idempotent_be: no mover SL si ya está en BE o más allá.
  - no_side_effects_on_error: las excepciones nunca corrompen el contador.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("position_manager")


# ---------------------------------------------------------------------------
# Config (leída de env, idéntica al resto del bot)
# ---------------------------------------------------------------------------

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "").lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return default


BREAKEVEN_ATR_MULT: float = _env_float("BREAKEVEN_ATR_MULT", 1.0)
CB_ENABLED: bool = _env_bool("CB_ENABLED", True)
CB_ATR_MULT: float = _env_float("CB_ATR_MULT", 3.0)
POSITION_CHECK_INTERVAL: int = int(_env_float("POSITION_CHECK_INTERVAL", 30))
MAX_OPEN_TRADES: int = int(_env_float("MAX_OPEN_TRADES", 10))


# ---------------------------------------------------------------------------
# Modelo de posición local (sólo para tracking de BE/CB ya aplicado)
# ---------------------------------------------------------------------------

@dataclass
class TrackedPosition:
    symbol: str
    side: str                   # "Buy" | "Sell"
    entry_price: float
    qty: float
    atr: float                  # ATR en el momento de apertura
    be_applied: bool = False    # ¿SL ya movido a BE?
    cb_sl: Optional[float] = None  # Nivel actual de trailing SL


# ---------------------------------------------------------------------------
# PositionManager
# ---------------------------------------------------------------------------

class PositionManager:
    """
    Gestiona todas las posiciones abiertas.

    Uso:
        pm = PositionManager(exchange_client, state)
        await pm.run()          # loop continuo
        # o llamada manual:
        await pm.check_once()
    """

    def __init__(self, exchange, state):
        """
        exchange : objeto con métodos async:
            get_open_positions() -> list[dict]   (bybit v5 structure)
            get_ticker(symbol)   -> dict  {'lastPrice': str, ...}
            amend_order(symbol, sl, tp)  -> dict | None
        state    : objeto compartido con atributos:
            open_trades (int)    — contador que usa el scanner
        """
        self.exchange = exchange
        self.state = state

        # Mapa local: symbol -> TrackedPosition
        # Solo para saber si ya aplicamos BE/CB, NO como fuente de verdad de cuántas hay
        self._tracked: dict[str, TrackedPosition] = {}

    # ------------------------------------------------------------------
    # Loop principal
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info("position_manager | loop iniciado (intervalo=%ds)", POSITION_CHECK_INTERVAL)
        while True:
            try:
                await self.check_once()
            except Exception as exc:
                logger.exception("position_manager | error inesperado en ciclo: %s", exc)
            await asyncio.sleep(POSITION_CHECK_INTERVAL)

    # ------------------------------------------------------------------
    # Un ciclo completo
    # ------------------------------------------------------------------

    async def check_once(self) -> None:
        """
        1. Obtiene posiciones abiertas del exchange (fuente de verdad).
        2. Actualiza state.open_trades.
        3. Detecta cierres externos y limpia tracking local.
        4. Para cada posición abierta: intenta BE y CB.
        """
        # --- 1. Fetch ground truth ---
        try:
            raw_positions = await self.exchange.get_open_positions()
        except Exception as exc:
            logger.error("position_manager | no se pudo obtener posiciones: %s", exc)
            return  # No tocar nada si el exchange falla

        # Filtrar posiciones con qty > 0 (bybit devuelve rows vacías a veces)
        open_positions: list[dict] = [
            p for p in raw_positions if float(p.get("size", 0)) > 0
        ]
        open_symbols: set[str] = {p["symbol"] for p in open_positions}

        # --- 2. Sincronizar contador (SIEMPRE desde el exchange) ---
        real_count = len(open_positions)
        if self.state.open_trades != real_count:
            logger.info(
                "position_manager | sincronizando open_trades: %d → %d",
                self.state.open_trades,
                real_count,
            )
            self.state.open_trades = real_count

        # --- 3. Detectar cierres externos ---
        closed_locally = set(self._tracked.keys()) - open_symbols
        for sym in closed_locally:
            tp = self._tracked.pop(sym)
            logger.info(
                "position_manager | cierre externo detectado: %s (entry=%.6f)",
                sym,
                tp.entry_price,
            )

        # --- 4. Registrar posiciones nuevas en tracking local ---
        for pos in open_positions:
            sym = pos["symbol"]
            if sym not in self._tracked:
                # Posición nueva: añadir al mapa local
                try:
                    tp = self._build_tracked(pos)
                    self._tracked[sym] = tp
                    logger.debug("position_manager | tracking iniciado: %s", sym)
                except Exception as exc:
                    logger.warning("position_manager | no se pudo trackear %s: %s", sym, exc)

        # --- 5. Revisar BE y CB para cada posición abierta ---
        tasks = [self._process_position(pos) for pos in open_positions]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for pos, result in zip(open_positions, results):
            if isinstance(result, Exception):
                logger.warning(
                    "position_manager | error procesando %s: %s",
                    pos["symbol"],
                    result,
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_tracked(self, pos: dict) -> TrackedPosition:
        """Construye un TrackedPosition desde un dict de bybit v5."""
        return TrackedPosition(
            symbol=pos["symbol"],
            side=pos["side"],               # "Buy" o "Sell"
            entry_price=float(pos["avgPrice"]),
            qty=float(pos["size"]),
            atr=float(pos.get("atr", 0)),   # El bot debe inyectar atr en pos o usar default
        )

    async def _get_current_price(self, symbol: str) -> Optional[float]:
        """Devuelve el último precio del exchange, o None si falla."""
        try:
            ticker = await self.exchange.get_ticker(symbol)
            return float(ticker["lastPrice"])
        except Exception as exc:
            logger.warning("position_manager | no se pudo obtener precio de %s: %s", symbol, exc)
            return None

    async def _verify_position_open(self, symbol: str) -> bool:
        """
        Consulta puntualmente si la posición sigue abierta.
        Evita el error 'position not exist' al intentar mover SL.
        """
        try:
            positions = await self.exchange.get_open_positions()
            for p in positions:
                if p["symbol"] == symbol and float(p.get("size", 0)) > 0:
                    return True
            return False
        except Exception as exc:
            logger.warning("position_manager | verify_position_open(%s) falló: %s", symbol, exc)
            # Asumir cerrada si no podemos verificar → seguro, evita error 109420
            return False

    # ------------------------------------------------------------------
    # Lógica de BE y CB
    # ------------------------------------------------------------------

    async def _process_position(self, pos: dict) -> None:
        """
        Intenta mover SL a BE y/o aplicar CB para una posición concreta.
        Verifica que la posición exista antes de cualquier amend.
        """
        symbol: str = pos["symbol"]
        side: str = pos["side"]                     # "Buy" | "Sell"
        entry: float = float(pos["avgPrice"])
        current_sl: float = float(pos.get("stopLoss", 0))
        current_tp: float = float(pos.get("takeProfit", 0))

        tp = self._tracked.get(symbol)
        if tp is None:
            return  # No tenemos info de ATR, ignorar

        if tp.atr <= 0:
            return  # Sin ATR no podemos calcular nada

        atr = tp.atr
        is_long = side == "Buy"

        # --- Precio actual ---
        price = await self._get_current_price(symbol)
        if price is None:
            return

        # --- Calcular niveles objetivo ---
        be_trigger = entry + BREAKEVEN_ATR_MULT * atr if is_long else entry - BREAKEVEN_ATR_MULT * atr
        be_sl = entry                                # SL exactamente en entry (sin fees para no sobrecomplicar)

        cb_distance = CB_ATR_MULT * atr
        cb_sl = price - cb_distance if is_long else price + cb_distance

        # --- Decidir si necesitamos mover el SL ---
        new_sl: Optional[float] = None

        # A) Breakeven
        if not tp.be_applied:
            if (is_long and price >= be_trigger) or (not is_long and price <= be_trigger):
                # ¿El SL ya está en BE o mejor? No mover.
                sl_already_ok = (
                    (is_long and current_sl >= be_sl) or
                    (not is_long and current_sl <= be_sl and current_sl > 0)
                )
                if not sl_already_ok:
                    new_sl = be_sl
                    logger.info(
                        "position_manager | [%s] BE activado: price=%.6f ≥ trigger=%.6f → SL→%.6f",
                        symbol, price, be_trigger, new_sl,
                    )
                tp.be_applied = True  # Marcar aunque el SL ya estuviera en BE

        # B) Chandelier / Trailing Stop (sólo si CB_ENABLED y BE ya aplicado)
        if CB_ENABLED and tp.be_applied:
            # Sólo subir (long) o bajar (short) el SL, nunca empeorarlo
            prev_cb = tp.cb_sl
            should_update_cb = (
                (is_long and (prev_cb is None or cb_sl > prev_cb)) or
                (not is_long and (prev_cb is None or cb_sl < prev_cb))
            )
            if should_update_cb:
                # Sólo aplicar si mejora respecto al SL actual del exchange
                better_than_exchange = (
                    (is_long and cb_sl > current_sl) or
                    (not is_long and (current_sl == 0 or cb_sl < current_sl))
                )
                if better_than_exchange:
                    new_sl = cb_sl
                    tp.cb_sl = cb_sl
                    logger.info(
                        "position_manager | [%s] CB trailing: price=%.6f → SL→%.6f",
                        symbol, price, new_sl,
                    )

        # --- Si no hay nada que mover, salir ---
        if new_sl is None:
            return

        # --- VERIFICAR que la posición sigue abierta ANTES del amend ---
        still_open = await self._verify_position_open(symbol)
        if not still_open:
            logger.info(
                "position_manager | [%s] ya no existe en exchange, skipping amend",
                symbol,
            )
            # Limpiar tracking local también
            self._tracked.pop(symbol, None)
            return

        # --- Ejecutar amend ---
        try:
            result = await self.exchange.amend_order(
                symbol=symbol,
                sl=round(new_sl, 6),
                tp=current_tp if current_tp > 0 else None,
            )
            if result:
                logger.info("position_manager | [%s] amend OK → SL=%.6f", symbol, new_sl)
            else:
                logger.warning("position_manager | [%s] amend devolvió None", symbol)
        except Exception as exc:
            err_str = str(exc)
            # Bybit code 109420 = position not exist → ya cerrada externamente
            # Bybit code 110406 = SL order already exists (idempotente, no es error real)
            if "109420" in err_str:
                logger.info(
                    "position_manager | [%s] position not exist (109420) — cerrada externamente",
                    symbol,
                )
                self._tracked.pop(symbol, None)
            elif "110406" in err_str:
                logger.debug(
                    "position_manager | [%s] SL order already exists (110406) — ignorando",
                    symbol,
                )
            else:
                logger.error("position_manager | [%s] amend falló: %s", symbol, exc)

    # ------------------------------------------------------------------
    # API pública de utilidad
    # ------------------------------------------------------------------

    def update_atr(self, symbol: str, atr: float) -> None:
        """
        Permite al scanner actualizar el ATR de una posición trackeada.
        Llamar después de abrir un trade: pm.update_atr('BTC-USDT', 45.3)
        """
        if symbol in self._tracked:
            self._tracked[symbol].atr = atr
        else:
            logger.debug("position_manager | update_atr: %s no está en tracking", symbol)

    def register_new_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        qty: float,
        atr: float,
    ) -> None:
        """
        El scanner puede llamar esto justo después de abrir un trade
        para que el ATR quede registrado antes del primer check.
        """
        self._tracked[symbol] = TrackedPosition(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            qty=qty,
            atr=atr,
        )
        logger.info(
            "position_manager | registrado nuevo trade: %s %s entry=%.6f atr=%.6f",
            symbol, side, entry_price, atr,
        )

    @property
    def open_count(self) -> int:
        """Devuelve el contador sincronizado (mismo que state.open_trades)."""
        return self.state.open_trades

    def can_open_trade(self) -> bool:
        """True si hay hueco para un trade más."""
        return self.state.open_trades < MAX_OPEN_TRADES
