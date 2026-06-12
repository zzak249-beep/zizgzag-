"""
QF×JP Bot v6.4 — Config
Variables via env vars. Defaults optimizados para estrategia SHORT.

Jerarquía de precedencia (mayor a menor):
  1. Variable de entorno en Railway / .env
  2. Default hardcodeado aquí

Añadir nueva variable → una línea con _bool/_float/_int/_list + comentario.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Helpers de parseo ─────────────────────────────────────────────────────────

def _bool(k, d):
    return os.getenv(k, str(d)).strip().lower() in ("true", "1", "yes")

def _float(k, d):
    try:
        return float(os.getenv(k, str(d)))
    except (TypeError, ValueError):
        return d

def _int(k, d):
    try:
        return int(os.getenv(k, str(d)))
    except (TypeError, ValueError):
        return d

def _list(k, d):
    raw = os.getenv(k, d).strip()
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else []

# ── BingX ─────────────────────────────────────────────────────────────────────

BINGX_API_KEY    = os.getenv("BINGX_API_KEY",    "")
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")
BINGX_BASE_URL   = "https://open-api.bingx.com"

# ── Telegram ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Modo ──────────────────────────────────────────────────────────────────────

MODE = os.getenv("MODE", "SIGNAL").upper()   # SIGNAL | LIVE

# ── Capital y riesgo ──────────────────────────────────────────────────────────

CAPITAL          = _float("CAPITAL",          1000.0)
RISK_PCT         = _float("RISK_PCT",         1.0)
LEVERAGE         = _int("LEVERAGE",           10)
MAX_OPEN_TRADES  = _int("MAX_OPEN_TRADES",    5)   # conservador
MAX_DAILY_TRADES = _int("MAX_DAILY_TRADES",   20)

# ── Umbrales de señal — optimizados al perfil SHORT ganador ───────────────────

MIN_SCORE  = _float("MIN_SCORE",  55.0)   # restaurado — 50 deja pasar señales débiles
FUEL_SCORE = _float("FUEL_SCORE", 62.0)   # Tier FUEL
SUP_SCORE  = _float("SUP_SCORE",  78.0)   # Tier SUPER
MIN_TIER   = os.getenv("MIN_TIER", "STD").upper()   # STD | FUEL | SUP

# ── Entrada ───────────────────────────────────────────────────────────────────

REQUIRE_TL_BREAK = _bool("REQUIRE_TL_BREAK", True)
HTF_MIN_ALIGNED  = _int("HTF_MIN_ALIGNED",   2)   # restaurado — con 1 entraban señales de baja calidad

# ── Scanner ───────────────────────────────────────────────────────────────────

SCAN_INTERVAL   = _int("SCAN_INTERVAL",     60)        # segundos entre ciclos
TOP_N_SYMBOLS   = _int("TOP_N_SYMBOLS",     0)         # 0 = todas
BLACKLIST       = set(_list("BLACKLIST",    ""))
MIN_VOLUME_USDT = _float("MIN_VOLUME_USDT", 5_000_000.0)

# ── Timeframes ────────────────────────────────────────────────────────────────

TIMEFRAME      = os.getenv("TIMEFRAME",      "3m")
HTF_TIMEFRAME  = os.getenv("HTF_TIMEFRAME",  "15m")
HTF2_TIMEFRAME = os.getenv("HTF2_TIMEFRAME", "1h")
HTF5_TIMEFRAME = os.getenv("HTF5_TIMEFRAME", "4h")

# ── ATR / SL / TP — ajustados al perfil ganador (cierres en 5-15 min) ─────────

ATR_LEN      = _int("ATR_LEN",       10)
SL_ATR_MULT  = _float("SL_ATR_MULT",  1.2)   # restaurado — 0.8 era demasiado ajustado
TP1_ATR_MULT = _float("TP1_ATR_MULT", 1.5)   # restaurado — 1.2 daba R:R demasiado bajo
TP2_ATR_MULT = _float("TP2_ATR_MULT", 2.5)   # TP2 realista

# ── ADX ───────────────────────────────────────────────────────────────────────

ADX_LEN     = _int("ADX_LEN",      14)
ADX_TREND   = _float("ADX_TREND",   25.0)
ADX_LATERAL = _float("ADX_LATERAL", 20.0)

# ── Kelly Criterion ───────────────────────────────────────────────────────────

KELLY_WIN_RATE = _float("KELLY_WIN_RATE", 0.60)   # win rate histórico
KELLY_RR       = _float("KELLY_RR",       1.5)    # R:R ajustado a TP1 cercano
KELLY_FRACTION = _float("KELLY_FRACTION", 0.25)   # fracción de Kelly

# ── Circuit Breaker ───────────────────────────────────────────────────────────

CB_ENABLED  = _bool("CB_ENABLED",   True)
CB_ATR_MULT = _float("CB_ATR_MULT", 3.0)
CB_BARS     = _int("CB_BARS",       10)

# ── Gestión de posiciones ─────────────────────────────────────────────────────

POSITION_CHECK_INTERVAL = _int("POSITION_CHECK_INTERVAL",   30)
BREAKEVEN_ATR_MULT      = _float("BREAKEVEN_ATR_MULT",       1.0)

# ── Puerto HTTP (Railway) ─────────────────────────────────────────────────────

PORT = _int("PORT", 8080)
