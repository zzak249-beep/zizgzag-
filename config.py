import os

# ── BINGX API
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")
BASE_URL         = "https://open-api.bingx.com"

# ── TELEGRAM
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── TIMEFRAME
# Investigación: 3m → 7-10 rupturas/día, con trigger 0.5×ATR → 1-2 señales/día/par
# 1m → 70-85 rupturas/día (mucho ruido), necesita más filtros
TIMEFRAME    = os.getenv("TIMEFRAME",     "3m")
KLINE_LIMIT  = int(os.getenv("KLINE_LIMIT", "150"))  # 150×3m = 7.5h historial
CANDLE_SLEEP = int(os.getenv("CANDLE_SLEEP", "30"))   # cada 30s para no perder cruces

# ── ZIGZAG CANAL
PIVOT_LEN        = int(os.getenv("PIVOT_LEN",         "5"))
ATR_LEN          = int(os.getenv("ATR_LEN",           "14"))
ATR_TRIGGER_MULT = float(os.getenv("ATR_TRIGGER_MULT", "0.5"))  # SHORT si >green+0.5ATR
SL_ATR_MULT      = float(os.getenv("SL_ATR_MULT",      "2.0"))  # SL = 2×ATR
MIN_CANAL_ATR    = float(os.getenv("MIN_CANAL_ATR",    "1.5"))  # canal mínimo 1.5×ATR
COOLDOWN_BARS    = int(os.getenv("COOLDOWN_BARS",      "10"))   # velas entre señales mismo par

# ── FILTROS (OFF por defecto → señales inmediatas; activar uno a uno)
USE_EMA_FILTER    = os.getenv("USE_EMA_FILTER",    "false").lower() == "true"
EMA_PERIOD        = int(os.getenv("EMA_PERIOD",    "50"))

USE_RSI_FILTER    = os.getenv("USE_RSI_FILTER",    "false").lower() == "true"
RSI_PERIOD        = int(os.getenv("RSI_PERIOD",    "14"))
RSI_SHORT_MIN     = float(os.getenv("RSI_SHORT_MIN","60"))
RSI_LONG_MAX      = float(os.getenv("RSI_LONG_MAX", "40"))

USE_VOL_FILTER    = os.getenv("USE_VOL_FILTER",    "false").lower() == "true"
VOL_MULT          = float(os.getenv("VOL_MULT",    "1.3"))

# ── TRAILING STOP
TRAIL_PCT = float(os.getenv("TRAIL_PCT", "50"))  # mover SL a BE cuando PnL=50% TP

# ── RIESGO
LEVERAGE       = int(os.getenv("LEVERAGE",       "10"))
RISK_PCT       = float(os.getenv("RISK_PCT",      "1.5"))
MAX_POSITIONS  = int(os.getenv("MAX_POSITIONS",   "5"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS","5.0"))

# ── SCANNER (investigación: óptimo 10-15 pares)
TOP_PAIRS     = int(os.getenv("TOP_PAIRS",    "15"))
MIN_QUOTE_VOL = float(os.getenv("MIN_QUOTE_VOL","50000000"))  # 50M USDT/día
MIN_PRICE_USDT= float(os.getenv("MIN_PRICE_USDT","0.0001"))
