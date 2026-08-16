import sys, tempfile, os, asyncio
sys.path.insert(0, "/home/claude/gold-3mountains-scanner")
os.environ["MODE"] = "SIGNAL"

from state import StateManager
from telegram_notifier import format_backup
import config as cfg
from scanner import _classify_tier, _ema_simple, _htf_bias_bearish, _send_daily_backup
from pattern import Candle


def make_candle(t, o, h, l, c, v):
    return Candle(open_time=t * 3600000, open=o, high=h, low=l, close=c, volume=v)


class FakeNotifierOk:
    enabled = True
    async def send_direct(self, text): return True

class FakeNotifierFail:
    enabled = True
    async def send_direct(self, text): return False

class FakeNotifierDisabled:
    enabled = False
    async def send_direct(self, text): raise AssertionError("no deberia llamarse si esta deshabilitado")

class FakeClientHTF:
    def __init__(self, candles): self._candles = candles
    async def get_klines(self, symbol, interval, limit): return self._candles


def run():
    # ── 1) Circuit breaker: racha de perdidas ──
    tmp = tempfile.mktemp(suffix=".json")
    s = StateManager(tmp)
    assert not s.circuit_breaker_active()
    s.positions["A-USDT"] = {"tier": "altcoin"}
    s.close_position("A-USDT", win=False)
    s.positions["B-USDT"] = {"tier": "altcoin"}
    s.close_position("B-USDT", win=False)
    assert not s.circuit_breaker_active(), "2 perdidas seguidas, umbral es 3 -- no deberia activarse todavia"
    s.positions["C-USDT"] = {"tier": "altcoin"}
    s.close_position("C-USDT", win=False)
    assert s.circuit_breaker_active(), "3 perdidas seguidas deberian activar el circuit breaker"
    print("OK: circuit breaker por racha de perdidas")

    s.positions["D-USDT"] = {"tier": "altcoin"}
    s.close_position("D-USDT", win=True)
    assert s.consecutive_losses == 0, "un win deberia resetear la racha de perdidas"
    print("OK: un win resetea la racha")

    # ── 2) Circuit breaker: limite diario de perdidas ──
    tmp2 = tempfile.mktemp(suffix=".json")
    s2 = StateManager(tmp2)
    for sym in ["A-USDT", "B-USDT"]:
        s2.positions[sym] = {"tier": "altcoin"}
        s2.close_position(sym, win=True)
    assert not s2.circuit_breaker_active()
    for sym in ["C-USDT", "D-USDT", "E-USDT"]:
        s2.positions[sym] = {"tier": "altcoin"}
        s2.close_position(sym, win=False)
    assert s2.circuit_breaker_active(), "3 perdidas en el dia deberian activar el breaker por limite diario"
    print("OK: circuit breaker por limite diario de perdidas")

    # ── 3) Tier tracking ──
    assert _classify_tier("BTC-USDT") == "major"
    assert _classify_tier("SOME-RANDOM-USDT") == "altcoin"
    tmp3 = tempfile.mktemp(suffix=".json")
    s3 = StateManager(tmp3)
    s3.open_position("BTC-USDT", "SHORT", 100, 105, 90, "key1", tier="major")
    s3.close_position("BTC-USDT", win=True)
    assert s3.tier_stats["major"]["w"] == 1
    assert s3.tier_stats["altcoin"]["w"] == 0
    print("OK: tier tracking clasifica y acumula correctamente")

    # ── 4) Respaldo diario ──
    async def _run_backup_tests():
        tmp4 = tempfile.mktemp(suffix=".json")
        s4 = StateManager(tmp4)
        await _send_daily_backup(s4, FakeNotifierFail())
        assert s4.needs_daily_backup(), "NO deberia marcarse enviado si send_direct fallo"

        tmp5 = tempfile.mktemp(suffix=".json")
        s5 = StateManager(tmp5)
        await _send_daily_backup(s5, FakeNotifierOk())
        assert not s5.needs_daily_backup(), "SI deberia marcarse enviado con confirmacion real"

        tmp6 = tempfile.mktemp(suffix=".json")
        s6 = StateManager(tmp6)
        await _send_daily_backup(s6, FakeNotifierDisabled())
        assert not s6.needs_daily_backup(), "con Telegram deshabilitado deberia marcarse igual, sin reintentar"

        s7 = StateManager(tempfile.mktemp(suffix=".json"))
        for i in range(5):
            s7.positions[f"SYM{i}-USDT"] = {"dir": "SHORT", "entry": 1.0, "sl": 1.1, "tp": 0.9, "tier": "altcoin"}
        snap = s7.backup_snapshot_json(include_positions=False)
        msg = format_backup(snap, 10, 20, 33.3)
        assert len(msg) < 4096, f"mensaje de {len(msg)} caracteres excede el limite de Telegram"
        print(f"OK: respaldo diario -- fallo no marca, exito si marca, deshabilitado marca sin reintentar, snapshot cabe en Telegram ({len(msg)} chars)")

    asyncio.run(_run_backup_tests())

    # ── 5) Sesgo HTF ──
    async def _run_htf_tests():
        bearish_candles = [make_candle(i, 100 - i, 100 - i, 99 - i, 99.5 - i, 1000) for i in range(80)]
        client_bear = FakeClientHTF(bearish_candles)
        allowed = await _htf_bias_bearish(client_bear, "TEST-USDT")
        assert allowed, "con tendencia HTF bajista, el short deberia permitirse"

        bullish_candles = [make_candle(i, 50 + i, 51 + i, 49 + i, 50.5 + i, 1000) for i in range(80)]
        client_bull = FakeClientHTF(bullish_candles)
        blocked = await _htf_bias_bearish(client_bull, "TEST-USDT")
        assert not blocked, "con tendencia HTF alcista, el short deberia BLOQUEARSE"
        print("OK: sesgo HTF permite short en tendencia bajista y lo bloquea en tendencia alcista")

    asyncio.run(_run_htf_tests())

    for p in (tmp, tmp2, tmp3):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

    print()
    print("=" * 50)
    print("TEST DE LAS CUATRO MEJORAS PASÓ")


run()
