"""
Configuración centralizada. Todo se lee de variables de entorno
(Railway → Variables). Ver .env.example para la lista completa.
"""
import os


def _bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _str(name, default=""):
    """Lee una variable de entorno y le quita espacios/saltos de línea
    accidentales (Railway/copiar-pegar suele dejar un '\\n' al final,
    lo que rompe cabeceras HTTP como X-BX-APIKEY con un ValueError)."""
    return os.getenv(name, default).strip()


# --- BingX ---
BINGX_API_KEY = _str("BINGX_API_KEY")
BINGX_API_SECRET = _str("BINGX_API_SECRET")
BINGX_BASE_URL = _str("BINGX_BASE_URL", "https://open-api.bingx.com")
BINGX_DEMO = _bool("BINGX_DEMO", "true")  # usa VST (demo trading) por defecto

# --- Telegram ---
TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID")

# --- Webhook ---
WEBHOOK_SECRET = _str("WEBHOOK_SECRET")  # token que añades a la URL de la alerta

# --- Modo de operación ---
# AUTO_TRADE=false  -> solo reenvía la señal a Telegram, no ejecuta nada (modo manual)
# AUTO_TRADE=true   -> ejecuta la orden en BingX y además avisa por Telegram
AUTO_TRADE = _bool("AUTO_TRADE", "false")

# --- Riesgo / cuenta ---
RISK_PCT_PER_TRADE = float(os.getenv("RISK_PCT_PER_TRADE", "2.0"))  # % del equity arriesgado (via SL)
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))

# --- Circuit breaker ---
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))
MAX_DAILY_DRAWDOWN_PCT = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "6.0"))

# --- Persistencia ---
STATE_FILE = _str("STATE_FILE", "state.json")

# --- Mapeo símbolo TradingView -> BingX ---
# TradingView suele mandar "BTCUSDT" o "BTCUSDT.P". BingX quiere "BTC-USDT".
def tv_symbol_to_bingx(tv_symbol: str) -> str:
    s = tv_symbol.upper().replace(".P", "").replace("PERP", "")
    if "-" in s:
        return s
    # separar el par asumiendo que termina en USDT/USDC/USD
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote):
            base = s[: -len(quote)]
            return f"{base}-{quote}"
    return s
