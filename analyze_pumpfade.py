"""
analyze_pumpfade.py — convierte el journal del pump-fade en el veredicto
del período de observación.

Uso (en la consola de Railway o local):
    python3 analyze_pumpfade.py /data/pumpfade_journal.json

Responde las preguntas del pase a real:
- ¿Cuántas señales hubo y cuántas GANARON (paper)?
- ¿El retest #1 rinde? ¿Los pumps grandes caen mejor que los chicos?
- ¿A qué horas aparecen las señales (killzones)?
- ¿Cuántas bloqueó el jump guard / el techo de SL? ¿El guard ayudó?
- Expectancy en R y en USDT simulados.
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

CET = timezone(timedelta(hours=2))


def load(path):
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def fmt_pct(x, n):
    return f"{x}/{n} ({x / n * 100:.0f}%)" if n else "0/0"


def bucket_gain(g):
    if g is None:
        return "?"
    if g < 50:
        return "+25-50%"
    if g < 100:
        return "+50-100%"
    return "+100%+"


def main(path):
    entries = load(path)
    opened = [e for e in entries if e.get("event") == "position_opened"]
    closed = [e for e in entries if e.get("event") == "position_closed"]
    blocked = [e for e in entries if e.get("event") == "signal_blocked"]
    by_key = {e.get("setup_key"): e for e in opened}

    print("═" * 62)
    print(f" PUMP FADE — análisis del journal ({len(entries)} eventos)")
    print("═" * 62)

    # ── Señales y resultados ──
    sims = [e for e in closed if e.get("simulated")]
    reales = [e for e in closed if not e.get("simulated")]
    wins = [e for e in sims if e.get("result") == "tp"]
    losses = [e for e in sims if e.get("result") == "sl"]
    print(f"\nSeñales abiertas: {len(opened)}  "
          f"(paper cerradas: {len(sims)}, reales: {len(reales)}, "
          f"aún abiertas: {len(opened) - len(closed)})")
    if sims:
        n = len(wins) + len(losses)
        wr = len(wins) / n * 100 if n else 0
        pnl = sum(float(e.get("pnl_usdt", 0)) for e in sims)
        print(f"PAPER: {len(wins)}W / {len(losses)}L  →  WR {wr:.0f}%  "
              f"(breakeven a 2R = 33%)")
        print(f"PnL simulado acumulado: {pnl:+.2f} USDT  |  "
              f"expectancy: {pnl / n:+.2f} USDT/trade" if n else "")

    # ── Por número de retest ──
    if sims:
        print("\nPor retest #:")
        agg = defaultdict(lambda: [0, 0])
        for e in sims:
            k = e.get("retest_count")
            agg[k][0 if e.get("result") == "tp" else 1] += 1
        for k in sorted(agg, key=lambda x: (x is None, x)):
            w, l = agg[k]
            print(f"  retest #{k}: {fmt_pct(w, w + l)} de win rate")

    # ── Por tamaño del pump ──
    if sims:
        print("\nPor tamaño del pump (peak o gain al abrir):")
        agg = defaultdict(lambda: [0, 0])
        for e in sims:
            op = by_key.get(e.get("setup_key")) or {}
            g = op.get("peak_gain_pct") or e.get("gain_24h_pct")
            agg[bucket_gain(g)][0 if e.get("result") == "tp" else 1] += 1
        for b in ("+25-50%", "+50-100%", "+100%+", "?"):
            if b in agg:
                w, l = agg[b]
                print(f"  {b:9s}: {fmt_pct(w, w + l)}")

    # ── Distribución horaria (CET) ──
    if opened:
        print("\nSeñales por hora (CET) — tus killzones son 09-11 y 14:30-17:30:")
        hh = Counter(datetime.fromtimestamp(e["ts"] / 1000, CET).hour
                     for e in opened if e.get("ts"))
        for h in sorted(hh):
            print(f"  {h:02d}h {'█' * hh[h]} {hh[h]}")

    # ── Bloqueadas ──
    if blocked:
        print(f"\nSeñales BLOQUEADAS: {len(blocked)}")
        for state, cnt in Counter(e.get("state") for e in blocked).items():
            print(f"  {state}: {cnt}")
        chase_l = [float((e.get('jump') or {}).get('L_last') or 0)
                   for e in blocked if e.get("state") == "bloqueada_chase"]
        if chase_l:
            print(f"  L del chase: min {min(chase_l):.1f} / "
                  f"max {max(chase_l):.1f} — si el WR general es bueno y "
                  f"estos L rondan el umbral (4.0), probar JUMP_THRESH=4.5")

    # ── Duración ──
    hm = [e.get("held_min") for e in closed if e.get("held_min") is not None]
    if hm:
        hm.sort()
        print(f"\nDuración de los trades: mediana {hm[len(hm) // 2]} min, "
              f"máx {hm[-1]} min (dato para el time-stop de Raschke)")

    # ── Veredicto orientativo ──
    print("\n" + "─" * 62)
    n = len(wins) + len(losses)
    if n < 10:
        print(f"VEREDICTO: {n} trades resueltos — muestra corta, seguir "
              f"observando (mínimo ~10-15 para decidir, 30+ para confiar).")
    else:
        wr = len(wins) / n * 100
        if wr >= 40:
            print(f"VEREDICTO: WR {wr:.0f}% a 2R con n={n} — margen sobre el "
                  f"breakeven (33%). Candidato a DRY_RUN=False con subcuenta "
                  f"dedicada y riesgo 1%.")
        elif wr >= 30:
            print(f"VEREDICTO: WR {wr:.0f}% con n={n} — en el filo. Cruzar "
                  f"por retest#/pump-size arriba y recortar lo que pierde "
                  f"antes de pasar a real.")
        else:
            print(f"VEREDICTO: WR {wr:.0f}% con n={n} — por debajo del "
                  f"breakeven. NO pasar a real; revisar buckets para ver si "
                  f"algún subconjunto salva el setup.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/data/pumpfade_journal.json")
