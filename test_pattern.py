import sys
sys.path.insert(0, "/home/claude/gold-3mountains-scanner")
from pattern import Candle, detect_three_mountains, check_breakdown_confirmed

def make_candle(t, o, h, l, c, v):
    return Candle(open_time=t * 3600000, open=o, high=h, low=l, close=c, volume=v)

# ── Replica la forma de la captura: peak1 alto, dip, peak2 similar,
# dip mas profundo, peak3 MAS BAJO que peak1/peak2, luego caida fuerte.
# Volumen deliberadamente MAS BAJO en el tramo hacia peak3 (empuje debil).
candles = []
t = 0

# Base plana antes del patron
for i in range(5):
    candles.append(make_candle(t, 4380, 4385, 4375, 4380, 1000)); t += 1

# Subida hacia peak1 (volumen normal-alto)
for i, price in enumerate([4390, 4405, 4420, 4435, 4442]):
    candles.append(make_candle(t, price - 5, price, price - 8, price - 2, 1400)); t += 1
candles.append(make_candle(t, 4440, 4450, 4438, 4442, 1500)); t += 1  # PEAK 1 ~4450

# Caida tras peak1
for i, price in enumerate([4430, 4415, 4400, 4390, 4385]):
    candles.append(make_candle(t, price + 5, price + 8, price - 3, price, 1200)); t += 1

# Subida hacia peak2 (volumen normal-alto, similar a peak1)
for i, price in enumerate([4395, 4410, 4425, 4438]):
    candles.append(make_candle(t, price - 5, price, price - 8, price - 2, 1450)); t += 1
candles.append(make_candle(t, 4438, 4448, 4436, 4440, 1480)); t += 1  # PEAK 2 ~4448

# Caida mas profunda tras peak2 (el valle que luego se rompe)
for i, price in enumerate([4425, 4405, 4385, 4368, 4363]):
    candles.append(make_candle(t, price + 5, price + 8, price - 3, price, 1300)); t += 1
trough_price = 4363

# Subida hacia peak3 -- MAS BAJA que peak1/peak2, con volumen MAS BAJO (empuje debil)
for i, price in enumerate([4375, 4385, 4395, 4400]):
    candles.append(make_candle(t, price - 5, price, price - 8, price - 2, 700)); t += 1
candles.append(make_candle(t, 4400, 4410, 4398, 4403, 650)); t += 1  # PEAK 3 ~4410 (< 4448/4450)

# Vela de ruptura: cierra por debajo del valle (trough_price=4363)
candles.append(make_candle(t, 4400, 4402, 4340, 4345, 2200)); t += 1  # rompe con fuerza
candles.append(make_candle(t, 4345, 4350, 4318, 4320, 2000)); t += 1  # sigue cayendo
candles.append(make_candle(t, 4320, 4325, 4300, 4305, 1900)); t += 1  # continua
candles.append(make_candle(t, 4305, 4310, 4285, 4290, 1800)); t += 1  # continua

pattern = detect_three_mountains(candles, pivot_len=3, zone_tolerance_pct=1.0,
                                   peak3_below_zone_pct_min=0.1, require_weak_push=True,
                                   weak_push_max_ratio=0.85)

assert pattern is not None, "el patron NO se detecto -- deberia haberse detectado con estos datos sinteticos"
print(f"OK: patron detectado")
print(f"  Peak1={pattern.peak1.price:.1f}  Peak2={pattern.peak2.price:.1f}  Peak3={pattern.peak3.price:.1f}")
print(f"  Zona resistencia: {pattern.resistance_zone_low:.1f}-{pattern.resistance_zone_high:.1f}")
print(f"  Valle 2-3: {pattern.trough_between_2_3.price:.1f}")
print(f"  Empuje debil confirmado: {pattern.weak_push_confirmed} (ratio={pattern.vol_ratio:.2f})")

assert pattern.peak3.price < pattern.resistance_zone_low, "peak3 deberia quedar por debajo de la zona"
assert pattern.weak_push_confirmed, "el empuje hacia peak3 deberia detectarse como debil (menor volumen)"
print("OK: peak3 por debajo de zona, empuje debil confirmado")

confirmed = check_breakdown_confirmed(candles, pattern)
assert confirmed, "la ruptura deberia confirmarse -- hay una vela que cierra por debajo del valle"
print("OK: ruptura confirmada tras peak3")

# ── Caso negativo: SIN el patron (tendencia simple, sin 3 picos descendentes) ──
flat_candles = [make_candle(i, 4380 + i, 4385 + i, 4375 + i, 4380 + i, 1000) for i in range(30)]
pattern_none = detect_three_mountains(flat_candles, pivot_len=3)
assert pattern_none is None, "NO deberia detectar el patron en una tendencia simple sin 3 picos"
print("OK: no detecta falsos positivos en una serie sin el patron")

# ── Caso negativo: peak3 NO por debajo de la zona (deberia rechazar) ──
candles_no_desc = list(candles)
idx = pattern.peak3.index
candles_no_desc[idx] = make_candle(candles[idx].open_time // 3600000, 4440, 4450, 4438, 4442, 650)
pattern_rejected = detect_three_mountains(candles_no_desc, pivot_len=3, zone_tolerance_pct=1.0,
                                            peak3_below_zone_pct_min=0.1)
assert pattern_rejected is None, "deberia rechazar cuando peak3 NO queda por debajo de la zona"
print("OK: rechaza correctamente cuando el tercer techo NO es descendente")

print()
print("=" * 50)
print("TEST DE DETECCIÓN DEL PATRÓN PASÓ")
