"""
Bot de arranque de impulso — BingX 30m.

Busca el ARRANQUE de un movimiento, no su continuación: compresión
previa + vela ancha con volumen + que el precio todavía NO esté
extendido. Entra largo y deja correr con un trailing por ATR.

Lo que lo diferencia de la ruptura de rango que ya se midió y falló
(482 operaciones, negativa en tres símbolos) es el último filtro: si
el precio ya se separó de su media más de MAX_STRETCH_AT_ENTRY, el
movimiento YA ocurrió y no se entra.

La salida es un trailing que solo sube. El stop que hay en BingX es el
que se envió con la entrada; el bot lleva su propio trailing y cierra a
mercado cuando lo pierde. Si el bot se cae, el stop original sigue vivo
en el exchange — peor precio, pero nunca una posición desnuda.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time

import httpx

import config
import journal
import strategy
from bingx import BingX, BingXError
from notify import State, Telegram

logging.basicConfig(
    level=getattr(logging, getattr(config, "LOG_LEVEL", "INFO"), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
if getattr(config, "LOG_LEVEL", "INFO") != "DEBUG":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("bot")



_DEFAULTS = {
    "TIMEFRAME": "30m", "ATR_LEN": 14, "MA_LEN": 20, "VOL_LEN": 20,
    "COMPRESSION_LEN": 12, "MAX_COMPRESSION_ATR": 3.0,
    "MIN_EXPANSION_ATR": 1.8, "MIN_CLOSE_POS": 0.66, "MIN_VOL_MULT": 2.0,
    "MAX_STRETCH_AT_ENTRY": 3.5,
    "SL_ATR": 0.5, "RR_TARGET": 4.0, "TRAIL_ATR": 2.0, "TRAIL_LOOKBACK": 3,
    "COST_ROUNDTRIP_PCT": 0.25, "MIN_ATR_PCT": 1.0, "MIN_COST_COVER": 6.0,
    "MAX_COST_IN_R": 0.20, "MAX_RISK_PCT": 4.0,
    "MIN_QUOTE_VOLUME_24H": 2_000_000.0, "SCAN_INTERVAL_SEC": 120,
    "MAX_SYMBOLS": 400, "SCAN_CONCURRENCY": 8, "SYMBOL_WHITELIST": [],
    "EXCLUDE_PREFIXES": ["NC"], "RISK_PCT": 0.25, "MAX_CONCURRENT": 2, "MAX_TOTAL_POSITIONS": 3,
    "LEVERAGE": 2, "MAX_CONSECUTIVE_LOSSES": 4, "COOLDOWN_MINUTES": 180,
    "ENTRY_TYPE": "MARKET", "LIMIT_OFFSET_PCT": 0.05,
    "SIGNAL_COOLDOWN_MIN": 120, "DAILY_SUMMARY": True,
    "DAILY_SUMMARY_HOUR_UTC": 7, "HEARTBEAT_HOURS": 12, "IDLE_ALERT_DAYS": 7,
    "STATE_PATH": "/data/state_impulse.json", "LOG_LEVEL": "INFO",
}


def ensure_config() -> list[str]:
    """Un config.py antiguo no puede tumbar un bot con dinero real."""
    faltan = []
    for nombre, valor in _DEFAULTS.items():
        if not hasattr(config, nombre):
            setattr(config, nombre, valor)
            faltan.append(nombre)
    return faltan


def fmt_signal(sig: strategy.Signal, live: bool) -> str:
    cabecera = "🟢 EJECUTADO" if live else "🔔 SEÑAL"
    nombre = sig.symbol.split("-")[0]
    riesgo = (sig.entry - sig.sl) / sig.entry * 100.0
    return (
        f"{cabecera} · LARGO <b>{nombre}</b>  (arranque de impulso)\n"
        f"Entrada <code>{sig.entry:.8g}</code>\n"
        f"SL <code>{sig.sl:.8g}</code>  ·  riesgo {riesgo:.2f}%\n"
        f"Compresión previa {sig.compresion:.1f} ATR · vela {sig.expansion:.1f} ATR\n"
        f"Volumen {sig.vol_mult:.1f}× · estirón {sig.stretch:+.1f} ATR (aún temprano)\n"
        f"ATR {sig.atr_pct:.2f}% · sale por trailing de {config.TRAIL_ATR} ATR"
    )


class Bot:
    def __init__(self) -> None:
        self.state = State(config.STATE_PATH)
        self.client = httpx.AsyncClient()
        self.api = BingX(self.client)
        self.tg = Telegram(self.client)
        self.symbols: list[str] = []
        self.volumes: dict[str, float] = {}
        self.live = config.is_live()
        self.last_heartbeat = time.time()
        self.sem = asyncio.Semaphore(config.SCAN_CONCURRENCY)
        self.journal = journal.Journal(
            os.path.join(os.path.dirname(config.STATE_PATH) or "/data", "operaciones_impulse-bot.csv")
        )

    async def start(self) -> None:
        faltan = ensure_config()
        if faltan:
            log.error("config.py desactualizado, faltaban: %s", ", ".join(faltan))
        log.info("Modo: %s", config.describe())
        await self.tg.send(
            f"🤖 <b>Bot de arranque de impulso iniciado</b>\n"
            f"{config.describe()}\n"
            f"Compresión ≤{config.MAX_COMPRESSION_ATR} ATR · vela ≥{config.MIN_EXPANSION_ATR} ATR\n"
            f"Volumen ≥{config.MIN_VOL_MULT}× · entrada solo si estirón ≤{config.MAX_STRETCH_AT_ENTRY} ATR\n"
            f"Timeframe {config.TIMEFRAME} · riesgo {config.RISK_PCT}% · trailing {config.TRAIL_ATR} ATR"
        )
        await self.refresh_symbols()
        while True:
            try:
                await self.reconcile()
                await self.manage_open()
                await self.maybe_daily_summary()
                await self.maybe_heartbeat()
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
        try:
            self.volumes = await self.api.tickers_24h()
            antes = len(syms)
            syms = [s for s in syms if self.volumes.get(s, 0.0) >= config.MIN_QUOTE_VOLUME_24H]
            log.info("Liquidez: %d de %d superan %.0f USDT", len(syms), antes, config.MIN_QUOTE_VOLUME_24H)
        except Exception as exc:  # noqa: BLE001
            log.warning("Sin filtro de liquidez (%s)", exc)
        self.symbols = syms[: config.MAX_SYMBOLS]
        log.info("Universo: %d símbolos", len(self.symbols))

    def in_cooldown(self) -> bool:
        return time.time() < float(self.state.data.get("cooldown_until", 0))

    async def _velas(self, sym: str) -> list[dict] | None:
        async with self.sem:
            try:
                return await self.api.klines(sym, config.TIMEFRAME, limit=400)
            except Exception:  # noqa: BLE001
                return None

    async def scan_once(self) -> None:
        if self.in_cooldown():
            return
        abiertas = len(self.state.data.get("open", {}))
        if abiertas >= config.MAX_CONCURRENT:
            return

        candidatos = 0
        motivos: dict[str, int] = {}
        for sym in self.symbols:
            if sym in self.state.data.get("open", {}):
                continue
            velas = await self._velas(sym)
            if not velas:
                continue
            sig, motivo = strategy.evaluate(sym, velas)
            if sig is None:
                # Se agrupa por CLASE de motivo, no por el texto exacto:
                # "sin señal (contador en 1...)" y "(contador en 0...)"
                # son el mismo caso y separarlos escondería el patrón.
                clave = motivo.split("(")[0].strip()
                motivos[clave] = motivos.get(clave, 0) + 1
                log.debug("%s: %s", sym, motivo)
                continue
            candidatos += 1
            await self.handle_signal(sig)
            abiertas += 1
            if abiertas >= config.MAX_CONCURRENT:
                break

        detalle = " · ".join(f"{k}: {v}" for k, v in sorted(motivos.items(), key=lambda x: -x[1])[:5])
        log.info("Ciclo completo · %d símbolos · %d señales | %s", len(self.symbols), candidatos, detalle)

        # Si el embudo se corta SIEMPRE en el mismo sitio, eso no es el
        # mercado: es un filtro mal calibrado. Se avisa una vez al día.
        if motivos:
            top = max(motivos.items(), key=lambda x: x[1])
            if top[1] >= len(self.symbols) * 0.9:
                hoy = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
                if self.state.data.get("warned_funnel") != hoy:
                    self.state.data["warned_funnel"] = hoy
                    self.state.save()
                    await self.tg.send(
                        f"🔻 <b>El embudo se corta siempre en el mismo punto</b>\n"
                        f"<code>{top[0]}</code> descarta {top[1]} de {len(self.symbols)} símbolos.\n"
                        f"Si es «señal, pero el SuperTrend sigue bajista», el filtro "
                        f"<code>REQUIRE_ST_BULL</code> está bloqueando la estrategia: un doble "
                        f"suelo ocurre POR DEFINICIÓN en caída, cuando el SuperTrend está bajista."
                    )

    async def handle_signal(self, sig: strategy.Signal) -> None:
        log.info("SEÑAL %s entrada=%.8g sl=%.8g rsi=%.1f", sig.symbol, sig.entry, sig.sl, sig.rsi)
        if not self.live:
            # En SIGNAL no se abre nada, así que la misma señal se
            # volvería a detectar en CADA ciclo mientras la vela siga
            # siendo la última cerrada: NEAR repetido cada 90 segundos.
            # Un enfriamiento por símbolo lo convierte en un aviso.
            ultimos = self.state.data.setdefault("last_signal", {})
            previo = float(ultimos.get(sig.symbol, 0) or 0)
            if time.time() - previo < config.SIGNAL_COOLDOWN_MIN * 60:
                log.debug("%s: señal repetida, en enfriamiento", sig.symbol)
                return
            ultimos[sig.symbol] = time.time()
            self.state.save()
            entregado = await self.tg.send(fmt_signal(sig, live=False))
            if not entregado:
                log.error(
                    "SEÑAL %s NO entregada por Telegram. Revisa TELEGRAM_CHAT_ID "
                    "en este servicio: el bot la detecta pero no puede avisarte.",
                    sig.symbol,
                )
            return

        client_id = f"rsi{sig.symbol.split('-')[0][:6]}{int(time.time())}"
        try:
            equity = await self.api.balance_usdt()
            if equity <= 0:
                await self.tg.send(f"⚠️ {sig.symbol} sin ejecutar: saldo 0")
                return
            qty = self.api.round_qty(sig.symbol, strategy.position_size(equity, sig.entry, sig.sl))
            minimo = self.api.min_qty(sig.symbol)
            if qty <= 0 or (minimo > 0 and qty < minimo):
                await self.tg.send(f"⚠️ {sig.symbol} sin ejecutar: tamaño {qty} bajo el mínimo {minimo}")
                return
            vivas = await self.api.open_positions()
            # Límite global: posiciones de TODA la cuenta, no solo de este bot.
            # En un desplome las alts se mueven juntas, así que lo que importa
            # es la exposición total y no cuántas abrió cada bot por su lado.
            n_total = sum(1 for p in vivas if float(p.get("positionAmt", 0) or 0) != 0)
            if n_total >= config.MAX_TOTAL_POSITIONS:
                await self.tg.send(
                    f"⚠️ <b>{sig.symbol}</b> sin abrir: ya hay <b>{n_total}</b> posiciones "
                    f"en la cuenta (límite global {config.MAX_TOTAL_POSITIONS}).\n"
                    f"<i>Puede haberlas abierto otro bot. En una caída las alts se mueven "
                    f"juntas, así que el riesgo se suma aunque los símbolos difieran.</i>"
                )
                return False
            if any(str(p.get("symbol")) == sig.symbol and float(p.get("positionAmt", 0) or 0) != 0 for p in vivas):
                return

            await self.api.set_leverage(sig.symbol, "LONG", config.LEVERAGE)
            sl_r = self.api.round_price(sig.symbol, sig.sl)
            tp_r = self.api.round_price(sig.symbol, sig.tp)
            if config.ENTRY_TYPE == "LIMIT":
                precio = self.api.round_price(sig.symbol, sig.entry * (1 - config.LIMIT_OFFSET_PCT / 100.0))
                await self.api.limit_order(sig.symbol, "BUY", qty, precio, sl_r, tp_r, client_id)
            else:
                await self.api.market_order(sig.symbol, "BUY", qty, sl_r, tp_r, client_id)
        except BingXError as exc:
            await self.tg.send(f"❌ BingX rechazó {sig.symbol}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            if await self.api.order_exists(sig.symbol, client_id):
                await self.tg.send(f"⚠️ {sig.symbol}: fallo de red pero la orden SÍ existe. Se registra.")
            else:
                await self.tg.send(f"❌ Error en {sig.symbol}: {exc}")
                return

        self.state.data.setdefault("open", {})[sig.symbol] = {
            "side": "BUY", "entry": sig.entry, "sl": sig.sl, "sl_inicial": sig.sl, "qty": qty,
            "opened_at": time.time(),
        }
        self.journal.abrir(sig, qty, "LIVE")
        self.state.data["last_trade_ts"] = time.time()
        self.state.save()
        await self.tg.send(fmt_signal(sig, live=True))

    async def reconcile(self) -> None:
        """
        Detecta las posiciones cerradas EN EL EXCHANGE que el bot no vio.

        Sin esto, cuando salta el stop la posición sigue "abierta" en el
        estado para siempre: bloquea el hueco de MAX_CONCURRENT y el
        circuit breaker no cuenta ni una pérdida.
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
        vivos = {
            str(p.get("symbol", "")) for p in posiciones
            if float(p.get("positionAmt", 0) or 0) != 0
        }
        for symbol, pos in list(abiertas.items()):
            if symbol in vivos:
                continue
            velas = await self._velas(symbol)
            ultimo = velas[-1]["close"] if velas else float(pos["entry"])
            gano = ultimo > float(pos["entry"])
            riesgo = abs(float(pos["entry"]) - float(pos.get("sl_inicial", pos["sl"])))
            r_real = (ultimo - float(pos["entry"])) / riesgo if riesgo > 0 else 0.0
            minutos = int((time.time() - float(pos.get("opened_at", time.time()))) / 60)
            self.journal.cerrar(symbol, "sl/tp", ultimo, r_real, minutos)
            await self.tg.send(
                f"{'✅' if gano else '🛑'} <b>{symbol.split('-')[0]}</b> cerrada en el exchange\n"
                f"Entrada {pos['entry']:.8g} → {ultimo:.8g}  ({r_real:+.2f} R)"
            )
            self.register_close(symbol, gano)

    async def manage_open(self) -> None:
        """
        Sube el stop siguiendo al SuperTrend y cierra cuando gira.

        El stop SOLO se mueve a favor. Reenviarlo al exchange en cada
        actualización es deliberado: si el bot se cae, el último stop
        enviado sigue protegiendo la posición.
        """
        abiertas = self.state.data.get("open", {})
        if not abiertas:
            return
        for symbol, pos in list(abiertas.items()):
            velas = await self._velas(symbol)
            if not velas:
                continue

            nuevo_stop = strategy.trailing_stop(velas, float(pos.get("sl", 0)))
            precio_actual = velas[-1]["close"]
            if precio_actual <= nuevo_stop:
                log.info("%s: trailing perdido, cierre", symbol)
                if self.live:
                    try:
                        await self.api.close_position(symbol, "BUY", float(pos.get("qty", 0)))
                    except Exception as exc:  # noqa: BLE001
                        await self.tg.send(f"⚠️ No se pudo cerrar {symbol}: {exc}")
                        continue
                gano = precio_actual > float(pos["entry"])
                await self.tg.send(
                    f"{'✅' if gano else '🛑'} <b>{symbol.split('-')[0]}</b> cerrada por trailing\n"
                    f"Entrada {pos['entry']:.8g} → {precio_actual:.8g}"
                )
                riesgo = abs(float(pos["entry"]) - float(pos.get("sl_inicial", pos["sl"])))
                r_real = (precio_actual - float(pos["entry"])) / riesgo if riesgo > 0 else 0.0
                minutos = int((time.time() - float(pos.get("opened_at", time.time()))) / 60)
                self.journal.cerrar(symbol, "trailing", precio_actual, r_real, minutos)
                self.register_close(symbol, gano)
                continue

            if nuevo_stop > float(pos.get("sl", 0)):
                pos["sl"] = nuevo_stop
                self.state.save()
                log.info("%s: trailing subido a %.8g", symbol, nuevo_stop)
                # El stop que hay en BingX es el que se envió con la
                # entrada. Actualizarlo requeriría cancelar y reemitir la
                # orden condicional, y una cancelación fallida dejaría la
                # posición DESPROTEGIDA unos segundos. Se prefiere el
                # stop original (más lejos, peor precio) a la ventana sin
                # red: el cierre por giro del SuperTrend lo hace el bot
                # igualmente en el ciclo siguiente.

    def register_close(self, symbol: str, won: bool) -> None:
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
                asyncio.create_task(self.tg.send(
                    f"⏸️ <b>Circuit breaker</b> · {config.MAX_CONSECUTIVE_LOSSES} pérdidas seguidas, "
                    f"pausa de {config.COOLDOWN_MINUTES} min."
                ))
        d.get("open", {}).pop(symbol, None)
        self.state.save()

    def stats_text(self) -> str:
        d = self.state.data
        n = d.get("closed_trades", 0)
        w = d.get("wins", 0)
        wr = (w / n * 100.0) if n else 0.0
        return (
            f"Cerradas: <b>{n}</b> · aciertos {w} ({wr:.0f}%)\n"
            f"Abiertas: {len(d.get('open', {}))} · racha: {d.get('consecutive_losses', 0)}"
        )

    async def maybe_daily_summary(self) -> None:
        if not config.DAILY_SUMMARY:
            return
        ahora = dt.datetime.now(dt.timezone.utc)
        hoy = ahora.strftime("%Y-%m-%d")
        if ahora.hour != config.DAILY_SUMMARY_HOUR_UTC or self.state.data.get("last_summary") == hoy:
            return
        self.state.data["last_summary"] = hoy
        self.state.save()
        await self.tg.send(
            f"📊 <b>Resumen diario RSI</b> · {hoy}\n{config.describe()}\n\n"
            f"{self.stats_text()}\nUniverso: {len(self.symbols)} símbolos"
        )

    async def maybe_heartbeat(self) -> None:
        if config.HEARTBEAT_HOURS <= 0:
            return
        if time.time() - self.last_heartbeat < config.HEARTBEAT_HOURS * 3600:
            return
        self.last_heartbeat = time.time()
        await self.tg.send(f"💓 Vivo (RSI) · {self.stats_text()}")


async def main() -> None:
    bot = Bot()
    try:
        await bot.start()
    finally:
        await bot.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
