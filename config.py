import os
from dotenv import load_dotenv

load_dotenv()

# ── API BingX ──────────────────────────────────────────────────────────────────
API_KEY    = os.getenv('BINGX_API_KEY',    '')
SECRET_KEY = os.getenv('BINGX_SECRET_KEY', '')

# ── Telegram ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN',   '')   # Bot token de @BotFather
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')   # Tu chat ID personal

# ── Estrategia ─────────────────────────────────────────────────────────────────
TIMEFRAME      = '15m'
HMA_LENGTH     = 20
ZIGZAG_WINDOW  = 5

# ── Escáner ────────────────────────────────────────────────────────────────────
TOP_PAIRS_COUNT = 5
SCAN_INTERVAL   = 60      # segundos entre ciclos

# ── Gestión de capital ─────────────────────────────────────────────────────────
ORDER_AMOUNT    = 10      # USDT por operación
LEVERAGE        = 5       # Apalancamiento

# ── Stop Loss / Take Profit ────────────────────────────────────────────────────
SL_PCT          = 0.015   # 1.5 % de Stop Loss
TP_PCT          = 0.030   # 3.0 % de Take Profit  (ratio 1:2)

# ── Modo live ──────────────────────────────────────────────────────────────────
# Cambia a True SOLO cuando estés listo para operar con dinero real
LIVE_TRADING    = os.getenv('LIVE_TRADING', 'false').lower() == 'true'
