"""
main.py — Sweep Reversal Map Bot — BingX

Mismo esqueleto que el bot wavelet (mismo bingx_client, misma forma de
reconciliar posiciones desde BingX, mismo patrón de batches), cambiando
solo el motor de señal (sweep_engine.replay_signal en vez de
wavelet_engine.compute_signal) y el cálculo de SL/TP (depende del
nivel barrido, no de un ATR simétrico).

DOS CORRECCIONES IMPORTANTES respecto a la versión anterior:

1. AFORO CON CERROJO. El límite de posiciones simultáneas se comprobaba
   contra un snapshot congelado al principio del ciclo y compartido por
   todos los hilos del batch. Con cientos de símbolos en paralelo, N
   señales del mismo batch leían todas "0 posiciones" y entraban todas:
   con el límite en 5 se llegaron a abrir 14. Ahora hay un contador vivo
   protegido por un Lock, y cada entrada RESERVA su plaza antes de
   mandar la orden.

2. ÁMBITO. Esta cuenta de BingX la comparten varios bots y operativa
   manual. El límite cuenta solo las posiciones que ESTE bot abrió; las
   ajenas no se cuentan, no se gestionan y no se notifican como cierres
   propios. Lo único que hacen las ajenas es bloquear su propio símbolo
   (no se abre encima de una posición existente, sea de quien sea).
"""

import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd

from bingx_client import BingXClient, BingXAPIError, ERR_POSITION_NOT_EXIST
from config import Config
import risk_manager
import sweep_engine
from state_manager import StateManager, timeframe_to_ms
from telegram_notifier import TelegramNotifier

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("sweep_bot.main")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_health_server(port: int) -> None:
    def _serve():
        try:
            HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()
        except OSError as exc:
            logger.warning("No se pudo levantar el servidor de salud en :%d (%s)", port, exc)

    threading.Thread(target=_serve, daemon=True).start()
    logger.info("Servidor de salud escuchando en :%d/health", port)


