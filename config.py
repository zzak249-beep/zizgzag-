"""config.py — Variables de entorno del bot multi-símbolo."""
import os
from dotenv import load_dotenv
load_dotenv()

def _req(k):
    v = os.getenv(k)
    if not v:
        raise EnvironmentError(f"❌ Variable requerida: {k}")
    return v

def _int(k, d):   return int(os.getenv(k, str(d)))
def _float(k, d): return float(os.getenv(k, str(d)))

# ── BingX ──────────────────────────────────────────────
BINGX_API_KEY    = _req("BINGX_API_KEY")
BINGX_SECRET_KEY = _req("BINGX_SECRET_KEY")
BINGX_BASE_URL   = "https://open-api.bingx.com"

# ── Telegram ───────────────────────────────────────────
TELEGRAM_TOKEN   = _req("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _req("TELEGRAM_CHAT_ID")

# ── Estrategia ─────────────────────────────────────────
LEVERAGE           = _int("LEVERAGE", 10)
TP_PIPS            = _float("TP_PIPS", 45)
SL_PIPS            = _float("SL_PIPS", 30)
TIMEFRAME          = os.getenv("TIMEFRAME", "15m")
CAPITAL_PER_TRADE  = _float("CAPITAL_PER_TRADE", 30)

# ── Multi-símbolo ──────────────────────────────────────
MAX_SYMBOLS        = _int("MAX_SYMBOLS", 30)
MIN_VOLUME_USDT    = _float("MIN_VOLUME_USDT", 5000000)
MAX_OPEN_POSITIONS = _int("MAX_OPEN_POSITIONS", 5)
SCAN_DELAY_SEC     = _float("SCAN_DELAY_SEC", 0.4)

# ── ZigZag ─────────────────────────────────────────────
ZZ_DEPTH     = _int("ZZ_DEPTH", 12)
ZZ_DEVIATION = _int("ZZ_DEVIATION", 5)
ZZ_BACKSTEP  = _int("ZZ_BACKSTEP", 2)
ZZ_LOOKBACK  = _int("ZZ_LOOKBACK", 100)

# ── Horario UTC ────────────────────────────────────────
TRADE_START_HOUR = _int("TRADE_START_HOUR", 0)
TRADE_END_HOUR   = _int("TRADE_END_HOUR", 23)

# ── General ────────────────────────────────────────────
ENV       = os.getenv("ENV", "production")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

TF_MAP = {"1m":"1","3m":"3","5m":"5","15m":"15",
          "30m":"30","1h":"60","4h":"240","1d":"D"}
BINGX_INTERVAL = TF_MAP.get(TIMEFRAME, "15")
