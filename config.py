"""config.py — Configuración central Sniper Bot V50 Ultimate"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Exchange ─────────────────────────────────────────────────
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "")
SYMBOL           = os.getenv("SYMBOL", "BTC/USDT")
TIMEFRAME        = os.getenv("TIMEFRAME", "5m")
LEVERAGE         = int(os.getenv("LEVERAGE", "3"))
MODE             = os.getenv("MODE", "paper")          # paper | live
MAX_TRADES_DAY   = int(os.getenv("MAX_TRADES_DAY", "6"))

# ─── Telegram ─────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Riesgo ───────────────────────────────────────────────────
RISK_PCT         = float(os.getenv("RISK_PCT", "2.0"))
ATR_MULT_TP      = 2.0
ATR_MULT_SL      = 1.2
MAX_BARS_HOLD    = 20

# ─── Motor Markov (V49) ───────────────────────────────────────
SLOPE_MIN        = 30.0
LOOKBACK_MARKOV  = 200
PROB_THRESHOLD   = 40.0

# ─── ADX Adaptativo ───────────────────────────────────────────
ADX_LEN          = 14
ADX_TREND        = 25
ADX_RANGE        = 20

# ─── Filtros institucionales ──────────────────────────────────
PIVOT_LEN        = 4
RVOL_MIN         = 1.5
POC_LOOKBACK     = 50

# ─── Funding Rate ─────────────────────────────────────────────
FUNDING_THRESHOLD = float(os.getenv("FUNDING_THRESHOLD", "0.01"))
FUNDING_AVOID     = os.getenv("FUNDING_AVOID", "true").lower() == "true"

# ─── Liquidation Map ──────────────────────────────────────────
LIQ_LOOKBACK     = int(os.getenv("LIQ_LOOKBACK", "100"))
LIQ_MULTIPLIER   = float(os.getenv("LIQ_MULTIPLIER", "1.5"))

# ─── Loop ─────────────────────────────────────────────────────
CANDLES_LIMIT    = 500
LOOP_INTERVAL    = 60