class Bot:
    def __init__(self):
        self.client = BingXClient(
            Config.BINGX_API_KEY, Config.BINGX_API_SECRET, Config.BINGX_BASE_URL,
            recv_window_ms=Config.BINGX_RECV_WINDOW_MS,
        )
        self.tg = TelegramNotifier(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)
        self.state = StateManager()
        self.timeframe_ms = timeframe_to_ms(Config.TIMEFRAME)
        self._contracts: dict[str, dict] = {}
        self._contracts_fetched_at = 0.0

        # Aforo: posiciones que ESTE bot tiene abiertas o está abriendo.
        # (symbol, positionSide) -> dict. Las plazas reservadas cuentan
        # desde antes de mandar la orden para que dos hilos no puedan
        # ocupar la misma.
        self._own_lock = threading.Lock()
        self._own: dict[tuple, dict] = {}
        # Símbolos ocupados por posiciones que NO son de este bot: no se
        # abre encima de ellas, pero tampoco cuentan para el aforo.
        self._foreign_symbols: set[str] = set()

    def refresh_contracts(self, force: bool = False) -> None:
        if not force and (time.time() - self._contracts_fetched_at) < 3600:
            return
        raw = self.client.get_contracts()
        contracts = {}
        for c in raw:
            symbol = c.get("symbol", "")
            if not symbol.endswith("-USDT") or int(c.get("status", 0)) != 1:
                continue
            contracts[symbol] = {
                "quantityPrecision": int(c.get("quantityPrecision", 4)),
                "pricePrecision": int(c.get("pricePrecision", 4)),
                "tradeMinQuantity": float(c.get("tradeMinQuantity", 0) or 0),
                "tradeMinUSDT": float(c.get("tradeMinUSDT", 0) or 0),
            }
        self._contracts = contracts
        self._contracts_fetched_at = time.time()
        logger.info("Contratos USDT-M activos: %d", len(contracts))

    def symbol_universe(self) -> list[str]:
        if Config.SYMBOLS.strip().upper() == "ALL":
            base = list(self._contracts.keys())
        else:
            base = [s.strip() for s in Config.SYMBOLS.split(",") if s.strip()]
        if Config.DEMO_MODE:
            return [s.replace("-USDT", "-VST") for s in base]
        return base

    def contract_meta(self, symbol: str) -> dict:
        key = symbol.replace("-VST", "-USDT")
        return self._contracts.get(key, {
            "quantityPrecision": 4, "pricePrecision": 4,
            "tradeMinQuantity": 0.0, "tradeMinUSDT": 0.0,
        })

    # ── Aforo ────────────────────────────────────────────────────────
    def _reserve_slot(self, symbol: str, side: str) -> bool:
        """Reserva plaza para una entrada. True si se puede abrir.

        Se llama ANTES de mandar la orden y dentro del cerrojo: es lo que
        impide que varios hilos del mismo batch pasen a la vez el control
        de aforo. Si la entrada falla, hay que llamar a _release_slot().
        """
        with self._own_lock:
            if symbol in self._foreign_symbols:
                return False
            if any(s == symbol for s, _ in self._own):
                return False
            if len(self._own) >= Config.MAX_CONCURRENT_POSITIONS:
                return False
            self._own[(symbol, side)] = {"reserved": True, "at": time.time()}
            return True

    def _release_slot(self, symbol: str, side: str) -> None:
        with self._own_lock:
            entry = self._own.get((symbol, side))
            if entry and entry.get("reserved"):
                self._own.pop((symbol, side), None)

    def _confirm_slot(self, symbol: str, side: str, info: dict) -> None:
        with self._own_lock:
            self._own[(symbol, side)] = {"reserved": False, **info}

    def own_count(self) -> int:
        with self._own_lock:
            return len(self._own)

    def reconcile_positions(self) -> dict:
        """Sincroniza el aforo con la realidad de BingX.

        Solo gestiona lo nuestro. Las posiciones ajenas se apuntan para
        no abrir encima de su símbolo, pero no se cuentan, no se cierran
        y no generan avisos de cierre.
        """
        try:
            positions = self.client.get_positions()
        except Exception as exc:
            logger.error("No se pudieron leer posiciones: %s", exc)
            with self._own_lock:
                return dict(self._own)

        current = {}
        for p in positions:
            amt = float(p.get("positionAmt", p.get("positionSize", 0)) or 0)
            if amt == 0:
                continue
            current[(p.get("symbol"), p.get("positionSide", "BOTH"))] = p

        cerradas = []
        with self._own_lock:
            for key, old in list(self._own.items()):
                if old.get("reserved"):
                    # Entrada en vuelo en otro hilo: no tocar.
                    continue
                if key not in current:
                    cerradas.append((key, old))
                    self._own.pop(key, None)
            propios = set(self._own.keys())
            self._foreign_symbols = {
                sym for (sym, _side) in current.keys()
                if not any(s == sym for s, _ in propios)
            }
            aforo = len(self._own)
            ajenas = len(self._foreign_symbols)

        for (symbol, side), old in cerradas:
            exit_price = old.get("markPrice") or old.get("avgPrice") or 0
            self.tg.exit_notice(symbol, side, float(exit_price or 0))
            logger.info("Posición propia cerrada: %s %s", symbol, side)

        logger.info("Aforo: %d/%d propias · %d posiciones ajenas en la cuenta (no se tocan)",
                    aforo, Config.MAX_CONCURRENT_POSITIONS, ajenas)

        with self._own_lock:
            return dict(self._own)

    def get_equity(self) -> float:
        try:
            bal = self.client.get_balance()
            for key in ("equity", "balance", "availableMargin"):
                if key in bal:
                    return float(bal[key])
            if isinstance(bal, list) and bal:
                return float(bal[0].get("equity", bal[0].get("balance", 0)))
        except Exception as exc:
            logger.error("No se pudo leer el balance: %s", exc)
        return 0.0

    def process_symbol(self, symbol: str, equity: float) -> None:
        try:
            if Config.SKIP_IF_SYMBOL_HAS_POSITION:
                with self._own_lock:
                    ocupado = (symbol in self._foreign_symbols
                               or any(s == symbol for s, _ in self._own))
                if ocupado:
                    return

            # Descarte barato ANTES de bajar velas: si el aforo está lleno
            # no tiene sentido pedir 300 klines de cada símbolo.
            if self.own_count() >= Config.MAX_CONCURRENT_POSITIONS:
                return

            # historial generoso: el replay necesita cubrir cualquier
            # sweep que pudiera seguir activo desde varias barras atrás
            candles = self.client.get_klines(
                symbol, Config.TIMEFRAME,
                limit=max(300, Config.MAX_CONFIRMATION_BARS + Config.STRUCTURE_LENGTH + Config.SWING_LENGTH * 4 + 100),
            )
            if len(candles) < 40:
                return

            now_ms = int(time.time() * 1000)
            if candles[-1]["time"] + self.timeframe_ms > now_ms:
                candles = candles[:-1]
            if not candles:
                return

            df = pd.DataFrame(candles)
            signal = sweep_engine.replay_signal(df, Config)
            if signal is None:
                return

            candle_time = signal["time"]
            if not self.state.can_signal(symbol, candle_time, 1, self.timeframe_ms):
                # cooldown mínimo de 1 barra: nunca proceses la misma
                # vela cerrada dos veces si el sondeo se solapa
                return

            # bearish y bullish son máquinas de estado independientes
            # (igual que en el Pine original): en teoría podrían
            # confirmar ambas en la misma barra. Sin lado claro, no se
            # entra en ninguna -- mejor que elegir una arbitrariamente.
            if signal["long_cond"] and signal["short_cond"]:
                logger.info("%s: long_cond y short_cond confirmados a la vez, señal ambigua, se descarta", symbol)
                return
            side = "LONG" if signal["long_cond"] else ("SHORT" if signal["short_cond"] else None)
            if side is None:
                return

            self.state.mark_signal(symbol, candle_time)
            self._handle_entry(symbol, side, signal, equity)

        except BingXAPIError as exc:
            if exc.code == ERR_POSITION_NOT_EXIST:
                return
            logger.warning("Error de API en %s: %s", symbol, exc)
        except Exception as exc:
            logger.exception("Error inesperado procesando %s: %s", symbol, exc)

    def _handle_entry(self, symbol: str, side: str, signal: dict, equity: float) -> None:
        meta = self.contract_meta(symbol)
        is_long = side == "LONG"
        entry_price = signal["close"]
        sl_price, tp_price = sweep_engine.compute_sweep_sl_tp(
            entry_price, is_long, signal["swept_level"], signal.get("atr"), Config,
        )

        # Red de seguridad ante SL/TP degenerados: en tokens de precio muy
        # bajo el redondeo puede dejar el SL igual a la entrada (riesgo 0,
        # división por cero en el sizing) o del lado equivocado, que se
        # dispararía nada más abrir.
        if sl_price <= 0 or tp_price <= 0 or sl_price == entry_price:
            logger.warning("%s: SL/TP inválidos tras el cálculo (SL=%s TP=%s entrada=%s), señal descartada",
                           symbol, sl_price, tp_price, entry_price)
            return
        if is_long and not (sl_price < entry_price < tp_price):
            logger.warning("%s LONG: orden de precios incoherente (SL=%s entrada=%s TP=%s), descartada",
                           symbol, sl_price, entry_price, tp_price)
            return
        if (not is_long) and not (tp_price < entry_price < sl_price):
            logger.warning("%s SHORT: orden de precios incoherente (SL=%s entrada=%s TP=%s), descartada",
                           symbol, sl_price, entry_price, tp_price)
            return

        if Config.MIN_BALANCE_USDT and equity < Config.MIN_BALANCE_USDT:
            self.tg.signal(symbol, side, entry_price, sl_price, tp_price, executed=False,
                            reason="balance por debajo del mínimo configurado")
            return

        sizing = risk_manager.compute_position_size(
            equity, Config.QTY_PCT, entry_price,
            meta["quantityPrecision"], meta["tradeMinQuantity"], meta["tradeMinUSDT"],
        )
        if not sizing.ok:
            self.tg.signal(symbol, side, entry_price, sl_price, tp_price, executed=False, reason=sizing.reason)
            return

        if not Config.LIVE_TRADING:
            self.tg.signal(symbol, side, entry_price, sl_price, tp_price, executed=False,
                            reason="LIVE_TRADING desactivado")
            return

        # Reserva de plaza: a partir de aquí el aforo ya cuenta esta
        # entrada, así que ningún otro hilo puede ocupar la misma.
        if not self._reserve_slot(symbol, side):
            self.tg.signal(symbol, side, entry_price, sl_price, tp_price, executed=False,
                            reason="máximo de posiciones simultáneas alcanzado")
            return

        try:
            leverage = None
            if not self.state.leverage_already_set(symbol):
                leverage = Config.LEVERAGE
                self.state.mark_leverage_set(symbol)

            # Apertura protegida: abre, lee el tamaño REAL rellenado,
            # coloca SL y TP por quantity (closePosition se rechaza en
            # Hedge con el error 109400, que es lo que dejaba las
            # posiciones abiertas y desnudas), verifica contra openOrders
            # y CIERRA si no hay stop.
            res = self.client.open_protected_position(
                symbol=symbol,
                position_side=side,
                quantity=sizing.quantity,
                stop_loss=sl_price,
                take_profit=tp_price,
                leverage=leverage,
            )

            if not res.get("ok"):
                self._release_slot(symbol, side)
                motivo = res.get("error") or "fallo desconocido en la apertura"
                if res.get("closed"):
                    motivo += " (posición cerrada, no quedó desprotegida)"
                self.tg.signal(symbol, side, entry_price, sl_price, tp_price,
                               executed=False, reason=motivo)
                logger.error("Entrada NO completada en %s %s: %s", symbol, side, motivo)
                return

            self._confirm_slot(symbol, side, {
                "quantity": res["quantity"], "entry_price": entry_price,
                "sl": sl_price, "tp": tp_price, "opened_at": time.time(),
            })

            aviso = "" if res.get("has_tp") else " ⚠️ SIN TP (con SL puesto)"
            self.tg.signal(symbol, side, entry_price, sl_price, tp_price, executed=True)
            logger.info("Entrada ejecutada: %s %s qty=%s @ %.6g (SL=%.6g TP=%.6g, barrido=%.6g)%s",
                        symbol, side, res["quantity"], entry_price, sl_price, tp_price,
                        signal["swept_level"], aviso)

        except Exception as exc:
            self._release_slot(symbol, side)
            logger.exception("Fallo al ejecutar la entrada en %s: %s", symbol, exc)
            self.tg.error(f"entrada {symbol} {side}", str(exc))

    def run(self) -> None:
        Config.validate()
        start_health_server(Config.HEALTH_PORT)
        logger.info("Iniciando bot.\n%s", Config.summary())
        self.tg.info("Bot iniciado.\n" + Config.summary())

        self.refresh_contracts(force=True)

        while True:
            cycle_start = time.time()
            try:
                self.refresh_contracts()
                self.reconcile_positions()
                equity = self.get_equity()
                symbols = self.symbol_universe()

                for i in range(0, len(symbols), Config.SYMBOL_BATCH_SIZE):
                    if self.own_count() >= Config.MAX_CONCURRENT_POSITIONS:
                        logger.info("Aforo lleno, se salta el resto del ciclo")
                        break
                    batch = symbols[i:i + Config.SYMBOL_BATCH_SIZE]
                    with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                        list(pool.map(lambda s: self.process_symbol(s, equity), batch))
                    time.sleep(Config.SYMBOL_BATCH_DELAY_SECONDS)

            except Exception as exc:
                logger.exception("Error en el ciclo principal: %s", exc)
                self.tg.error("ciclo principal", str(exc))

            elapsed = time.time() - cycle_start
            time.sleep(max(1.0, Config.POLL_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    Bot().run()
