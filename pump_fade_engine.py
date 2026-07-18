"""
pump_fade_engine.py — Techo del día -> CHoCH bajista -> PRIMER retest = SHORT.

Secuencia exigida (las tres, en orden, sobre velas 5m cerradas):

1. TECHO CON RECHAZO: una vela hace el máximo de la ventana del día
   (DAY_BARS), cierra en el tramo inferior de su propio rango
   (REJECT_MAX_CLOSE_POS) con rango >= CEILING_MIN_RANGE_ATR * ATR, Y
   volumen >= CEILING_VOL_MULT x el promedio reciente (distribución real
   de un blow-off, no ruido silencioso).

2. CHoCH BAJISTA: después del techo, un CIERRE por debajo del último
   swing low confirmado (STRUCT_PIVOT_LEN a cada lado) que ADEMÁS caiga
   al menos MIN_CHOCH_DROP_ATR ATRs desde el techo (significancia
   estructural — se ignoran rupturas de pivotes insignificantes pegados
   al propio techo). El nivel roto queda como resistencia.

3. PRIMER RETEST (Raschke): el precio se ALEJA del nivel (ARM_DIST_ATR,
   arma el contador), vuelve a tocarlo desde abajo (RETEST_TOUCH_ATR) y
   una vela cierra de nuevo por debajo (RETEST_BREAK_ATR). Solo el
   episodio #1..PUMP_MAX_RETEST vale: cada toque gasta la zona.

Invalidaciones (setup muerto, esperar uno nuevo):
- cierre por encima del techo
- cierre > nivel + RECLAIM_ATR*ATR (reclaim: el quiebre era falso)

La señal solo dispara si la vela de retest es LA ÚLTIMA CERRADA (frescura).
SL = máx(high desde el CHoCH, nivel) + SL_ATR_BUFFER*ATR, con piso
MIN_SL_DIST_PCT y techo MAX_SL_DIST_PCT (más de eso = parabólico
demasiado salvaje, se pasa). TP = RR fijo.

Score de calidad (0-100, mismo espíritu que tu QF×JP): retest#1 (40pts) +
volumen del techo (30pts) + profundidad del CHoCH (20pts) + sin chase
reciente (10pts). Se calcula y journalea SIEMPRE; solo bloquea señales si
QUALITY_GATE_MODE=block (default "log" — el journal decide el umbral).

ATR con suavizado de Wilder (RMA), el mismo método que ta.atr() en Pine:
el número que ve el bot y el que dibuja el script en pantalla coinciden.

Todo stateless: se recalcula desde las velas en cada scan, cero memoria
entre ciclos (el patrón que nunca se corrompe con reinicios de Railway).
"""
import logging

import jump_detector

log = logging.getLogger("pump_fade_engine")


def _true_range(candles, i):
    h, l = candles[i]["high"], candles[i]["low"]
    pc = candles[i - 1]["close"]
    return max(h - l, abs(h - pc), abs(l - pc))


def _atr(candles, period=14):
    """ATR con suavizado de Wilder (RMA) — el MISMO método que usa ta.atr()
    en el script de TradingView. Antes esta función promediaba solo los
    últimos N true ranges con ventana fija (sin memoria de nada anterior);
    ahora es la EMA de Wilder sobre toda la serie disponible, igual que la
    ve el gráfico. Con esto el ATR que usa el bot para SL/TP y el ATR que
    dibuja el script en pantalla son el mismo número — antes divergían."""
    n = len(candles)
    if n < period + 1:
        return None
    atr_val = sum(_true_range(candles, i) for i in range(1, period + 1)) / period
    for i in range(period + 1, n):
        atr_val = (atr_val * (period - 1) + _true_range(candles, i)) / period
    return atr_val


def _swing_lows(candles, pl):
    """Índices de swing lows confirmados (pl velas a cada lado)."""
    out = []
    for i in range(pl, len(candles) - pl):
        lo = candles[i]["low"]
        if all(candles[j]["low"] >= lo for j in range(i - pl, i + pl + 1) if j != i):
            out.append(i)
    return out


def _find_ceiling(candles, atr, config):
    """Última vela-techo con rechazo dentro de CEILING_MAX_AGE_BARS.
    Devuelve (idx, vol_ratio) o (None, None).

    NUEVO: exige volumen >= CEILING_VOL_MULT x el promedio de las
    CEILING_VOL_LEN velas previas. Un blow-off top real muestra compra
    climática siendo absorbida — volumen por encima del promedio, no una
    vela angosta cualquiera con mecha. Sin este filtro, ruido silencioso
    calificaba igual que una distribución real.
    """
    n = len(candles)
    day_start = max(0, n - config.DAY_BARS)
    min_idx = max(day_start, n - config.CEILING_MAX_AGE_BARS)
    vol_len = config.CEILING_VOL_LEN
    best, best_ratio = None, None
    for i in range(min_idx, n):
        c = candles[i]
        rng = c["high"] - c["low"]
        if rng <= 0 or rng < config.CEILING_MIN_RANGE_ATR * atr:
            continue
        # máximo del día HASTA esa vela inclusive
        if c["high"] < max(x["high"] for x in candles[day_start: i + 1]):
            continue
        close_pos = (c["close"] - c["low"]) / rng
        if close_pos > config.REJECT_MAX_CLOSE_POS:
            continue
        vstart = max(0, i - vol_len)
        vol_hist = [candles[j]["volume"] for j in range(vstart, i)]
        avg_vol = sum(vol_hist) / len(vol_hist) if vol_hist else 0.0
        vol_ratio = (c["volume"] / avg_vol) if avg_vol > 0 else 0.0
        if vol_ratio < config.CEILING_VOL_MULT:
            continue
        best, best_ratio = i, vol_ratio  # nos quedamos con el techo más reciente
    return best, best_ratio


