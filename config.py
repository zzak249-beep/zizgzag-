"""
Configuracion del scanner multi-simbolo del patron "Tres Montañas /
Tercer Techo Descendente" -- misma logica de pattern.py que
gold-three-mountains-bot, pero corrida sobre TODOS los simbolos USDT-M
de BingX en vez de uno solo.

DECISIONES IMPORTANTES:

1. EXCLUDE_TOKENIZED_ASSETS=true por defecto. bingx-ict-scanner (el
   scanner de sweep+FVG de este mismo proyecto) NUNCA tuvo este filtro
   -- solo fibstruct-bot lo tenia. Al escanear TODOS los simbolos, este
   scanner SI se encontraria con los NC-prefijo (NCS=acciones,
   NCCO=materias primas, NCFX=forex) -- instrumentos con horario de
   mercado real, no 24/7, que pueden generar velas de 1h con gaps
   extraños durante cierres de mercado y confundir la deteccion de
   pivotes. Reutiliza el mismo prefijo raiz "NC" ya confirmado en
   fibstruct-bot, no una lista nueva sin verificar.

2. MAX_CONCURRENT_POSITIONS -- limite explicito. Sin esto, si el patron
   fuera a dispararse en muchos simbolos a la vez (un dia de mercado
   inusualmente direccional), el bot abriria tantas posiciones como
   señales encuentre, sin ningun techo. Con una estrategia nunca
   backtesteada, eso es exposicion sin control, no solo velocidad.

3. MAX_TRADES_PER_DAY es GLOBAL aqui (no por simbolo como en el bot de
   un solo simbolo) -- con cientos de simbolos en juego, un limite por
   simbolo no protege nada; el limite tiene que ser sobre el TOTAL de
   operaciones del dia en todo el universo.

4. MODE=SIGNAL por defecto, igual que TODOS los bots de este proyecto.
   Con MAS simbolos en juego que en el bot original de un solo simbolo,
   hay MAS razon para la cautela, no menos.
"""
import os


def _clean(v):
    return v.strip() if isinstance(v, str) else v


def _bool(name: str, default: bool) -> bool:
    v = _clean(os.getenv(name))
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = _clean(os.getenv(name))
    try:
        return int(v) if v else default
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    v = _clean(os.getenv(name))
    try:
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


def _set(name: str, default: str) -> set:
    v = _clean(os.getenv(name, default)) or ""
    return {s.strip() for s in v.split(",") if s.strip()}


CODE_VERSION = "gold-3mountains-scanner v1.0.0"

# ── BingX ──
API_KEY = _clean(os.getenv("BINGX_API_KEY", ""))
API_SECRET = _clean(os.getenv("BINGX_API_SECRET", ""))
BASE_URL = _clean(os.getenv("BINGX_BASE_URL")) or "https://open-api.bingx.com"

# ── Universo de simbolos ──
QUOTE_ASSET = _clean(os.getenv("QUOTE_ASSET")) or "USDT"
SYMBOL_BLACKLIST = _set("SYMBOL_BLACKLIST", "")
SYMBOL_WHITELIST = _set("SYMBOL_WHITELIST", "")
MIN_24H_VOLUME_USDT = _float("MIN_24H_VOLUME_USDT", 0.0)
SYMBOL_REFRESH_MIN = _int("SYMBOL_REFRESH_MIN", 60)

# Ver nota (1) arriba.
EXCLUDE_TOKENIZED_ASSETS = _bool("EXCLUDE_TOKENIZED_ASSETS", True)
_TOKENIZED_PREFIXES = ("NC",)

# ── Timeframe -- fijo a 1h, el patron lo exige (ver pattern.py) ──
TIMEFRAME = "1h"
CANDLE_LIMIT = _int("CANDLE_LIMIT", 150)
SCAN_INTERVAL_SEC = _int("SCAN_INTERVAL_SEC", 300)
MAX_CONCURRENT_FETCHES = _int("MAX_CONCURRENT_FETCHES", 12)  # llamadas API en paralelo al escanear el universo

# ── Modo ──
MODE = (_clean(os.getenv("MODE")) or "SIGNAL").upper()  # SIGNAL | LIVE

# ── Parametros del patron (identicos a gold-three-mountains-bot) ──
PIVOT_LEN = _int("PIVOT_LEN", 3)
ZONE_TOLERANCE_PCT = _float("ZONE_TOLERANCE_PCT", 0.5)
PEAK3_BELOW_ZONE_PCT_MIN = _float("PEAK3_BELOW_ZONE_PCT_MIN", 0.15)
REQUIRE_WEAK_PUSH = _bool("REQUIRE_WEAK_PUSH", True)
WEAK_PUSH_MAX_RATIO = _float("WEAK_PUSH_MAX_RATIO", 0.85)

# ── Riesgo ──
RISK_PCT = _float("RISK_PCT", 1.0)
SL_BUFFER_ATR_MULT = _float("SL_BUFFER_ATR_MULT", 0.3)
RR_RATIO = _float("RR_RATIO", 2.0)
MIN_RR = _float("MIN_RR", 1.5)
ATR_LEN = _int("ATR_LEN", 14)

# ── Limites de exposicion (ver notas 2 y 3 arriba) ──
MAX_CONCURRENT_POSITIONS = _int("MAX_CONCURRENT_POSITIONS", 5)
MAX_TRADES_PER_DAY = _int("MAX_TRADES_PER_DAY", 10)

# ── Circuit breaker -- ver la nota completa en state.py sobre por que
# esto usa conteo de perdidas en vez de % de drawdown (no hay P&L en $
# trackeado todavia en este bot). Nunca toca posiciones ya abiertas,
# solo frena señales NUEVAS. ──
USE_CIRCUIT_BREAKER = _bool("USE_CIRCUIT_BREAKER", True)
LOSS_STREAK_THRESHOLD = _int("LOSS_STREAK_THRESHOLD", 3)
MAX_DAILY_LOSSES = _int("MAX_DAILY_LOSSES", 3)

# ── Tier tracking -- mismos majors que bingx-ict-scanner, para poder
# comparar la misma pregunta (¿el patron funciona distinto en majors
# que en altcoins?) con datos propios de este bot desde el primer trade.
MAJOR_SYMBOLS = _set("MAJOR_SYMBOLS", "BTC-USDT,ETH-USDT,XRP-USDT,BNB-USDT,SOL-USDT")

# ── Sesgo HTF -- filtro de tendencia en un timeframe superior antes de
# aceptar el SHORT. Solo se consulta para simbolos donde el patron YA
# es valido (no en cada simbolo del universo cada ciclo) -- evita
# duplicar las llamadas a la API sin necesidad. ──
USE_HTF_BIAS = _bool("USE_HTF_BIAS", True)
HTF_TIMEFRAME = _clean(os.getenv("HTF_TIMEFRAME")) or "4h"
HTF_EMA_LEN = _int("HTF_EMA_LEN", 50)

# ── Persistencia ──
STATE_FILE = _clean(os.getenv("STATE_FILE")) or "/data/state.json"

# ── Telegram ──
TELEGRAM_BOT_TOKEN = _clean(os.getenv("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean(os.getenv("TELEGRAM_CHAT_ID", ""))

# ── Healthcheck ──
HEALTHCHECK_PORT = _int("PORT", 8080)
