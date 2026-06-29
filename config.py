import os


def _bool(k, d="false"):
    return os.getenv(k, d).strip().split("#")[0].strip().lower() in ("1", "true", "yes")

def _float(k, d):
    try:
        return float(os.getenv(k, str(d)).strip().split("#")[0].strip())
    except:
        return d

def _int(k, d):
    try:
        return int(os.getenv(k, str(d)).strip().split("#")[0].strip())
    except:
        return d

def _str(k, d=""):
    return os.getenv(k, d).strip().split("#")[0].strip()


# ── Identity
BOT_NAME   = _str("BOT_NAME", "ema9-vwap")

# ── BingX
API_KEY    = _str("BINGX_API_KEY")
SECRET_KEY = _str("BINGX_SECRET_KEY")
BASE_URL   = "https://open-api.bingx.com"

# ── Trading
SYMBOL     = _str("SYMBOL", "BTC-USDT")
TIMEFRAME  = _str("TIMEFRAME", "5m")        # 1m 3m 5m 15m 30m 1h
DIRECTION  = _str("DIRECTION", "BOTH")      # LONG | SHORT | BOTH
LEVERAGE   = _int("LEVERAGE", 10)

# ── Strategy params (matches Pine Script defaults)
ATR_LENGTH = _int("ATR_LENGTH", 14)
ATR_MULT   = _float("ATR_MULT", 2.0)
EMA_PERIOD = _int("EMA_PERIOD", 9)
CANDLES    = _int("CANDLES", 300)           # lookback window

# ── Risk
RISK_PCT          = _float("RISK_PCT", 1.0)          # % equity per trade
MAX_NOTIONAL_USDT = _float("MAX_NOTIONAL_USDT", 200.0)
MAX_DAILY_LOSS_PCT = _float("MAX_DAILY_LOSS_PCT", 3.0)

# ── Loop timing
TRAILING_CHECK_SEC = _int("TRAILING_CHECK_SEC", 20)
SIGNAL_CHECK_SEC   = _int("SIGNAL_CHECK_SEC", 60)

# ── Telegram
TELEGRAM_TOKEN = _str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = _str("TELEGRAM_CHAT_ID")
