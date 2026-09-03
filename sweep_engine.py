"""
sweep_engine.py — Puerto a Python del motor de "Sweep Reversal Map".

El script Pine compartido se cortó justo en medio del bloque bajista
("else if expireBearish: box.delete(...); line.delete(bearishLine" es
la última línea del archivo recibido) y nunca llegó a mostrar el
bloque alcista ni los plots/alertas. Lo que SÍ estaba completo:

  - detección de swing high/low (ta.pivothigh/pivotlow)
  - estructura local previa (highest/lowest de N barras, shift 1)
  - sweep bajista completo: disparo, tracking del extremo, "reclaim",
    confirmación (reclaim + cierre rompe estructura + displacement),
    caducidad por barras
  - las variables `bullish*` ya estaban declaradas en el mismo orden
    exacto que las `bearish*`, confirmando que es un espejo

Este motor reconstruye el bloque alcista como espejo exacto del
bajista (mismo patrón, ejes invertidos: swing low en vez de swing
high, sweep hacia abajo, ruptura de estructura hacia arriba). Si tu
versión completa del script difiere en algún detalle del lado
alcista, dime y lo ajusto.

DISEÑO PROPIO (no estaba en el Pine, que es un indicator() sin
gestión de trade): SL/TP para operar esto en real. SL más allá del
extremo barrido (si se re-toma, la tesis de reversión queda inválida)
+ colchón de ATR; TP a un múltiplo configurable de esa distancia de
riesgo (RR). Es una elección razonable pero no la única — ajústala
con SWEEP_SL_ATR_BUFFER / SWEEP_RR_RATIO si quieres otra cosa.

A diferencia de wavelet_engine (vectorizado con pandas, sin estado
entre llamadas), este motor SÍ tiene una máquina de estados que
persiste varias barras (un sweep puede tardar hasta
MAX_CONFIRMATION_BARS en confirmarse o caducar). En vez de guardar
ese estado en memoria entre sondeos (frágil: Railway puede reiniciar
el proceso en cualquier momento), se RE-REPRODUCE desde cero sobre
la ventana de velas de cada sondeo -- más caro en CPU que un cálculo
vectorizado, pero nunca depende de sobrevivir un reinicio.
"""

import numpy as np
import pandas as pd


