"""test_pump_fade_engine.py — escenarios sintéticos del setup completo."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["JUMP_GUARD_MODE"] = "block"

import config  # noqa: E402
import pump_fade_engine as eng  # noqa: E402


def mk(o, c, h=None, l=None, t=None):
    return {"open": o, "close": c,
            "high": h if h is not None else max(o, c) * 1.001,
            "low": l if l is not None else min(o, c) * 0.999,
            "volume": 100.0, "time": t or 0}


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


def test_secuencia_completa():
    cs, p, t = build_pump()
    # TECHO: vela que hace el high del día y cierra abajo (rechazo)
    hi = p * 1.02
    cs.append(mk(p, p * 0.995, h=hi, l=p * 0.994, t=(t := t + 1)))
    p *= 0.995
    res = eng.analyze(cs, config)
    assert res["state"] == "techo_sin_choch", res["state"]

    # CHoCH: cierres a la baja hasta romper el último swing low (~-11%)
    for _ in range(12):
        cs.append(mk(p, p * 0.990, t=(t := t + 1)))
        p *= 0.990
    res = eng.analyze(cs, config)
    assert res["state"] == "esperando_retest", res["state"]
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
    print(f"secuencia completa: señal en retest #1, RR={rr:.2f}, "
          f"SL {res['sl_dist_pct']}% — OK")
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
    cs.append(mk(p, p * 0.995, h=hi, l=p * 0.994, t=(t := t + 1)))
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


if __name__ == "__main__":
    test_sin_techo_no_hay_nada()
    test_secuencia_completa()
    test_segundo_retest_gastado()
    test_reclaim_invalida()
    test_chase_bloqueado()
    print("\nTODOS LOS TESTS OK")
