"""test_pump_fade_engine.py — escenarios sintéticos del setup completo."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["JUMP_GUARD_MODE"] = "block"

import config  # noqa: E402
import pump_fade_engine as eng  # noqa: E402


def mk(o, c, h=None, l=None, t=None, v=100.0):
    return {"open": o, "close": c,
            "high": h if h is not None else max(o, c) * 1.001,
            "low": l if l is not None else min(o, c) * 0.999,
            "volume": v, "time": t or 0}


def mkz(o, c, t, v=100.0):
    """Vela SIN mecha extra (high=max(o,c), low=min(o,c)) — para escenarios
    donde necesito control fino sobre el ATR sin ruido incidental."""
    return {"open": o, "close": c, "high": max(o, c), "low": min(o, c),
            "volume": v, "time": t}


def build_pump(scale=1.0):
    """Base: 200 velas laterales + pump del +40% con pullbacks (swings)."""
    cs, p, t = [], 1.00 * scale, 1000
    for _ in range(200):                      # base tranquila
        cs.append(mk(p, p * 1.0005, t=(t := t + 1)))
        p *= 1.0005
    for leg in range(4):                      # pump con estructura
        for _ in range(12):
            cs.append(mk(p, p * 1.009, t=(t := t + 1)))
            p *= 1.009
        for _ in range(4):                    # pullback -> deja swing lows
            cs.append(mk(p, p * 0.997, t=(t := t + 1)))
            p *= 0.997
    return cs, p, t


# Volumen del "techo" en todos los escenarios: por encima de CEILING_VOL_MULT
# (1.3x default) sobre el promedio de las CEILING_VOL_LEN velas previas
# (volumen=100 en toda la base) — 300 = 3x, blow-off inequívoco.
CEIL_VOL = 300.0


def test_secuencia_completa():
    cs, p, t = build_pump()
    # TECHO: vela que hace el high del día, cierra abajo (rechazo) y trae
    # volumen de distribución real (3x el promedio)
    hi = p * 1.02
    cs.append(mk(p, p * 0.995, h=hi, l=p * 0.994, t=(t := t + 1), v=CEIL_VOL))
    p *= 0.995
    res = eng.analyze(cs, config)
    assert res["state"] == "techo_sin_choch", res["state"]
    assert res["ceiling_vol_ratio"] >= config.CEILING_VOL_MULT, res["ceiling_vol_ratio"]

    # CHoCH: cierres a la baja hasta romper el último swing low (~-11%,
    # muy por encima de MIN_CHOCH_DROP_ATR)
    for _ in range(12):
        cs.append(mk(p, p * 0.990, t=(t := t + 1)))
        p *= 0.990
    res = eng.analyze(cs, config)
    assert res["state"] == "esperando_retest", res["state"]
    assert res["choch_drop_atr"] >= config.MIN_CHOCH_DROP_ATR, res["choch_drop_atr"]
    level = res["broken_level"]

    # RETEST #1: sube hasta tocar el nivel y cierra rechazado por debajo
    while p < level * 0.997:
        cs.append(mk(p, p * 1.004, t=(t := t + 1)))
        p *= 1.004
        assert eng.analyze(cs, config)["signal"] is None
    atr = eng._atr(cs)
    cs.append(mk(p, level - 0.2 * atr,
                 h=level + 0.02 * atr, t=(t := t + 1)))
    p = level - 0.2 * atr
    res = eng.analyze(cs, config)
    assert res["signal"] == "SHORT" and res["retest_count"] == 1, res
    assert res["sl"] > res["entry"] > res["tp"] > 0
    rr = (res["entry"] - res["tp"]) / (res["sl"] - res["entry"])
    assert abs(rr - config.RR) < 0.01, rr
    assert res["sl_dist_pct"] >= config.MIN_SL_DIST_PCT
    assert 0 <= res["quality_score"] <= 100
    print(f"secuencia completa: señal en retest #1, RR={rr:.2f}, "
          f"SL {res['sl_dist_pct']}%, quality_score={res['quality_score']} — OK")
    return cs, p, t, level, atr


def test_segundo_retest_gastado():
    cs, p, t, level, atr = test_secuencia_completa()
    # se aleja y vuelve una SEGUNDA vez
    for _ in range(3):
        cs.append(mk(p, p * 0.996, t=(t := t + 1)))
        p *= 0.996
    while p < level * 0.997:
        cs.append(mk(p, p * 1.004, t=(t := t + 1)))
        p *= 1.004
    cs.append(mk(p, level - 0.2 * atr, h=level + 0.02 * atr, t=(t := t + 1)))
    res = eng.analyze(cs, config)
    assert res["signal"] is None and res["retest_count"] >= 2, res
    print(f"retest #2 (zona gastada): sin señal, count={res['retest_count']} — OK")


def test_reclaim_invalida():
    cs, p, t, level, atr = test_secuencia_completa()
    cs.append(mk(p, level + 1.0 * atr, t=(t := t + 1)))  # cierre bien arriba
    res = eng.analyze(cs, config)
    assert res["signal"] is None and res["state"] == "invalidado", res
    print("reclaim del nivel: setup invalidado — OK")


def test_chase_bloqueado():
    """Un desplome vertical (salto bajista) justo antes del retest-close
    debe bloquear la señal en modo block (perseguir el latigazo)."""
    cs, p, t = build_pump()
    hi = p * 1.02
    cs.append(mk(p, p * 0.995, h=hi, l=p * 0.994, t=(t := t + 1), v=CEIL_VOL))
    p *= 0.995
    for _ in range(12):
        cs.append(mk(p, p * 0.990, t=(t := t + 1)))
        p *= 0.990
    res = eng.analyze(cs, config)
    level = res["broken_level"]
    while p < level * 0.997:
        cs.append(mk(p, p * 1.004, t=(t := t + 1)))
        p *= 1.004
    atr = eng._atr(cs)
    # vela de rechazo que ADEMÁS es un desplome estadístico (-3%)
    crash_close = min(level - 0.2 * atr, p * 0.94)
    cs.append(mk(p, crash_close, h=level + 0.02 * atr, t=(t := t + 1)))
    res = eng.analyze(cs, config)
    assert res["signal"] is None and res["state"] == "bloqueada_chase", res
    print(f"desplome vertical: bloqueada por chase "
          f"(L={res['jump']['L_last']:.1f}) — OK")


def test_sin_techo_no_hay_nada():
    cs, _, _ = build_pump()  # pump sin vela de rechazo en el high
    res = eng.analyze(cs, config)
    assert res["signal"] is None
    print(f"pump sin rechazo: {res['state']}, sin señal (no se shortea "
          "la subida) — OK")


def test_ceiling_requiere_volumen():
    """Una vela con forma de techo perfecta pero volumen normal NO debe
    detectarse como techo — recién califica con volumen de distribución
    real (blow-off), no cualquier vela angosta con mecha."""
    cs, p, t = build_pump()
    hi = p * 1.02
    # MISMO volumen que el resto de la base (100.0, sin spike)
    cs.append(mk(p, p * 0.995, h=hi, l=p * 0.994, t=(t := t + 1), v=100.0))
    res_sin_vol = eng.analyze(cs, config)
    assert res_sin_vol["ceiling_high"] is None, res_sin_vol
    print("techo sin volumen de distribución: NO detectado — OK")

    # la MISMA forma, pero con volumen de blow-off (3x) SÍ califica
    cs2, p2, t2 = build_pump()
    cs2.append(mk(p2, p2 * 0.995, h=hi, l=p2 * 0.994, t=(t2 := t2 + 1), v=CEIL_VOL))
    res_con_vol = eng.analyze(cs2, config)
    assert res_con_vol["ceiling_high"] is not None
    assert res_con_vol["ceiling_vol_ratio"] >= config.CEILING_VOL_MULT
    print(f"misma vela con volumen 3x: techo SÍ detectado "
          f"(vol_ratio={res_con_vol['ceiling_vol_ratio']}) — OK")


def test_choch_requiere_significancia():
    """Romper un pivote bajo NO alcanza si la caída total desde el techo
    es menor a MIN_CHOCH_DROP_ATR — evita CHoCH de ruido pegado al techo.
    Escenario a escala única (mismo 'unit' en base/acercamiento/V) para que
    el ATR sea estable y el gap techo-nivel quede diseñado en ~0.5 ATR."""
    unit = 0.10
    cs, t = [], 0
    p = 100.0
    for i in range(300):
        newp = p + (unit if i % 2 == 0 else -unit * 0.9)
        t += 1
        cs.append(mkz(p, newp, t))
        p = newp
    for _ in range(20):
        t += 1
        newp = p + unit
        cs.append(mkz(p, newp, t))
        p = newp
    atr2 = eng._atr(cs)
    depth = 0.2 * atr2                 # V deliberadamente CHICA (gap ~0.5 ATR)
    n_steps = max(6, int(depth / (unit * 0.5)))
    step = depth / n_steps
    for _ in range(n_steps):
        t += 1
        val = p - step
        cs.append(mkz(p, val, t))
        p = val
    swing_low = p
    for _ in range(n_steps):
        t += 1
        val = p + step
        cs.append(mkz(p, val, t))
        p = val

    lows = eng._swing_lows(cs, config.STRUCT_PIVOT_LEN)
    assert lows, "el pivote bajo debe confirmarse"

    atr3 = eng._atr(cs)
    day_max = max(c["high"] for c in cs[-96:])
    t += 1
    ceiling_high = day_max + 0.001
    close_c = ceiling_high - 1.0 * atr3
    low_c = ceiling_high - 1.3 * atr3        # rechazo válido (close_pos ~0.23)
    cs.append({"open": p, "close": close_c, "high": ceiling_high,
               "low": low_c, "volume": CEIL_VOL, "time": t})
    p = close_c
    r0 = eng.analyze(cs, config)
    assert r0["state"] == "techo_sin_choch", r0["state"]
    gap = (r0["ceiling_high"] - swing_low) / atr3
    assert gap < config.MIN_CHOCH_DROP_ATR, gap  # el nivel es "insignificante"

    # romper el nivel de a poco: debe seguir SIN CHoCH mientras el drop
    # total (desde el techo) no llegue al mínimo, aunque el PRECIO ya haya
    # cerrado por debajo del nivel roto
    broke_bar, flip_bar, flip_res = None, None, None
    for k in range(20):
        t += 1
        p -= step * 0.3
        cs.append(mkz(p, p, t))
        r = eng.analyze(cs, config)
        if p < swing_low and broke_bar is None:
            broke_bar = k
            assert r["state"] == "techo_sin_choch", (
                f"vela {k}: rompió el nivel pero el drop aún es "
                f"insignificante — no debería aceptar CHoCH todavía")
        if r["state"] != "techo_sin_choch":
            flip_bar, flip_res = k, r
            break
    assert broke_bar is not None and flip_bar is not None
    assert flip_bar > broke_bar, "debe haber una ventana rota-pero-rechazada"
    assert flip_res["choch_drop_atr"] >= config.MIN_CHOCH_DROP_ATR
    print(f"CHoCH insignificante: nivel roto en vela {broke_bar}, "
          f"aceptado recién en vela {flip_bar} (drop={flip_res['choch_drop_atr']}) — OK")


def test_quality_score_calculado():
    """El score de calidad se calcula siempre (modo log no bloquea) y
    refleja retest#1 + volumen fuerte + CHoCH profundo + sin chase."""
    cs2, p2, t2 = build_pump()
    hi = p2 * 1.02
    cs2.append(mk(p2, p2 * 0.995, h=hi, l=p2 * 0.994, t=(t2 := t2 + 1), v=CEIL_VOL))
    p2 *= 0.995
    for _ in range(12):
        cs2.append(mk(p2, p2 * 0.990, t=(t2 := t2 + 1)))
        p2 *= 0.990
    res = eng.analyze(cs2, config)
    level2 = res["broken_level"]
    while p2 < level2 * 0.997:
        cs2.append(mk(p2, p2 * 1.004, t=(t2 := t2 + 1)))
        p2 *= 1.004
    atr2 = eng._atr(cs2)
    cs2.append(mk(p2, level2 - 0.2 * atr2, h=level2 + 0.02 * atr2, t=(t2 := t2 + 1)))
    res = eng.analyze(cs2, config)
    assert res["signal"] == "SHORT"
    assert isinstance(res["quality_score"], int)
    assert 0 <= res["quality_score"] <= 100
    # retest#1 (40) + volumen ~3x (30, cerca del cap) + choch profundo
    # (20, cap) + sin chase (10) -> debería quedar alto
    assert res["quality_score"] >= 70, res["quality_score"]
    print(f"quality_score en setup limpio: {res['quality_score']}/100 — OK")

    gate_before = config.QUALITY_GATE_MODE
    assert gate_before == "log", (
        "QUALITY_GATE_MODE debe nacer en 'log' — primero medir con el "
        "journal, no bloquear señales de entrada por un umbral sin datos")
    print(f"QUALITY_GATE_MODE default='{gate_before}' (no bloquea de fábrica) — OK")


if __name__ == "__main__":
    test_sin_techo_no_hay_nada()
    test_ceiling_requiere_volumen()
    test_choch_requiere_significancia()
    test_secuencia_completa()
    test_segundo_retest_gastado()
    test_reclaim_invalida()
    test_chase_bloqueado()
    test_quality_score_calculado()
    print("\nTODOS LOS TESTS OK")
