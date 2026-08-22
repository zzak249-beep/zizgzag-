"""
Bot de reversión por sobreextensión — BingX USDT-M, 5 minutos.

Escanea, filtra por amplitud, busca el estirón y avisa (o ejecuta si
LIVE está confirmado dos veces).

Lo que este bot NO hace, a propósito:
  · No promedia a la baja.
  · No reentra tras un stop en el mismo símbolo hasta pasado el enfriamiento.
  · No opera sin stop: el SL viaja en la misma orden que la entrada.
  · No opera símbolos sin amplitud, por bonito que sea el patrón.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

import httpx

import config
import scanner
import strategy
from bingx import BingX, BingXError
from notify import State, Telegram

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# httpx registra en INFO CADA petición. Con ~940 símbolos por ciclo y un
# ciclo cada pocos minutos, son decenas de miles de líneas al día que
# entierran lo único que importa leer: los ciclos, las señales y los
# errores. Se silencia salvo que se pida DEBUG a propósito.
if config.LOG_LEVEL != "DEBUG":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("bot")


def fmt_signal(sig: strategy.Signal, live: bool) -> str:
    lado = "LARGO" if sig.side == "BUY" else "CORTO"
    cabecera = "🟢 EJECUTADO" if live else "🔔 SEÑAL"
    return (
        f"{cabecera} · {lado} <b>{sig.symbol}</b>\n"
        f"Entrada <code>{sig.entry:.8g}</code>\n"
        f"SL <code>{sig.sl:.8g}</code>  ·  TP <code>{sig.tp:.8g}</code>\n"
        f"R:R {sig.rr:.2f}  ·  estirón {sig.stretch:+.2f} ATR\n"
        f"ATR {sig.atr_pct:.2f}%  ·  {sig.cost_cover:.0f}× el coste"
    )


class Bot:
    def __init__(self) -> None:
        self.state = State(config.STATE_PATH)
        self.client = httpx.AsyncClient()
        self.api = BingX(self.client)
        self.tg = Telegram(self.client)
        self.symbols: list[str] = []
        self.live = config.is_live()
        self.scanner = scanner.Scanner(self.api)
        self.last_rank = 0.0
        self.volumes: dict[str, float] = {}
        self.last_heartbeat = time.time()

    async def start(self) -> None:
        log.info("Modo: %s", config.describe())
        await self.tg.send(
            f"🤖 <b>Bot de reversión iniciado</b>\n"
            f"{config.describe()}\n"
            f"Filtro de amplitud: ATR ≥ {config.MIN_ATR_PCT}% y ≥ {config.MIN_COST_COVER:.0f}× el coste\n"
            f"Riesgo por operación: {config.RISK_PCT}%"
        )
        await self.refresh_symbols()
        while True:
            try:
                await self.reconcile()
                await self.maybe_daily_summary()
                await self.maybe_heartbeat()
                await self.check_time_exits()
                await self.maybe_rank()
                await self.scan_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("Fallo en el ciclo: %s", exc)
            await asyncio.sleep(config.SCAN_INTERVAL_SEC)

    async def refresh_symbols(self) -> None:
        try:
            syms = await self.api.symbols()
        except Exception as exc:  # noqa: BLE001
            log.error("No se pudo listar símbolos: %s", exc)
            return
        if config.SYMBOL_WHITELIST:
            syms = [s for s in syms if s.split("-")[0].upper() in config.SYMBOL_WHITELIST]

        # Filtro de liquidez: filtrar por amplitud sin mirar el volumen es
        # cazar justo las monedas donde el libro es un colador.
        try:
            self.volumes = await self.api.tickers_24h()
            antes = len(syms)
            syms = [s for s in syms if self.volumes.get(s, 0.0) >= config.MIN_QUOTE_VOLUME_24H]
            log.info(
                "Liquidez: %d de %d símbolos superan %.0f USDT de volumen 24h",
                len(syms), antes, config.MIN_QUOTE_VOLUME_24H,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo filtrar por liquidez (%s): se sigue sin ese filtro", exc)
        self.symbols = syms if config.SCAN_ALL else syms[: config.MAX_SYMBOLS]
        log.info("Universo: %d símbolos", len(self.symbols))

    def stats_text(self) -> str:
        d = self.state.data
        cerradas = d.get("closed_trades", 0)
        wins = d.get("wins", 0)
        wr = (wins / cerradas * 100.0) if cerradas else 0.0
        abiertas = len(d.get("open", {}))
        enfr = ""
        if self.in_cooldown():
            mins = int((float(d["cooldown_until"]) - time.time()) / 60)
            enfr = f"\n⏸️ En enfriamiento {mins} min"
        return (
            f"Cerradas: <b>{cerradas}</b>  ·  aciertos {wins} ({wr:.0f}%)\n"
            f"Abiertas: {abiertas}  ·  racha de pérdidas: {d.get('consecutive_losses', 0)}"
            f"{enfr}"
        )

    async def maybe_daily_summary(self) -> None:
        """Un resumen al día. Sin esto solo te enteras cuando hay señal,
        y el silencio no distingue entre 'no hay nada' y 'está caído'."""
        if not config.DAILY_SUMMARY:
            return
        ahora = dt.datetime.now(dt.timezone.utc)
        hoy = ahora.strftime("%Y-%m-%d")
        if ahora.hour != config.DAILY_SUMMARY_HOUR_UTC:
            return
        if self.state.data.get("last_summary") == hoy:
            return
        self.state.data["last_summary"] = hoy
        self.state.save()
        entregado = await self.tg.send(
            f"📊 <b>Resumen diario</b> · {hoy}\n"
            f"{config.describe()}\n\n"
            f"{self.stats_text()}\n"
            f"Universo: {len(self.symbols)} símbolos"
        )
        if not entregado:
            log.error("El resumen diario NO se entregó: revisa las credenciales de Telegram")

    async def maybe_heartbeat(self) -> None:
        """Señal de vida periódica: si deja de llegar, el bot está caído."""
        if config.HEARTBEAT_HOURS <= 0:
            return
        if time.time() - self.last_heartbeat < config.HEARTBEAT_HOURS * 3600:
            return
        self.last_heartbeat = time.time()
        await self.tg.send(f"💓 Vivo · {self.stats_text()}")

    async def reconcile(self) -> None:
        """
        BUG QUE ESTO ARREGLA: el bot abría posiciones y las guardaba en
        el estado, pero NADIE registraba los cierres. Cuando saltaba el
        SL o el TP en el exchange, el bot no se enteraba: la posición
        seguía "abierta" para siempre, bloqueando el hueco de
        MAX_CONCURRENT y dejando el circuit breaker sin contar ni una
        pérdida. En SIGNAL no se nota; en LIVE el bot se habría quedado
        mudo y bloqueado tras las primeras operaciones.

        El resultado (ganada/perdida) se estima comparando el último
        precio con la entrada. Es una APROXIMACIÓN — el fill real puede
        diferir — y sirve para el circuit breaker, no para contabilidad.
        """
        if not self.live:
            return
        abiertas = self.state.data.get("open", {})
        if not abiertas:
            return
        try:
            posiciones = await self.api.open_positions()
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudieron leer las posiciones: %s", exc)
            return

        vivos = {str(p.get("symbol", "")) for p in posiciones if float(p.get("positionAmt", 0) or 0) != 0}
        for symbol, pos in list(abiertas.items()):
            if symbol in vivos:
                continue
            # Ya no está en el exchange: se cerró por SL o por TP.
            try:
                velas = await self.api.klines(symbol, config.TIMEFRAME, limit=2)
                ultimo = velas[-1]["close"] if velas else pos["entry"]
            except Exception:  # noqa: BLE001
                ultimo = pos["entry"]
            if pos["side"] == "BUY":
                gano = ultimo > pos["entry"]
            else:
                gano = ultimo < pos["entry"]
            log.info("%s cerrada fuera del bot (%s)", symbol, "ganada" if gano else "perdida")
            await self.tg.send(
                f"{'✅' if gano else '🛑'} <b>{symbol}</b> cerrada · "
                f"{'objetivo' if gano else 'stop'}\n{self.stats_text()}"
            )
            self.register_close(symbol, gano)

    async def check_time_exits(self) -> None:
        """
        Cierra las posiciones que llevan demasiado tiempo abiertas.
        El SL y el TP los vigila el exchange; el reloj lo vigila el bot,
        porque el exchange no sabe nada de la ventana en la que la
        estrategia tiene sentido.
        """
        if not config.USE_TIME_EXIT:
            return
        limite = config.max_trade_seconds()
        ahora = time.time()
        for symbol, pos in list(self.state.data.get("open", {}).items()):
            edad = ahora - float(pos.get("opened_at", ahora))
            if edad < limite:
                continue
            log.info("%s lleva %.0f min abierta: cierre por tiempo", symbol, edad / 60)
            if self.live:
                try:
                    qty = float(pos.get("qty", 0))
                    if qty > 0:
                        await self.api.close_position(symbol, pos["side"], qty)
                except Exception as exc:  # noqa: BLE001
                    await self.tg.send(f"⚠️ No se pudo cerrar {symbol} por tiempo: {exc}")
                    continue
            self.state.data["open"].pop(symbol, None)
            self.state.save()
            await self.tg.send(
                f"⏱️ <b>{symbol}</b> cerrada por tiempo tras {edad / 60:.0f} min "
                f"(límite {limite // 60} min)\nLa vuelta no llegó: fuera de la ventana útil."
            )

    async def maybe_rank(self) -> None:
        """Ranking del universo completo, cada RANK_INTERVAL_MIN."""
        if not config.SCAN_ALL:
            return
        if time.time() - self.last_rank < config.RANK_INTERVAL_MIN * 60:
            return
        self.last_rank = time.time()
        rows = await self.scanner.run(self.symbols)
        await self.tg.send(scanner.format_ranking(rows, config.RANK_TOP_N))

    def in_cooldown(self) -> bool:
        return time.time() < float(self.state.data.get("cooldown_until", 0))

    async def scan_once(self) -> None:
        if self.in_cooldown():
            restante = int(float(self.state.data["cooldown_until"]) - time.time()) // 60
            log.info("En enfriamiento, %d min restantes", restante)
            return

        abiertas = len(self.state.data.get("open", {}))
        if abiertas >= config.MAX_CONCURRENT:
            log.info("Límite de posiciones alcanzado (%d)", abiertas)
            return

        con_amplitud = 0
        for sym in self.symbols:
            try:
                velas = await self.api.klines(sym, config.TIMEFRAME, limit=300)
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: sin velas (%s)", sym, exc)
                continue

            sig, motivo = strategy.evaluate(sym, velas)
            if not motivo.startswith("sin amplitud"):
                con_amplitud += 1
            if sig is None:
                log.debug("%s: %s", sym, motivo)
                continue
            if sym in self.state.data.get("open", {}):
                continue

            await self.handle_signal(sig)
            abiertas += 1
            if abiertas >= config.MAX_CONCURRENT:
                break
            await asyncio.sleep(0.2)  # respiro entre llamadas

        log.info(
            "Ciclo completo · %d símbolos, %d con amplitud suficiente",
            len(self.symbols),
            con_amplitud,
        )

    async def handle_signal(self, sig: strategy.Signal) -> None:
        log.info(
            "SEÑAL %s %s entrada=%.8g sl=%.8g tp=%.8g rr=%.2f atr=%.2f%%",
            sig.side, sig.symbol, sig.entry, sig.sl, sig.tp, sig.rr, sig.atr_pct,
        )

        if not self.live:
            await self.tg.send(fmt_signal(sig, live=False))
            self.state.data.setdefault("last_signal", {})[sig.symbol] = time.time()
            self.state.save()
            return

        try:
            equity = await self.api.balance_usdt()
            if equity <= 0:
                await self.tg.send(f"⚠️ Señal en {sig.symbol} sin ejecutar: saldo 0")
                return
            qty = strategy.position_size(equity, sig.entry, sig.sl)
            if qty <= 0:
                await self.tg.send(f"⚠️ Señal en {sig.symbol} sin ejecutar: tamaño 0")
                return
            await self.api.set_leverage(sig.symbol, "LONG" if sig.side == "BUY" else "SHORT", config.LEVERAGE)
            if config.ENTRY_TYPE == "LIMIT":
                # Se pide un pelín MEJOR que el precio actual: en un libro
                # fino no entrar es mejor que entrar a cualquier precio.
                ajuste = 1 + config.LIMIT_OFFSET_PCT / 100.0
                precio = sig.entry * (ajuste if sig.side == "SELL" else 2 - ajuste)
                await self.api.limit_order(sig.symbol, sig.side, qty, precio, sig.sl, sig.tp)
            else:
                await self.api.market_order(sig.symbol, sig.side, qty, sig.sl, sig.tp)
        except BingXError as exc:
            await self.tg.send(f"❌ BingX rechazó la orden en {sig.symbol}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            await self.tg.send(f"❌ Error al ejecutar {sig.symbol}: {exc}")
            return

        self.state.data.setdefault("open", {})[sig.symbol] = {
            "side": sig.side,
            "entry": sig.entry,
            "sl": sig.sl,
            "tp": sig.tp,
            "qty": qty,
            "opened_at": time.time(),
        }
        self.state.save()
        await self.tg.send(fmt_signal(sig, live=True))

    def register_close(self, symbol: str, won: bool) -> None:
        """
        Lo llama el reconciliador cuando detecta una posición cerrada.
        El circuit breaker cuenta RACHAS, no dinero: el bot no lleva la
        contabilidad en euros del exchange y fingir un drawdown en % con
        datos que no tiene sería inventarse una cifra.
        """
        d = self.state.data
        d["closed_trades"] = d.get("closed_trades", 0) + 1
        if won:
            d["wins"] = d.get("wins", 0) + 1
            d["consecutive_losses"] = 0
        else:
            d["losses"] = d.get("losses", 0) + 1
            d["consecutive_losses"] = d.get("consecutive_losses", 0) + 1
            if d["consecutive_losses"] >= config.MAX_CONSECUTIVE_LOSSES:
                d["cooldown_until"] = time.time() + config.COOLDOWN_MINUTES * 60
                d["consecutive_losses"] = 0
                asyncio.create_task(
                    self.tg.send(
                        f"⏸️ <b>Circuit breaker</b>\n"
                        f"{config.MAX_CONSECUTIVE_LOSSES} pérdidas seguidas · "
                        f"pausa de {config.COOLDOWN_MINUTES} min.\n"
                        f"No es un fallo: es el bot dejando de insistir."
                    )
                )
        d.get("open", {}).pop(symbol, None)
        self.state.save()


async def main() -> None:
    bot = Bot()
    try:
        await bot.start()
    finally:
        await bot.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
