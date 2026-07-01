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
TIMEFRAME  = _str("TIMEFRAME", "5m")
DIRECTION  = _str("DIRECTION", "BOTH")   # LONG | SHORT | BOTH
LEVERAGE   = _int("LEVERAGE", 10)

# ── Strategy selector
# EMA9_VWAP  = solo cruce EMA9 × VWAP
# PDH_BOS    = solo PDH/PDL Break of Structure + retest
# BOTH       = PDH_BOS prioritario, EMA9_VWAP como fallback
STRATEGY   = _str("STRATEGY", "BOTH")

# ── EMA9 × VWAP params
ATR_LENGTH = _int("ATR_LENGTH", 14)
ATR_MULT   = _float("ATR_MULT", 2.0)
EMA_PERIOD = _int("EMA_PERIOD", 9)
CANDLES    = _int("CANDLES", 300)

# ── PDH BOS Retest params
PDH_RETEST_ZONE_PCT = _float("PDH_RETEST_ZONE_PCT", 0.15)  # % zona retest
SL_ATR              = _float("SL_ATR",  1.5)               # SL en ATRs
TP1_ATR             = _float("TP1_ATR", 3.0)               # TP en ATRs
EMA8_EXIT           = _bool("EMA8_EXIT", "true")           # exit EMA8 break

# ── Unicorn Model params
UNICORN_PIVOT_LEN   = _int("UNICORN_PIVOT_LEN",   5)     # velas para pivot HTF
UNICORN_SWEEP_LB    = _int("UNICORN_SWEEP_LB",   30)     # lookback sweep en 5m
UNICORN_REQUIRE_FVG = _bool("UNICORN_REQUIRE_FVG","true") # requiere FVG (modo Unicorn)
UNICORN_RR          = _float("UNICORN_RR",        2.0)   # R/R target

# ── Fibonacci Golden Pocket params
FIB_LOOKBACK      = _int("FIB_LOOKBACK", 50)       # velas para detectar swing
FIB_ZONE_PCT      = _float("FIB_ZONE_PCT", 0.001)  # tolerancia zona 0.1%
FIB_RSI_LONG_MIN  = _float("FIB_RSI_LONG_MIN", 35) # RSI mín para LONG
FIB_RSI_LONG_MAX  = _float("FIB_RSI_LONG_MAX", 60) # RSI máx para LONG
FIB_RSI_SHORT_MIN = _float("FIB_RSI_SHORT_MIN", 40)
FIB_RSI_SHORT_MAX     = _float("FIB_RSI_SHORT_MAX", 65)
FIB_EXTREME_RSI_LONG  = _float("FIB_EXTREME_RSI_LONG",  28) # override tendencia si RSI <= 28
FIB_EXTREME_RSI_SHORT = _float("FIB_EXTREME_RSI_SHORT", 72) # override tendencia si RSI >= 72

# ── Risk
RISK_PCT            = _float("RISK_PCT", 1.0)
MAX_DAILY_LOSS_PCT  = _float("MAX_DAILY_LOSS_PCT", 3.0)
FIXED_NOTIONAL_USDT = _float("FIXED_NOTIONAL_USDT", 15.0)
MIN_NOTIONAL_USDT   = _float("MIN_NOTIONAL_USDT", 10.0)
MAX_NOTIONAL_USDT   = _float("MAX_NOTIONAL_USDT", 200.0)

# ── Loop timing
TRAILING_CHECK_SEC  = _int("TRAILING_CHECK_SEC", 30)
SIGNAL_CHECK_SEC    = _int("SIGNAL_CHECK_SEC", 90)

# ── Telegram
TELEGRAM_TOKEN = _str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = _str("TELEGRAM_CHAT_ID")