def analyze(candles, config):
    """
    Devuelve dict:
      signal: "SHORT" | None
      entry, sl, tp (si signal)
      state: "sin_techo" | "techo_sin_choch" | "esperando_retest" |
             "retest_gastado" | "invalidado" | "senal" | "bloqueada_chase" |
             "sl_fuera_de_rango"
      ceiling_idx, ceiling_high, broken_level, choch_idx, retest_count,
      sl_widened, jump (dict del guard), setup_key_suffix
    """
    out = {"signal": None, "state": "sin_techo", "retest_count": 0, "leg": 1,
           "ceiling_high": None, "broken_level": None, "sl_widened": False,
           "jump": None, "setup_key_suffix": None, "ceiling_vol_ratio": None,
           "choch_drop_atr": None, "quality_score": None}
    n = len(candles)
    min_bars = max(config.DAY_BARS // 2, config.JUMP_WIN + 10,
                   2 * config.STRUCT_PIVOT_LEN + 10)
    if n < min_bars:
        out["state"] = f"datos_insuficientes ({n}/{min_bars})"
        return out

    atr = _atr(candles)
    if not atr or atr <= 0:
        out["state"] = "sin_atr"
        return out

    ceil_idx, ceiling_vol_ratio = _find_ceiling(candles, atr, config)
    if ceil_idx is None:
        return out
    ceiling_high = candles[ceil_idx]["high"]
    out.update(ceiling_idx=ceil_idx, ceiling_high=ceiling_high,
               ceiling_vol_ratio=round(ceiling_vol_ratio, 2),
               state="techo_sin_choch")

    # ── CHoCH: primer cierre bajo el último swing low previo al quiebre,
    #    que ADEMÁS represente una caída significativa desde el techo ──
    # NUEVO: exige (techo - cierre) >= MIN_CHOCH_DROP_ATR x ATR. Sin esto,
    # romper un pivote insignificante pegado al propio techo (ruido, no
    # reversión real) contaba como CHoCH válido — la búsqueda sigue
    # avanzando hasta que la caída sea estructuralmente real.
    lows = _swing_lows(candles, config.STRUCT_PIVOT_LEN)
    choch_idx = None
    broken_level = None
    choch_drop_atr = None
    for i in range(ceil_idx + 1, n):
        # swing lows confirmados hasta la vela i (confirmación = pl velas después)
        confirmed = [j for j in lows if j + config.STRUCT_PIVOT_LEN <= i]
        prior = [j for j in confirmed if j <= i]
        if not prior:
            continue
        level = candles[max(prior)]["low"]
        if candles[i]["close"] < level:
            drop_atr = (ceiling_high - candles[i]["close"]) / atr
            if drop_atr < config.MIN_CHOCH_DROP_ATR:
                continue  # rotura real pero todavía poco significativa
            choch_idx, broken_level, choch_drop_atr = i, level, drop_atr
            break
    if choch_idx is None:
        return out
    out.update(choch_idx=choch_idx, broken_level=broken_level,
               choch_drop_atr=round(choch_drop_atr, 2),
               state="esperando_retest",
               setup_key_suffix=f"{candles[choch_idx].get('time', choch_idx)}")

    # ── Post-CHoCH: invalidaciones y episodios de retest ──
    touch_lo = broken_level - config.RETEST_TOUCH_ATR * atr
    break_lv = broken_level - config.RETEST_BREAK_ATR * atr
    reclaim_lv = broken_level + config.RECLAIM_ATR * atr

    arm_lv = broken_level - config.ARM_DIST_ATR * atr
    episodes = 0
    touching = False
    armed = False       # el precio debe ALEJARSE del nivel antes de que un
    signal_idx = None   # acercamiento cuente como retest (si no, el propio
    leg = 1             # ESCALERA: peldano vigente (cada CHoCH nuevo = +1)
    for i in range(choch_idx + 1, n):   # desplome consume el episodio #1)
        c = candles[i]
        if c["close"] > ceiling_high or c["close"] > reclaim_lv:
            out["state"] = "invalidado"
            return out
        # ── ESCALERA: cierre bajo un swing low confirmado MAS BAJO que el
        # nivel vigente = nuevo peldano; el nivel operable pasa a ser ese
        # (primer pullback fresco de Raschke POR peldano).
        nuevo = None
        for lo_idx in lows:
            lo_lv = candles[lo_idx]["low"]
            if (lo_idx > choch_idx
                    and lo_idx + config.STRUCT_PIVOT_LEN <= i
                    and lo_lv < broken_level - config.ARM_DIST_ATR * atr
                    and c["close"] < lo_lv
                    and (nuevo is None or lo_idx > nuevo[0])):
                nuevo = (lo_idx, lo_lv)
        if nuevo is not None:
            leg += 1
            broken_level = nuevo[1]
            reclaim_lv = broken_level + config.RECLAIM_ATR * atr
            touch_lo = broken_level - config.RETEST_TOUCH_ATR * atr
            break_lv = broken_level - config.RETEST_BREAK_ATR * atr
            arm_lv = broken_level - config.ARM_DIST_ATR * atr
            choch_idx = i
            episodes = 0
            touching = False
            armed = False
            signal_idx = None
            continue
        if not armed:
            if c["high"] < arm_lv:
                armed = True
            continue
        touch_now = c["high"] >= touch_lo
        if touch_now and not touching:
            episodes += 1
        # vela de rechazo: tocó (esta o venía tocando), cierra debajo Y es
        # bajista — una vela verde que roza el nivel es la aproximación,
        # no el rechazo (espejo del crossunder del superscript)
        if (touch_now or touching) and c["close"] < break_lv and c["close"] < c["open"]:
            if episodes <= config.PUMP_MAX_RETEST:
                signal_idx = i          # el último rechazo válido manda
            touching = False
        else:
            touching = touch_now
    out["retest_count"] = episodes
    out["leg"] = leg
    out["broken_level"] = broken_level
    out["choch_idx"] = choch_idx

    if episodes == 0:
        return out
    if signal_idx is None:
        # hubo toque pero todavia ninguna vela de rechazo valida
        out["state"] = ("retest_gastado" if episodes > config.PUMP_MAX_RETEST
                        else "esperando_retest")
        return out
    if episodes > config.PUMP_MAX_RETEST:
        out["state"] = "retest_gastado"
        return out
    # frescura: la señal es la ÚLTIMA vela cerrada, no una vieja
    if signal_idx != n - 1:
        out["state"] = "retest_gastado" if episodes >= config.PUMP_MAX_RETEST \
            else "esperando_retest"
        return out

    # ── Jump guard: no perseguir el latigazo bajista ──
    jump = jump_detector.confirms_direction(candles, "SHORT", config)
    out["jump"] = {k: jump.get(k) for k in
                   ("confirms", "L_last", "jump_recent", "jump_dir",
                    "jump_share", "reason", "mode")}
    if jump.get("confirms") is False:
        out["state"] = "bloqueada_chase"
        return out

    # ── Score de calidad (0-100), igual espíritu que tu QF×JP: no binario,
    #    compuesto por cuatro capas independientes. Se calcula y journalea
    #    SIEMPRE; solo bloquea señales si QUALITY_GATE_MODE=block (default
    #    "log" — el journal decide el umbral con datos, no de entrada) ──
    retest_pts = max(10, 40 - 15 * (episodes - 1))            # 40 en retest #1
    vol_pts = min(30, max(0, (ceiling_vol_ratio - 1.0) * 30))  # techo con volumen
    choch_pts = min(20, max(0, (choch_drop_atr - config.MIN_CHOCH_DROP_ATR) * 10))
    jump_pts = 0 if jump.get("jump_recent") else 10           # sin chase reciente
    quality_score = int(round(max(0, min(100,
        retest_pts + vol_pts + choch_pts + jump_pts))))
    out["quality_score"] = quality_score

    gate_mode = str(getattr(config, "QUALITY_GATE_MODE", "log")).lower()
    if gate_mode == "block" and quality_score < config.MIN_QUALITY_SCORE:
        out["state"] = "calidad_insuficiente"
        return out

    # ── E / SL / TP ──
    entry = candles[-1]["close"]
    local_high = max(c["high"] for c in candles[choch_idx:])
    sl_raw = max(local_high, broken_level) + config.SL_ATR_BUFFER * atr
    sl_dist_pct = (sl_raw - entry) / entry * 100 if entry > 0 else 0
    sl = sl_raw
    if sl_dist_pct < config.MIN_SL_DIST_PCT:
        sl = entry * (1 + config.MIN_SL_DIST_PCT / 100)
        out["sl_widened"] = True
        sl_dist_pct = config.MIN_SL_DIST_PCT
    if sl_dist_pct > config.MAX_SL_DIST_PCT:
        out["state"] = "sl_fuera_de_rango"
        out["sl_dist_pct"] = round(sl_dist_pct, 2)
        return out

    tp = entry - config.RR * (sl - entry)
    if tp <= 0:
        out["state"] = "tp_invalido"
        return out

    out.update(signal="SHORT", entry=entry, sl=sl, tp=tp,
               sl_dist_pct=round(sl_dist_pct, 2), state="senal")
    return out
