import sys, tempfile, os, asyncio
sys.path.insert(0, "/home/claude/gold-3mountains-scanner")
os.environ["MODE"] = "SIGNAL"

from pattern import Candle
from state import StateManager
import config as cfg
from scanner import get_symbol_universe, run_scan_cycle


def make_candle(t, o, h, l, c, v):
    return Candle(open_time=t * 3600000, open=o, high=h, low=l, close=c, volume=v)


def build_pattern_candles(base_price=1.0):
    """Misma forma exacta que test_pattern.py, escalada por base_price
    para poder reutilizarla en simbolos con precios muy distintos."""
    scale = base_price / 4400.0
    candles = []
    t = 0
    def sc(p): return p * scale
    for i in range(5):
        candles.append(make_candle(t, sc(4380), sc(4385), sc(4375), sc(4380), 1000)); t += 1
    for i, price in enumerate([4390, 4405, 4420, 4435, 4442]):
        candles.append(make_candle(t, sc(price - 5), sc(price), sc(price - 8), sc(price - 2), 1400)); t += 1
    candles.append(make_candle(t, sc(4440), sc(4450), sc(4438), sc(4442), 1500)); t += 1
    for i, price in enumerate([4430, 4415, 4400, 4390, 4385]):
        candles.append(make_candle(t, sc(price + 5), sc(price + 8), sc(price - 3), sc(price), 1200)); t += 1
    for i, price in enumerate([4395, 4410, 4425, 4438]):
        candles.append(make_candle(t, sc(price - 5), sc(price), sc(price - 8), sc(price - 2), 1450)); t += 1
    candles.append(make_candle(t, sc(4438), sc(4448), sc(4436), sc(4440), 1480)); t += 1
    for i, price in enumerate([4425, 4405, 4385, 4368, 4363]):
        candles.append(make_candle(t, sc(price + 5), sc(price + 8), sc(price - 3), sc(price), 1300)); t += 1
    for i, price in enumerate([4375, 4385, 4395, 4400]):
        candles.append(make_candle(t, sc(price - 5), sc(price), sc(price - 8), sc(price - 2), 700)); t += 1
    candles.append(make_candle(t, sc(4400), sc(4410), sc(4398), sc(4403), 650)); t += 1
    candles.append(make_candle(t, sc(4400), sc(4402), sc(4340), sc(4345), 2200)); t += 1
    candles.append(make_candle(t, sc(4345), sc(4350), sc(4318), sc(4320), 2000)); t += 1
    candles.append(make_candle(t, sc(4320), sc(4325), sc(4300), sc(4305), 1900)); t += 1
    candles.append(make_candle(t, sc(4305), sc(4310), sc(4285), sc(4290), 1800)); t += 1
    return candles


def build_flat_candles(base_price=1.0, n=30):
    return [make_candle(i, base_price, base_price * 1.001, base_price * 0.999, base_price, 1000) for i in range(n)]


class FakeClient:
    """Simula get_contracts() y get_klines() -- sin red real."""
    def __init__(self, contracts, klines_by_symbol):
        self._contracts = contracts
        self._klines = klines_by_symbol
    async def get_contracts(self):
        return self._contracts
    async def get_klines(self, symbol, interval, limit):
        return self._klines.get(symbol, [])


class FakeNotifier:
    enabled = True
    def __init__(self):
        self.sent = []
    async def send(self, text):
        self.sent.append(text)
    async def send_direct(self, text):
        self.sent.append(text)
        return True


async def run_tests():
    # ── Test 1: el filtro de tokenizados excluye NC-prefijo ──
    contracts = [
        {"symbol": "BTC-USDT", "status": "1"},
        {"symbol": "ETH-USDT", "status": "1"},
        {"symbol": "NCSKNOK2USD-USDT", "status": "1"},   # accion tokenizada
        {"symbol": "NCCO7241NATGAS2USD-USDT", "status": "1"},  # materia prima tokenizada
        {"symbol": "NCFXUSD2DKK-USDT", "status": "1"},   # forex tokenizado
        {"symbol": "XAUT-USDT", "status": "1"},          # oro tokenizado -- este SI debe pasar (no empieza por NC en el sentido filtrado... espera, "XAUT" no empieza por "NC")
    ]
    client_universe = FakeClient(contracts, {})
    symbols = await get_symbol_universe(client_universe, force=True)
    assert "BTC-USDT" in symbols and "ETH-USDT" in symbols and "XAUT-USDT" in symbols
    assert "NCSKNOK2USD-USDT" not in symbols
    assert "NCCO7241NATGAS2USD-USDT" not in symbols
    assert "NCFXUSD2DKK-USDT" not in symbols
    print(f"OK: filtro de tokenizados -- {len(symbols)} simbolos pasan, los 3 NC-prefijo excluidos, XAUT-USDT SI incluido")

    # ── Test 2: escaneo concurrente detecta el patron en 2 de 3 simbolos ──
    klines = {
        "BTC-USDT": build_pattern_candles(base_price=60000),
        "ETH-USDT": build_pattern_candles(base_price=3000),
        "SOL-USDT": build_flat_candles(base_price=150),  # sin patron
    }
    contracts2 = [{"symbol": s, "status": "1"} for s in klines]
    client2 = FakeClient(contracts2, klines)
    tmp = tempfile.mktemp(suffix=".json")
    state = StateManager(tmp)
    notifier = FakeNotifier()

    await run_scan_cycle(client2, state, notifier)
    assert "BTC-USDT" in state.positions, "deberia haber detectado el patron en BTC"
    assert "ETH-USDT" in state.positions, "deberia haber detectado el patron en ETH"
    assert "SOL-USDT" not in state.positions, "SOL no tiene el patron, no deberia haber posicion"
    print(f"OK: escaneo concurrente -- {len(state.positions)} posiciones abiertas (BTC, ETH), SOL correctamente sin señal")

    # ── Test 3: limite de posiciones concurrentes se respeta ──
    tmp2 = tempfile.mktemp(suffix=".json")
    state2 = StateManager(tmp2)
    original_max = cfg.MAX_CONCURRENT_POSITIONS
    cfg.MAX_CONCURRENT_POSITIONS = 1
    notifier2 = FakeNotifier()
    await run_scan_cycle(client2, state2, notifier2)
    assert len(state2.positions) <= 1, f"con MAX_CONCURRENT_POSITIONS=1 no deberia haber mas de 1 posicion, hay {len(state2.positions)}"
    print(f"OK: limite de posiciones concurrentes respetado ({len(state2.positions)}/1)")
    cfg.MAX_CONCURRENT_POSITIONS = original_max

    for p in (tmp, tmp2):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

    print()
    print("=" * 50)
    print("TEST DE INTEGRACIÓN MULTI-SÍMBOLO PASÓ")


asyncio.run(run_tests())
