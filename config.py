"""
Configuración del bot de arranque de impulso.

MODE=SIGNAL por defecto y con motivo: esta estrategia tiene CERO
operaciones medidas, y su prima hermana (ruptura de rango) perdió con
482 operaciones en tres símbolos. Lo que aquí se añade —compresión
previa, volumen y sobre todo el filtro de "que sea pronto"— puede ser
la diferencia o puede no serlo. Mídelo con backtest.py antes de nada.
"""
import os


def _bool(n, d=False):
    return os.getenv(n, str(d)).strip().lower() in ("1", "true", "yes", "si", "sí")


def _float(n, d):
    try:
        return float(os.getenv(n, d))
    except (TypeError, ValueError):
        return d


def _int(n, d):
    try:
        return int(os.getenv(n, d))
    except (TypeError, ValueError):
        return d


MODE = os.getenv("MODE", "SIGNAL").strip().upper()
LIVE_CONFIRMED = _bool("LIVE_CONFIRMED", False)

BINGX_API_KEY = os.getenv("BINGX_API_KEY", "").strip()
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "").strip()
BINGX_BASE_URL = os.getenv("BINGX_BASE_URL", "https://open-api.bingx.com").strip()

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()

# ── Los cinco parámetros del patrón ───────────────────────────────────
TIMEFRAME = os.getenv("TIMEFRAME", "30m").strip()
ATR_LEN = _int("ATR_LEN", 14)
MA_LEN = _int("MA_LEN", 20)
VOL_LEN = _int("VOL_LEN", 20)

# Compresión: rango de las N velas previas medido en ATR. Más bajo =
# más exigente. Un pump nace de la calma; si venía dando bandazos, la
# ruptura es una más del montón.
COMPRESSION_LEN = _int("COMPRESSION_LEN", 12)
MAX_COMPRESSION_ATR = _float("MAX_COMPRESSION_ATR", 3.0)

# Expansión: la vela que rompe debe ser mucho más ancha que el ATR y
# cerrar arriba. Una vela ancha que cierra por la mitad es indecisión.
MIN_EXPANSION_ATR = _float("MIN_EXPANSION_ATR", 1.8)
MIN_CLOSE_POS = _float("MIN_CLOSE_POS", 0.66)

MIN_VOL_MULT = _float("MIN_VOL_MULT", 2.0)

# EL FILTRO QUE LO DIFERENCIA DE LA RUPTURA QUE YA FALLÓ: si el precio
# ya está a más de esto de su media, el movimiento YA ocurrió.
# INTERACCIÓN A TENER EN CUENTA (destapada al probar el motor): la
# propia vela de arranque consume estirón. Si se exige una vela de 1.8
# ATR y a la vez un estirón máximo de 2.0, la ventana es casi
# imposible. Este umbral tiene que ser bastante MAYOR que
# MIN_EXPANSION_ATR o el bot no disparará nunca — el mismo tipo de
# choque entre dos filtros que ya apareció con amplitud y coste.
MAX_STRETCH_AT_ENTRY = _float("MAX_STRETCH_AT_ENTRY", 3.5)

# ── Salida ────────────────────────────────────────────────────────────
SL_ATR = _float("SL_ATR", 0.5)
RR_TARGET = _float("RR_TARGET", 4.0)   # objetivo lejano: el trailing hace el trabajo
TRAIL_ATR = _float("TRAIL_ATR", 2.0)
TRAIL_LOOKBACK = _int("TRAIL_LOOKBACK", 3)

# ── Coste y liquidez ──────────────────────────────────────────────────
COST_ROUNDTRIP_PCT = _float("COST_ROUNDTRIP_PCT", 0.25)
MIN_ATR_PCT = _float("MIN_ATR_PCT", 1.0)
MIN_COST_COVER = _float("MIN_COST_COVER", 6.0)
MAX_COST_IN_R = _float("MAX_COST_IN_R", 0.20)
MAX_RISK_PCT = _float("MAX_RISK_PCT", 4.0)
MIN_QUOTE_VOLUME_24H = _float("MIN_QUOTE_VOLUME_24H", 2_000_000.0)

# ── Universo ──────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = _int("SCAN_INTERVAL_SEC", 120)
MAX_SYMBOLS = _int("MAX_SYMBOLS", 400)
SCAN_CONCURRENCY = _int("SCAN_CONCURRENCY", 8)
SYMBOL_WHITELIST = [s.strip().upper() for s in os.getenv("SYMBOL_WHITELIST", "").split(",") if s.strip()]
EXCLUDE_PREFIXES = [p.strip().upper() for p in os.getenv("EXCLUDE_PREFIXES", "NC").split(",") if p.strip()]

# ── Riesgo ────────────────────────────────────────────────────────────
RISK_PCT = _float("RISK_PCT", 0.25)
# LÍMITE GLOBAL DE LA CUENTA: cuenta TODAS las posiciones abiertas en
# BingX, las de este bot y las de cualquier otro.
# Varios bots comparten cuenta y ninguno sabe de los otros. Con 2
# posiciones por bot y tres bots, el riesgo "declarado" sería la suma de
# riesgos independientes; pero la correlación entre criptos pasa de ~0.30
# en calma a 0.77-0.90 en un desplome, así que en el momento malo esas
# posiciones se mueven como UNA SOLA apuesta multiplicada. La
# diversificación entre alts es una ilusión justo cuando hace falta.
MAX_TOTAL_POSITIONS = _int("MAX_TOTAL_POSITIONS", 3)

MAX_CONCURRENT = _int("MAX_CONCURRENT", 2)
LEVERAGE = _int("LEVERAGE", 2)
MAX_CONSECUTIVE_LOSSES = _int("MAX_CONSECUTIVE_LOSSES", 4)
COOLDOWN_MINUTES = _int("COOLDOWN_MINUTES", 180)
ENTRY_TYPE = os.getenv("ENTRY_TYPE", "MARKET").strip().upper()
LIMIT_OFFSET_PCT = _float("LIMIT_OFFSET_PCT", 0.05)

SIGNAL_COOLDOWN_MIN = _int("SIGNAL_COOLDOWN_MIN", 120)
DAILY_SUMMARY = _bool("DAILY_SUMMARY", True)
DAILY_SUMMARY_HOUR_UTC = _int("DAILY_SUMMARY_HOUR_UTC", 7)
HEARTBEAT_HOURS = _int("HEARTBEAT_HOURS", 12)
IDLE_ALERT_DAYS = _int("IDLE_ALERT_DAYS", 7)

STATE_PATH = os.getenv("STATE_PATH", "/data/state_impulse.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()


def is_live() -> bool:
    return MODE == "LIVE" and LIVE_CONFIRMED and bool(BINGX_API_KEY) and bool(BINGX_API_SECRET)


def describe() -> str:
    if is_live():
        return "LIVE — enviando órdenes reales a BingX"
    if MODE == "LIVE":
        return "LIVE pedido pero SIN confirmar — sigue en SIGNAL"
    return "SIGNAL — solo avisos, no toca el exchange"
