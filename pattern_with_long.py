"""
Deteccion del patron "tres montañas, tercer techo descendente, empuje debil,
ruptura fuerte" descrito por el usuario -- variante informal de un triple
techo clasico, NO un patron con validacion estadistica publicada que yo haya
podido verificar. Construido como descripcion estructural razonable de lo
que se dibujo a mano sobre el grafico de XAUUSD, no como una regla con
edge probado. Tratar los resultados de esto con la MISMA disciplina que
el resto de la sesion: es una hipotesis para poner a prueba, no una
certeza.

Los tres picos:
  - Peak1, Peak2: dos maximos recientes dentro de una tolerancia el uno del
    otro -- definen una "zona de resistencia" (no un nivel exacto).
  - Peak3: un tercer maximo MAS BAJO que la zona -- "tercer techo
    descendente", el mercado ya no logra alcanzar la resistencia previa.
  - "Empuje debil": el volumen promedio en la subida hacia Peak3 es menor
    que el de las subidas hacia Peak1/Peak2 -- interpretacion del "ventas
    debiles" del usuario como participacion mas baja en el ultimo intento.

Disparo (no la forma sola): el precio debe CERRAR por debajo del valle
entre Peak2 y Peak3 -- confirmacion de ruptura real, no solo la silueta.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, NamedTuple


class Candle(NamedTuple):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: Optional[int] = None


@dataclass
class Pivot:
    index: int          # indice dentro de la lista de velas
    price: float
    open_time: int


@dataclass
class ThreeMountainsPattern:
    peak1: Pivot
    peak2: Pivot
    peak3: Pivot
    trough_between_2_3: Pivot     # el valle mas bajo entre peak2 y peak3
    resistance_zone_high: float   # max(peak1, peak2)
    resistance_zone_low: float    # min(peak1, peak2)
    weak_push_confirmed: bool     # volumen hacia peak3 < volumen hacia peak1/2
    vol_ratio: float              # volumen_hacia_peak3 / volumen_promedio_hacia_peak1_peak2


def compute_atr(candles: List[Candle], length: int = 14) -> float:
    """ATR simple (SMA de true range), suficiente para un colchon de SL --
    no hace falta la suavizacion de Wilder para este uso."""
    if len(candles) < length + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    recent = trs[-length:]
    return sum(recent) / len(recent) if recent else 0.0


def find_pivot_highs(candles: List[Candle], pivot_len: int) -> List[Pivot]:
    """Pivote alto clasico: pivot_len velas a cada lado, todas con high
    menor. Solo pivotes ya CONFIRMADOS (con pivot_len velas despues
    disponibles) -- nunca el ultimo tramo del historial, que podria
    cambiar en la proxima vela."""
    pivots = []
    n = len(candles)
    for i in range(pivot_len, n - pivot_len):
        h = candles[i].high
        is_pivot = True
        for j in range(i - pivot_len, i + pivot_len + 1):
            if j == i:
                continue
            if candles[j].high >= h:
                is_pivot = False
                break
        if is_pivot:
            pivots.append(Pivot(index=i, price=h, open_time=candles[i].open_time))
    return pivots


def find_trough_between(candles: List[Candle], idx_start: int, idx_end: int) -> Optional[Pivot]:
    """Minimo simple (no pivote formal) entre dos indices -- el valle que
    se rompe para confirmar la señal no necesita ser un pivote de
    pivot_len completo, solo el punto mas bajo del tramo."""
    if idx_end <= idx_start:
        return None
    segment = candles[idx_start:idx_end + 1]
    if not segment:
        return None
    min_candle = min(segment, key=lambda c: c.low)
    min_idx = idx_start + segment.index(min_candle)
    return Pivot(index=min_idx, price=min_candle.low, open_time=min_candle.open_time)


def avg_volume_between(candles: List[Candle], idx_start: int, idx_end: int) -> float:
    if idx_end <= idx_start:
        return 0.0
    segment = candles[idx_start:idx_end + 1]
    if not segment:
        return 0.0
    return sum(c.volume for c in segment) / len(segment)


def detect_three_mountains(
    candles: List[Candle],
    pivot_len: int = 3,
    zone_tolerance_pct: float = 0.5,
    peak3_below_zone_pct_min: float = 0.15,
    require_weak_push: bool = True,
    weak_push_max_ratio: float = 0.85,
) -> Optional[ThreeMountainsPattern]:
    """
    Busca el patron usando los ULTIMOS 3 pivot highs disponibles en la
    lista de velas (mas antigua -> mas reciente). Devuelve None si no hay
    suficientes pivotes o si no cumplen los criterios.

    zone_tolerance_pct: cuanto pueden diferir peak1 y peak2 entre si (%)
        para contar como "la misma zona de resistencia".
    peak3_below_zone_pct_min: cuanto debe quedar peak3 POR DEBAJO del
        borde inferior de la zona, como minimo, para contar como
        "tercer techo descendente" real y no solo ruido.
    weak_push_max_ratio: el volumen promedio de la subida hacia peak3
        debe ser <= este ratio del volumen promedio de las subidas hacia
        peak1/peak2 para confirmar "empuje debil".
    """
    pivots = find_pivot_highs(candles, pivot_len)
    if len(pivots) < 3:
        return None

    peak1, peak2, peak3 = pivots[-3], pivots[-2], pivots[-1]

    zone_high = max(peak1.price, peak2.price)
    zone_low = min(peak1.price, peak2.price)
    if zone_high <= 0:
        return None
    zone_diff_pct = (zone_high - zone_low) / zone_high * 100.0
    if zone_diff_pct > zone_tolerance_pct:
        return None  # peak1/peak2 no estan lo bastante cerca como para ser "la misma zona"

    peak3_below_pct = (zone_low - peak3.price) / zone_low * 100.0
    if peak3_below_pct < peak3_below_zone_pct_min:
        return None  # peak3 no quedo genuinamente por debajo de la zona

    trough = find_trough_between(candles, peak2.index, peak3.index)
    if trough is None:
        return None

    # Empuje hacia peak3: desde el valle entre peak2/peak3, hasta peak3.
    vol_push_to_3 = avg_volume_between(candles, trough.index, peak3.index)
    # Empuje de referencia: aproximacion via el tramo hacia peak1 y peak2
    # (desde pivot_len velas antes de cada uno, que es donde arranca el
    # ultimo tramo alcista visible antes del pivote).
    ref_start_1 = max(0, peak1.index - pivot_len * 3)
    ref_start_2 = max(0, peak2.index - pivot_len * 3)
    vol_push_1 = avg_volume_between(candles, ref_start_1, peak1.index)
    vol_push_2 = avg_volume_between(candles, ref_start_2, peak2.index)
    vol_ref = (vol_push_1 + vol_push_2) / 2.0 if (vol_push_1 + vol_push_2) > 0 else 0.0

    vol_ratio = vol_push_to_3 / vol_ref if vol_ref > 0 else 1.0
    weak_push_confirmed = vol_ratio <= weak_push_max_ratio

    if require_weak_push and not weak_push_confirmed:
        return None

    return ThreeMountainsPattern(
        peak1=peak1, peak2=peak2, peak3=peak3,
        trough_between_2_3=trough,
        resistance_zone_high=zone_high, resistance_zone_low=zone_low,
        weak_push_confirmed=weak_push_confirmed, vol_ratio=vol_ratio,
    )


def check_breakdown_confirmed(candles: List[Candle], pattern: ThreeMountainsPattern) -> bool:
    """La FORMA sola no es una entrada -- exige que el precio ya haya
    CERRADO por debajo del valle entre peak2 y peak3, en una vela
    posterior a peak3 (la ruptura real que el usuario describe como
    'fuerte desplome de golpe')."""
    if pattern.peak3.index >= len(candles) - 1:
        return False  # peak3 es la ultima vela, todavia no hay vela de ruptura despues
    for c in candles[pattern.peak3.index + 1:]:
        if c.close < pattern.trough_between_2_3.price:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════
# ESPEJO LONG -- "tres valles, tercer suelo ascendente"
#
# Investigado antes de construir (no una inversion de signos a ciegas):
# el triple bottom CLASICO exige los tres minimos AL MISMO NIVEL, no un
# tercer minimo mas alto -- varias fuentes de analisis tecnico son
# explicitas en que un extremo marcadamente distinto de los otros dos
# apunta a un hombro-cabeza-hombro invertido, no a un triple bottom
# puro. El patron original (picos 1/2/3 con el 3ro DISTINTO/mas bajo)
# ya se alineaba mas con esa familia que con el triple top/bottom puro
# -- este espejo sigue la MISMA logica ya construida y probada para el
# lado corto, no la version "de manual" del triple bottom clasico.
#
# Dato de la investigacion, con la misma cautela que el resto de la
# sesion (fuente de blog de trading, no revisada por pares, pero el
# numero mas concreto disponible sobre esto): ~65% de ruptura al alza,
# ~23% de fallo (el mas alto entre patrones de suelo), ~35% de subida
# media tras la ruptura. Punto de partida para poner a prueba, no una
# garantia.
#
# Mejora identificada pero NO aplicada todavia (a proposito, para no
# tocar dos cosas a la vez): varias fuentes describen la confirmacion
# contra un "neckline" -- la linea que conecta los DOS retrocesos
# intermedios, no solo el mas reciente. Aplicable a ambos lados
# (corto y largo), ofrecido como mejora futura, no forzado ahora sobre
# logica ya validada.
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ThreeValleysPattern:
    valley1: Pivot
    valley2: Pivot
    valley3: Pivot
    peak_between_2_3: Pivot        # el pico mas alto entre valley2 y valley3
    support_zone_high: float       # max(valley1, valley2)
    support_zone_low: float        # min(valley1, valley2)
    weak_push_confirmed: bool      # volumen hacia valley3 < volumen hacia valley1/2
    vol_ratio: float


def find_pivot_lows(candles: List[Candle], pivot_len: int) -> List[Pivot]:
    """Espejo exacto de find_pivot_highs(): pivote bajo, pivot_len velas
    a cada lado todas con low MAYOR. Mismo criterio de solo pivotes ya
    confirmados."""
    pivots = []
    n = len(candles)
    for i in range(pivot_len, n - pivot_len):
        l = candles[i].low
        is_pivot = True
        for j in range(i - pivot_len, i + pivot_len + 1):
            if j == i:
                continue
            if candles[j].low <= l:
                is_pivot = False
                break
        if is_pivot:
            pivots.append(Pivot(index=i, price=l, open_time=candles[i].open_time))
    return pivots


def find_peak_between(candles: List[Candle], idx_start: int, idx_end: int) -> Optional[Pivot]:
    """Espejo exacto de find_trough_between(): maximo simple (no pivote
    formal) entre dos indices."""
    if idx_end <= idx_start:
        return None
    segment = candles[idx_start:idx_end + 1]
    if not segment:
        return None
    max_candle = max(segment, key=lambda c: c.high)
    max_idx = idx_start + segment.index(max_candle)
    return Pivot(index=max_idx, price=max_candle.high, open_time=max_candle.open_time)


def detect_three_valleys(
    candles: List[Candle],
    pivot_len: int = 3,
    zone_tolerance_pct: float = 0.5,
    valley3_above_zone_pct_min: float = 0.15,
    require_weak_push: bool = True,
    weak_push_max_ratio: float = 0.85,
) -> Optional[ThreeValleysPattern]:
    """
    Espejo exacto de detect_three_mountains(). Misma cadena de
    condiciones, cada comparacion invertida:
      - valley1/valley2 dentro de tolerancia -> "zona de soporte".
      - valley3 GENUINAMENTE POR ENCIMA del borde superior de la zona
        (en vez de por debajo) -- "tercer suelo ascendente", el mercado
        ya no logra alcanzar el soporte previo.
      - Empuje debil: volumen promedio EMPUJANDO HACIA ABAJO hacia
        valley3 menor que hacia valley1/valley2 -- menos participacion
        vendedora en el ultimo intento de romper soporte.
    """
    pivots = find_pivot_lows(candles, pivot_len)
    if len(pivots) < 3:
        return None

    valley1, valley2, valley3 = pivots[-3], pivots[-2], pivots[-1]

    zone_high = max(valley1.price, valley2.price)
    zone_low = min(valley1.price, valley2.price)
    if zone_high <= 0:
        return None
    zone_diff_pct = (zone_high - zone_low) / zone_high * 100.0
    if zone_diff_pct > zone_tolerance_pct:
        return None

    valley3_above_pct = (valley3.price - zone_high) / zone_high * 100.0
    if valley3_above_pct < valley3_above_zone_pct_min:
        return None  # valley3 no quedo genuinamente por encima de la zona

    peak = find_peak_between(candles, valley2.index, valley3.index)
    if peak is None:
        return None

    # Empuje hacia valley3: desde el pico entre valley2/valley3, hasta valley3.
    vol_push_to_3 = avg_volume_between(candles, peak.index, valley3.index)
    ref_start_1 = max(0, valley1.index - pivot_len * 3)
    ref_start_2 = max(0, valley2.index - pivot_len * 3)
    vol_push_1 = avg_volume_between(candles, ref_start_1, valley1.index)
    vol_push_2 = avg_volume_between(candles, ref_start_2, valley2.index)
    vol_ref = (vol_push_1 + vol_push_2) / 2.0 if (vol_push_1 + vol_push_2) > 0 else 0.0

    vol_ratio = vol_push_to_3 / vol_ref if vol_ref > 0 else 1.0
    weak_push_confirmed = vol_ratio <= weak_push_max_ratio

    if require_weak_push and not weak_push_confirmed:
        return None

    return ThreeValleysPattern(
        valley1=valley1, valley2=valley2, valley3=valley3,
        peak_between_2_3=peak,
        support_zone_high=zone_high, support_zone_low=zone_low,
        weak_push_confirmed=weak_push_confirmed, vol_ratio=vol_ratio,
    )


def check_breakout_confirmed(candles: List[Candle], pattern: ThreeValleysPattern) -> bool:
    """Espejo exacto de check_breakdown_confirmed(): exige que el precio
    ya haya CERRADO por encima del pico entre valley2 y valley3, en una
    vela posterior a valley3."""
    if pattern.valley3.index >= len(candles) - 1:
        return False
    for c in candles[pattern.valley3.index + 1:]:
        if c.close > pattern.peak_between_2_3.price:
            return True
    return False