def compute_atr(df: pd.DataFrame, length: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()  # RMA de Wilder


def _pivot(values: np.ndarray, idx: int, left: int, right: int, kind: str):
    """kind: 'high' o 'low'. Replica ta.pivothigh/pivotlow: el pivote se
    confirma `right` barras después del extremo real. Comparación
    ESTRICTA: el centro debe ser mayor/menor que TODO el resto de la
    ventana (no solo >=/<=), si no una meseta plana (varios valores
    exactamente iguales) dispara un pivote en cada barra de la meseta
    en vez de ninguno."""
    if idx < left + right:
        return None
    center = idx - right
    window = values[center - left: center + right + 1]
    candidate = values[center]
    others = np.delete(window, left)
    if kind == "high" and candidate > others.max():
        return candidate
    if kind == "low" and candidate < others.min():
        return candidate
    return None


def replay_signal(df: pd.DataFrame, params) -> dict | None:
    """
    df: DataFrame open/high/low/close/volume ordenado ascendente, SOLO
        velas cerradas.
    params: Config con SWING_LENGTH, SWEEP_ATR_LENGTH, STRUCTURE_LENGTH,
        MAX_CONFIRMATION_BARS, MIN_PENETRATION_ATR, MIN_DISPLACEMENT_ATR,
        SWEEP_SL_ATR_BUFFER, SWEEP_RR_RATIO.

    Devuelve None si no hay histórico suficiente. Si no, un dict con el
    estado de la ÚLTIMA vela cerrada: long_cond/short_cond (True solo si
    la confirmación ocurrió EXACTAMENTE en esa barra) y, si aplica,
    swept_level/structure_level para calcular SL/TP.
    """
    n = len(df)
    min_len = params.SWING_LENGTH * 2 + params.STRUCTURE_LENGTH + params.SWEEP_ATR_LENGTH + 5
    if n < min_len:
        return None

    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    open_ = df["open"].to_numpy()
    atr = compute_atr(df, params.SWEEP_ATR_LENGTH).to_numpy()

    swing = params.SWING_LENGTH
    struct_len = params.STRUCTURE_LENGTH
    max_confirm = params.MAX_CONFIRMATION_BARS
    min_pen_mult = params.MIN_PENETRATION_ATR
    min_disp_mult = params.MIN_DISPLACEMENT_ATR

    latest_swing_high = latest_swing_low = None
    swing_high_available = swing_low_available = False

    bearish_active = False
    bearish_reclaimed = False
    bearish_start = bearish_level = bearish_extreme = bearish_confirm_level = None

    bullish_active = False
    bullish_reclaimed = False
    bullish_start = bullish_level = bullish_extreme = bullish_confirm_level = None

    last = n - 1
    result_short = result_long = None

    start = max(swing * 2, struct_len) + 1
    for i in range(start, n):
        ph = _pivot(high, i, swing, swing, "high")
        if ph is not None:
            latest_swing_high = ph
            swing_high_available = True
        pl = _pivot(low, i, swing, swing, "low")
        if pl is not None:
            latest_swing_low = pl
            swing_low_available = True

        prior_structure_low = low[i - struct_len:i].min() if i >= struct_len else None
        prior_structure_high = high[i - struct_len:i].max() if i >= struct_len else None

        atr_i = atr[i]
        atr_valid = not np.isnan(atr_i) and atr_i > 0
        candle_body = abs(close[i] - open_[i])
        displacement_pass = atr_valid and candle_body >= atr_i * min_disp_mult
        min_penetration = (atr_i * min_pen_mult) if atr_valid else 0.0

        # ── Bajista: sweep de un swing high, confirma con ruptura de
        # estructura hacia ABAJO ──────────────────────────────────────
        bearish_sweep = (
            not bearish_active and swing_high_available and latest_swing_high is not None
            and prior_structure_low is not None and high[i] >= latest_swing_high + min_penetration
        )
        if bearish_sweep:
            bearish_active = True
            bearish_reclaimed = close[i] < latest_swing_high
            bearish_start = i
            bearish_level = latest_swing_high
            bearish_extreme = high[i]
            bearish_confirm_level = prior_structure_low
            swing_high_available = False

        if bearish_active:
            bearish_extreme = max(bearish_extreme, high[i])
            bearish_reclaimed = bearish_reclaimed or (close[i] < bearish_level)
            confirm = bearish_reclaimed and (close[i] < bearish_confirm_level) and displacement_pass
            expire = (i - bearish_start) > max_confirm
            if confirm:
                if i == last:
                    result_short = {"swept_level": bearish_extreme, "structure_level": bearish_confirm_level}
                bearish_active = False
            elif expire:
                bearish_active = False

        # ── Alcista (espejo exacto): sweep de un swing low, confirma con
        # ruptura de estructura hacia ARRIBA ────────────────────────────
        bullish_sweep = (
            not bullish_active and swing_low_available and latest_swing_low is not None
            and prior_structure_high is not None and low[i] <= latest_swing_low - min_penetration
        )
        if bullish_sweep:
            bullish_active = True
            bullish_reclaimed = close[i] > latest_swing_low
            bullish_start = i
            bullish_level = latest_swing_low
            bullish_extreme = low[i]
            bullish_confirm_level = prior_structure_high
            swing_low_available = False

        if bullish_active:
            bullish_extreme = min(bullish_extreme, low[i])
            bullish_reclaimed = bullish_reclaimed or (close[i] > bullish_level)
            confirm = bullish_reclaimed and (close[i] > bullish_confirm_level) and displacement_pass
            expire = (i - bullish_start) > max_confirm
            if confirm:
                if i == last:
                    result_long = {"swept_level": bullish_extreme, "structure_level": bullish_confirm_level}
                bullish_active = False
            elif expire:
                bullish_active = False

    out = {
        "time": int(df["time"].iloc[last]) if "time" in df.columns else None,
        "close": float(close[last]),
        "atr": float(atr[last]) if not np.isnan(atr[last]) else None,
        "long_cond": result_long is not None,
        "short_cond": result_short is not None,
    }
    if result_long:
        out.update({"swept_level": float(result_long["swept_level"]),
                     "structure_level": float(result_long["structure_level"])})
    if result_short:
        out.update({"swept_level": float(result_short["swept_level"]),
                     "structure_level": float(result_short["structure_level"])})
    return out


def compute_sweep_sl_tp(entry_price: float, is_long: bool, swept_level: float,
                         atr_value: float | None, params) -> tuple[float, float]:
    """
    Diseño propio (el indicador original no define trade management):
    SL más allá del extremo barrido (si se retoma, la reversión queda
    invalidada) + colchón de ATR; TP a params.SWEEP_RR_RATIO veces esa
    distancia de riesgo.
    """
    buffer = (atr_value or 0.0) * params.SWEEP_SL_ATR_BUFFER
    if is_long:
        sl = swept_level - buffer
        risk = entry_price - sl
        tp = entry_price + risk * params.SWEEP_RR_RATIO
    else:
        sl = swept_level + buffer
        risk = sl - entry_price
        tp = entry_price - risk * params.SWEEP_RR_RATIO
    return sl, tp
