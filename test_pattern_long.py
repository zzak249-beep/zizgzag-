import sys
sys.path.insert(0, "/home/claude/gold-3mountains-scanner")
from pattern import Candle, detect_three_valleys, check_breakout_confirmed

def make_candle(t, o, h, l, c, v):
    return Candle(open_time=t * 3600000, open=o, high=h, low=l, close=c, volume=v)

candles = []
t = 0

for i in range(5):
    candles.append(make_candle(t, 4380, 4385, 4375, 4380, 1000)); t += 1

for i, price in enumerate([4370, 4355, 4340, 4325, 4318]):
    candles.append(make_candle(t, price + 5, price + 8, price - 2, price + 2, 1400)); t += 1
candles.append(make_candle(t, 4320, 4322, 4310, 4318, 1500)); t += 1  # VALLEY 1 ~4310

for i, price in enumerate([4330, 4345, 4360, 4370, 4375]):
    candles.append(make_candle(t, price - 5, price + 3, price - 8, price, 1200)); t += 1

for i, price in enumerate([4365, 4350, 4335, 4322]):
    candles.append(make_candle(t, price + 5, price + 8, price - 2, price + 2, 1450)); t += 1
candles.append(make_candle(t, 4322, 4324, 4312, 4320, 1480)); t += 1  # VALLEY 2 ~4312

for i, price in enumerate([4335, 4355, 4375, 4392, 4397]):
    candles.append(make_candle(t, price - 5, price + 3, price - 8, price, 1300)); t += 1

for i, price in enumerate([4385, 4375, 4365, 4360]):
    candles.append(make_candle(t, price + 5, price + 8, price - 2, price + 2, 700)); t += 1
candles.append(make_candle(t, 4360, 4362, 4350, 4357, 650)); t += 1  # VALLEY 3 ~4350 (> 4310/4312)

candles.append(make_candle(t, 4360, 4402, 4358, 4400, 2200)); t += 1
candles.append(make_candle(t, 4400, 4425, 4398, 4420, 2000)); t += 1
candles.append(make_candle(t, 4420, 4440, 4415, 4435, 1900)); t += 1
candles.append(make_candle(t, 4435, 4450, 4430, 4445, 1800)); t += 1

pattern = detect_three_valleys(candles, pivot_len=3, zone_tolerance_pct=1.0,
                                 valley3_above_zone_pct_min=0.1, require_weak_push=True,
                                 weak_push_max_ratio=0.85)

assert pattern is not None, "el patron LONG NO se detecto"
print(f"OK: patron LONG detectado")
print(f"  Valley1={pattern.valley1.price:.1f}  Valley2={pattern.valley2.price:.1f}  Valley3={pattern.valley3.price:.1f}")
print(f"  Zona soporte: {pattern.support_zone_low:.1f}-{pattern.support_zone_high:.1f}")
print(f"  Pico 2-3: {pattern.peak_between_2_3.price:.1f}")
print(f"  Empuje debil confirmado: {pattern.weak_push_confirmed} (ratio={pattern.vol_ratio:.2f})")

assert pattern.valley3.price > pattern.support_zone_high, "valley3 deberia quedar por ENCIMA de la zona"
assert pattern.weak_push_confirmed, "el empuje hacia valley3 deberia detectarse como debil"
print("OK: valley3 por encima de zona, empuje debil confirmado")

confirmed = check_breakout_confirmed(candles, pattern)
assert confirmed, "la ruptura alcista deberia confirmarse"
print("OK: ruptura alcista confirmada tras valley3")

flat_candles = [make_candle(i, 4380 - i, 4385 - i, 4375 - i, 4380 - i, 1000) for i in range(30)]
pattern_none = detect_three_valleys(flat_candles, pivot_len=3)
assert pattern_none is None, "NO deberia detectar el patron en una tendencia simple"
print("OK: no detecta falsos positivos en una serie sin el patron")

candles_no_asc = list(candles)
idx = pattern.valley3.index
candles_no_asc[idx] = make_candle(candles[idx].open_time // 3600000, 4320, 4322, 4310, 4318, 650)
pattern_rejected = detect_three_valleys(candles_no_asc, pivot_len=3, zone_tolerance_pct=1.0,
                                          valley3_above_zone_pct_min=0.1)
assert pattern_rejected is None, "deberia rechazar cuando valley3 NO es ascendente"
print("OK: rechaza correctamente cuando el tercer suelo NO es ascendente")

print()
print("=" * 50)
print("TEST DEL ESPEJO LONG (TRES VALLES) PASÓ")
