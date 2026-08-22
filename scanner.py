"""
Escáner del universo completo de BingX.

Sustituye el trabajo manual de elegir diez símbolos a mano en el radar
de TradingView: recorre TODOS los perpetuos, mide amplitud y estado, y
publica un ranking.

Sobre el límite de peticiones: mil símbolos son mil llamadas. Se lanzan
con un semáforo y una pausa entre tandas. Si BingX empieza a devolver
429, baja CONCURRENCY antes que subir el intervalo — es la palanca que
no te cuesta frescura en los datos.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import config
import strategy
from bingx import BingX

log = logging.getLogger("scanner")


@dataclass
class Row:
    symbol: str
    atr_pct: float
    cover: float
    er_short: float
    er_long: float
    stretch: float
    state: str      # "en rango" | "estirado" | "fuera"
    verdict: str


def efficiency_ratio(closes: list[float], length: int) -> float:
    """Ratio de Kaufman: recorrido neto entre suma de movimientos."""
    if len(closes) < length + 1:
        return 0.0
    net = abs(closes[-1] - closes[-1 - length])
    total = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - length, len(closes)))
    return net / total if total > 0 else 0.0


def analyse(symbol: str, candles: list[dict]) -> Row | None:
    if len(candles) < 200:
        return None
    c = candles[:-1]  # la última está en curso
    closes = [x["close"] for x in c]
    highs = [x["high"] for x in c]
    lows = [x["low"] for x in c]

    ma = strategy.ema(closes, config.MA_LEN)
    a = strategy.atr(highs, lows, closes, config.ATR_LEN)
    if not ma or not a or a[-1] <= 0 or closes[-1] <= 0:
        return None

    atr_pct = a[-1] / closes[-1] * 100.0
    cover = atr_pct / config.COST_ROUNDTRIP_PCT if config.COST_ROUNDTRIP_PCT > 0 else 0.0
    stretch = (closes[-1] - ma[-1]) / a[-1]

    er_s = efficiency_ratio(closes, config.ER_SHORT)
    er_l = efficiency_ratio(closes, config.ER_LONG)

    hi = max(highs[-config.RANGE_LEN - 1 : -1])
    lo = min(lows[-config.RANGE_LEN - 1 : -1])
    if closes[-1] > hi or closes[-1] < lo:
        state = "fuera"
    elif abs(stretch) >= config.STRETCH_ATR:
        state = "estirado"
    else:
        state = "en rango"

    # Mismo criterio que el radar de TradingView: manda la amplitud.
    if atr_pct < config.MIN_ATR_PCT or cover < config.MIN_COST_COVER:
        verdict = "sin amplitud"
    elif state == "estirado":
        verdict = "REVERSIÓN"
    elif state == "fuera" and er_l >= config.ER_TREND:
        verdict = "RUPTURA"
    else:
        verdict = "en espera"

    return Row(symbol, atr_pct, cover, er_s, er_l, stretch, state, verdict)


class Scanner:
    def __init__(self, api: BingX) -> None:
        self.api = api
        self.sem = asyncio.Semaphore(config.SCAN_CONCURRENCY)
        self.last_run = 0.0

    async def _one(self, symbol: str) -> Row | None:
        async with self.sem:
            try:
                candles = await self.api.klines(symbol, config.TIMEFRAME, limit=300)
                return analyse(symbol, candles)
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: %s", symbol, exc)
                return None

    async def run(self, symbols: list[str]) -> list[Row]:
        t0 = time.time()
        results = await asyncio.gather(*(self._one(s) for s in symbols))
        rows = [r for r in results if r is not None]
        rows.sort(key=lambda r: r.cover, reverse=True)
        self.last_run = time.time()
        log.info(
            "Escaneo: %d/%d símbolos analizados en %.0fs · %d con amplitud",
            len(rows),
            len(symbols),
            time.time() - t0,
            sum(1 for r in rows if r.verdict != "sin amplitud"),
        )
        return rows


def format_ranking(rows: list[Row], top: int) -> str:
    con_amplitud = [r for r in rows if r.verdict != "sin amplitud"]
    if not con_amplitud:
        return (
            f"📡 <b>Escaneo BingX</b>\n"
            f"{len(rows)} símbolos analizados.\n"
            f"<b>Ninguno con amplitud suficiente</b> (≥{config.MIN_ATR_PCT}% y "
            f"≥{config.MIN_COST_COVER:.0f}× el coste).\n"
            f"Mercado tranquilo: no es un fallo, es que no hay nada que operar."
        )

    lineas = [f"📡 <b>Escaneo BingX</b> — {len(rows)} símbolos, {len(con_amplitud)} con amplitud\n"]
    for r in con_amplitud[:top]:
        marca = "🔶" if r.verdict == "REVERSIÓN" else "🟩" if r.verdict == "RUPTURA" else "·"
        base = r.symbol.split("-")[0]
        lineas.append(
            f"{marca} <b>{base}</b>  {r.atr_pct:.2f}% ({r.cover:.0f}×)  "
            f"ER {r.er_short:.2f}/{r.er_long:.2f}  {r.state} {r.stretch:+.1f}"
        )
    lineas.append("\n🔶 reversión lista · 🟩 ruptura lista · · con amplitud, en espera")
    return "\n".join(lineas)
