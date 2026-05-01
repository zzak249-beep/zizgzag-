"""
config.py — Carga y valida todas las variables de entorno del bot.
"""
import os
from dotenv import load_dotenv

load_dotenv()

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"❌ Variable de entorno requerida no definida: {key}")
    return val

def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))

def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))

# ---- BingX ----
BINGX_API_KEY    = _require("BINGX_API_KEY")
BINGX_SECRET_KEY = _require("BINGX_SECRET_KEY")
BINGX_BASE_URL   = "https://open-api.bingx.com"

# ---- Telegram ----
TELEGRAM_TOKEN   = _require("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _require("TELEGRAM_CHAT_ID")

# ---- Estrategia ----
SYMBOL             = os.getenv("SYMBOL", "BTC-USDT")
LEVERAGE           = _int("LEVERAGE", 10)
TP_PIPS            = _float("TP_PIPS", 45)
SL_PIPS            = _float("SL_PIPS", 30)
TIMEFRAME          = os.getenv("TIMEFRAME", "15m")
CAPITAL_PER_TRADE  = _float("CAPITAL_PER_TRADE", 50)  # USDT

# ---- ZigZag ----
ZZ_DEPTH      = _int("ZZ_DEPTH", 12)
ZZ_DEVIATION  = _int("ZZ_DEVIATION", 5)
ZZ_BACKSTEP   = _int("ZZ_BACKSTEP", 2)
ZZ_LOOKBACK   = _int("ZZ_LOOKBACK", 100)

# ---- Horario ----
TRADE_START_HOUR = _int("TRADE_START_HOUR", 0)
TRADE_END_HOUR   = _int("TRADE_END_HOUR", 23)

# ---- General ----
ENV       = os.getenv("ENV", "production")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ---- Mapa de timeframe a ms para BingX ----
TF_MAP = {
    "1m":  "1",
    "3m":  "3",
    "5m":  "5",
    "15m": "15",
    "30m": "30",
    "1h":  "60",
    "4h":  "240",
    "1d":  "D",
}
BINGX_INTERVAL = TF_MAP.get(TIMEFRAME, "15")
