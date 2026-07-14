"""
config.py — PUMP FADE BOT (short a parabólicos agotados)

Estrategia: monedas que subieron mucho en 24h (BSB/EVAA/OWL/SXT/LABU style),
tocan techo con rechazo, quiebran estructura (CHoCH bajista en 5m) y se
shortea el PRIMER retest del nivel roto. La imagen espejo de zesty-reverence.

REGLA DE ORO cableada en todo el diseño: NUNCA shortear la subida en sí.
Solo se entra DESPUÉS de techo + quiebre + retest, con jump-guard en block.

Todas las variables se pisan por env var en Railway.
"""
import os


def _f(name, default):
    try:
        return float(str(os.getenv(name, default)).strip().split()[0])
    except (ValueError, IndexError):
        return float(default)


def _i(name, default):
    try:
        return int(float(str(os.getenv(name, default)).strip().split()[0]))
    except (ValueError, IndexError):
        return int(default)


def _b(name, default):
    v = str(os.getenv(name, default)).strip().lower()
    return v in ("1", "true", "yes", "on")


def _s(name, default):
    return str(os.getenv(name, default)).strip()


CODE_VERSION = "2026-07-13-pumpfade-v1"

# ── Modo ──────────────────────────────────────────────────────────────
# ARRANCA EN DRY_RUN: este bot shortea los activos más violentos del día.
# Se pasa a real recién después de ver señales en seco 3-5 días.
DRY_RUN = _b("DRY_RUN", True)

# ── BingX ─────────────────────────────────────────────────────────────
BINGX_API_KEY = _s("BINGX_API_KEY", "")
BINGX_API_SECRET = _s("BINGX_API_SECRET", "")
BINGX_BASE_URL = _s("BINGX_BASE_URL", "https://open-api.bingx.com")

# ── Universo: los ganadores del día ──────────────────────────────────
PUMP_MIN_24H_PCT = _f("PUMP_MIN_24H_PCT", 25.0)    # mínimo +25% en 24h
PUMP_MAX_24H_PCT = _f("PUMP_MAX_24H_PCT", 300.0)   # >300% = degenerado, fuera
MIN_24H_VOLUME_USDT = _f("MIN_24H_VOLUME_USDT", 5_000_000)  # lección LAB:
# el piso aplica SIEMPRE — en día de pump el volumen explota, así que los
# pumps reales pasan; lo que filtra es la chicharra ilíquida donde el
# propio stop mueve el precio.
TOP_GAINERS_N = _i("TOP_GAINERS_N", 25)
REQUIRE_USDT_QUOTE = _b("REQUIRE_USDT_QUOTE", True)
NON_CRYPTO_PREFIXES = tuple(
    p.strip() for p in _s(
        "NON_CRYPTO_PREFIXES",
        "XAU,XAG,EUR,GBP,JPY,AUD,CHF,CAD,NZD,SPX,NAS,DJI,US30,US500,USTEC,OIL,WTI,BRENT",
    ).split(",") if p.strip()
)

# ── Timeframe y datos ────────────────────────────────────────────────
ENTRY_TF = _s("ENTRY_TF", "5m")
KLINES_LIMIT = _i("KLINES_LIMIT", 400)      # ~33h de velas 5m
DAY_BARS = _i("DAY_BARS", 288)              # ventana del "techo del día" (24h)
CEILING_MAX_AGE_BARS = _i("CEILING_MAX_AGE_BARS", 96)  # techo válido <= 8h

# ── Detección del setup (techo -> CHoCH -> retest) ───────────────────
STRUCT_PIVOT_LEN = _i("STRUCT_PIVOT_LEN", 5)       # swings 5/5 (estándar)
REJECT_MAX_CLOSE_POS = _f("REJECT_MAX_CLOSE_POS", 0.40)  # el techo debe
# cerrar en el 40% inferior de su propia vela (rechazo real, no doji)
CEILING_MIN_RANGE_ATR = _f("CEILING_MIN_RANGE_ATR", 0.5)
RETEST_TOUCH_ATR = _f("RETEST_TOUCH_ATR", 0.10)    # tocar el nivel roto
RETEST_BREAK_ATR = _f("RETEST_BREAK_ATR", 0.05)    # cierre de rechazo
RECLAIM_ATR = _f("RECLAIM_ATR", 0.75)              # cierre > nivel+0.75ATR
# = reclaim: el quiebre era falso, setup muerto
PUMP_MAX_RETEST = _i("PUMP_MAX_RETEST", 1)         # Raschke: SOLO el primer
# retest — el #2+ entra contra una zona gastada y acá no hay margen de error

# ── SL / TP (mismas reglas que renewed-love + guía anti-caza manual) ─
SL_ATR_BUFFER = _f("SL_ATR_BUFFER", 0.4)
MIN_SL_DIST_PCT = _f("MIN_SL_DIST_PCT", 1.0)   # piso: dentro del ruido no hay stop
MAX_SL_DIST_PCT = _f("MAX_SL_DIST_PCT", 4.0)   # techo: si la estructura pide
# más de 4%, el parabólico está demasiado salvaje — se pasa, no se opera
RR = _f("RR", 2.0)

# ── Riesgo (más conservador que renewed-love: esto es contra-tendencia) ─
RISK_PCT_PER_TRADE = _f("RISK_PCT_PER_TRADE", 1.0)
MAX_ACTIVE_POSITIONS = _i("MAX_ACTIVE_POSITIONS", 3)
MAX_CONCURRENT_RISK_PCT = _f("MAX_CONCURRENT_RISK_PCT", 3.0)
DAILY_MAX_LOSS_PCT = _f("DAILY_MAX_LOSS_PCT", 8.0)
MAX_CONSECUTIVE_LOSSES = _i("MAX_CONSECUTIVE_LOSSES", 4)
LOSS_STREAK_PAUSE_MIN = _i("LOSS_STREAK_PAUSE_MIN", 60)
LEVERAGE = _i("LEVERAGE", 5)
MIN_NOTIONAL_USDT = _f("MIN_NOTIONAL_USDT", 10.0)
MIN_NOTIONAL_MAX_RISK_PCT = _f("MIN_NOTIONAL_MAX_RISK_PCT", 1.5)

# ── Jump guard: acá BLOCK de fábrica ─────────────────────────────────
# En renewed-love arrancó en "log" para validar. En ESTE bot el chase es
# letal por definición (shortear el latigazo de un parabólico = peor
# slippage posible), así que el guard nace bloqueando. Cambiable por env.
JUMP_GUARD_MODE = _s("JUMP_GUARD_MODE", "block")
JUMP_THRESH = _f("JUMP_THRESH", 4.0)
JUMP_WIN = _i("JUMP_WIN", 50)
JUMP_COOLDOWN_BARS = _i("JUMP_COOLDOWN_BARS", 2)

# ── Loop / infraestructura ───────────────────────────────────────────
SCAN_INTERVAL_S = _i("SCAN_INTERVAL_S", 90)
DATA_DIR = _s("DATA_DIR", "/data")
STATE_FILE = os.path.join(DATA_DIR, "pumpfade_state.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "pumpfade_journal.json")
API_MAX_CONCURRENCY = _i("API_MAX_CONCURRENCY", 4)
