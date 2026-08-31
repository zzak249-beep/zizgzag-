"""
Motor: arranque de impulso ("la primera subida").

QUÉ LO DIFERENCIA DE LA RUPTURA DE RANGO QUE YA FALLÓ
La ruptura de rango simple —cerrar por encima del máximo de N velas—
se midió con 482 operaciones y perdió en los tres símbolos: -0.32R en
INDEXUS con 159 operaciones, -18% en CATE, -27% en JIMOTHY. El motivo
es que en un mercado picado el precio sale del rango constantemente y
casi todas esas salidas mueren en el primer retroceso.

Aquí se exigen CUATRO cosas a la vez, y las cuatro tienen que darse en
la misma vela:

  1. COMPRESIÓN PREVIA. Antes del arranque tiene que haber calma: el
     rango de las últimas N velas, medido en ATR, por debajo de un
     umbral. Un pump nace de la quietud; si el precio ya venía dando
     bandazos, la "ruptura" es una más del montón.
  2. EXPANSIÓN REAL. La vela que rompe debe ser mucho más ancha que el
     ATR y cerrar en el tercio alto de su propio rango. Una vela ancha
     que cierra por la mitad es indecisión, no arranque.
  3. VOLUMEN. Un pump sin volumen no es un pump: es un hueco en el
     libro. Se exige un múltiplo de la media reciente.
  4. QUE SEA PRONTO. Este es el filtro que de verdad separa esta
     estrategia de la anterior: si el precio ya está a más de X ATR de
     su media, el movimiento YA ocurrió y entrar es comprar el final.
     La ruptura de rango no miraba esto — por eso entraba tarde una y
     otra vez en las verticales.

SALIDA: trailing por ATR. Los pumps tienen colas largas — la mayoría
no va a ninguna parte y unos pocos corren muchísimo. Un objetivo fijo
cobraría 2R en los que iban a hacer 10R, que es donde está el dinero
de una estrategia de continuación.

ADVERTENCIA: cero operaciones medidas. El paquete incluye backtest.py;
mídelo antes de ponerle un euro.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import config

log = logging.getLogger("strategy")


@dataclass
class Signal:
    symbol: str
    side: str          # "BUY" — de momento solo al alza
    entry: float
    sl: float
    tp: float
    atr_pct: float
    compresion: float  # rango previo en ATR: cuanto menor, más limpia la salida
    expansion: float   # tamaño de la vela en ATR
    vol_mult: float
    stretch: float


def sma(values: list[float], length: int) -> list[float]:
    out, acc = [], 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= length:
            acc -= values[i - length]
        out.append(acc / min(i + 1, length))
    return out


def ema(values: list[float], length: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (length + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def atr(highs: list[float], lows: list[float], closes: list[float], length: int) -> list[float]:
    if len(closes) < 2:
        return []
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    out = [trs[0]]
    for tr in trs[1:]:
        out.append((out[-1] * (length - 1) + tr) / length)
    return out


def evaluate(symbol: str, candles: list[dict]) -> tuple[Signal | None, str]:
    need = config.COMPRESSION_LEN + config.ATR_LEN + config.MA_LEN + 10
    if len(candles) < need:
        return None, "pocas velas"

    c = candles[:-1]  # solo velas cerradas
    closes = [x["close"] for x in c]
    highs = [x["high"] for x in c]
    lows = [x["low"] for x in c]
    vols = [x["volume"] for x in c]

    a = atr(highs, lows, closes, config.ATR_LEN)
    ma = ema(closes, config.MA_LEN)
    if not a or not ma or a[-1] <= 0 or closes[-1] <= 0:
        return None, "sin indicadores"

    atr_pct = a[-1] / closes[-1] * 100.0
    cover = atr_pct / config.COST_ROUNDTRIP_PCT if config.COST_ROUNDTRIP_PCT > 0 else 0.0
    if atr_pct < config.MIN_ATR_PCT or cover < config.MIN_COST_COVER:
        return None, f"sin amplitud ({atr_pct:.2f}%, {cover:.0f}x)"

    # ── 1. Compresión previa (sin contar la vela de ruptura) ──────────
    ini = -1 - config.COMPRESSION_LEN
    rango_prev = max(highs[ini:-1]) - min(lows[ini:-1])
    compresion = rango_prev / a[-1] if a[-1] > 0 else 99.0
    if compresion > config.MAX_COMPRESSION_ATR:
        return None, f"sin compresión previa ({compresion:.1f} ATR)"

    # ── 2. Expansión de la vela ──────────────────────────────────────
    rango_vela = highs[-1] - lows[-1]
    expansion = rango_vela / a[-1] if a[-1] > 0 else 0.0
    if expansion < config.MIN_EXPANSION_ATR:
        return None, f"vela estrecha ({expansion:.1f} ATR)"

    pos_cierre = (closes[-1] - lows[-1]) / rango_vela if rango_vela > 0 else 0.5
    if pos_cierre < config.MIN_CLOSE_POS:
        return None, f"cierre débil en la vela ({pos_cierre:.2f})"

    if closes[-1] <= max(highs[ini:-1]):
        return None, "no supera el rango comprimido"

    # ── 3. Volumen ───────────────────────────────────────────────────
    vsma = sma(vols, config.VOL_LEN)
    vol_mult = vols[-1] / vsma[-1] if vsma and vsma[-1] > 0 else 0.0
    if vol_mult < config.MIN_VOL_MULT:
        return None, f"sin volumen ({vol_mult:.1f}x)"

    # ── 4. QUE SEA PRONTO ────────────────────────────────────────────
    # El filtro que separa esto de la ruptura de rango que ya falló.
    stretch = (closes[-1] - ma[-1]) / a[-1]
    if stretch > config.MAX_STRETCH_AT_ENTRY:
        return None, f"ya extendido ({stretch:.1f} ATR): el movimiento ya ocurrió"

    entrada = closes[-1]
    stop = min(lows[-1], lows[-2]) - a[-1] * config.SL_ATR
    riesgo = entrada - stop
    if riesgo <= 0:
        return None, "riesgo no válido"

    riesgo_pct = riesgo / entrada * 100.0
    coste_r = config.COST_ROUNDTRIP_PCT / riesgo_pct if riesgo_pct > 0 else 99.0
    if coste_r > config.MAX_COST_IN_R:
        return None, f"stop demasiado cerca (coste {coste_r:.2f}R)"
    if riesgo_pct > config.MAX_RISK_PCT:
        return None, f"stop demasiado lejos ({riesgo_pct:.1f}%)"

    return (
        Signal(
            symbol=symbol, side="BUY", entry=entrada, sl=stop,
            tp=entrada + riesgo * config.RR_TARGET,
            atr_pct=atr_pct, compresion=compresion, expansion=expansion,
            vol_mult=vol_mult, stretch=stretch,
        ),
        "ok",
    )


def trailing_stop(candles: list[dict], stop_actual: float) -> float:
    """
    Stop que solo sube. Los pumps corren o mueren rápido: dejar correr
    con un trailing es lo que permite que las pocas que vuelan paguen
    las muchas que no.
    """
    c = candles[:-1]
    a = atr([x["high"] for x in c], [x["low"] for x in c], [x["close"] for x in c], config.ATR_LEN)
    if not a:
        return stop_actual
    candidato = max(x["high"] for x in c[-config.TRAIL_LOOKBACK:]) - a[-1] * config.TRAIL_ATR
    return max(stop_actual, candidato)


def position_size(equity: float, entry: float, sl: float) -> float:
    riesgo = abs(entry - sl)
    if riesgo <= 0 or entry <= 0:
        return 0.0
    return (equity * config.RISK_PCT / 100.0) / riesgo
