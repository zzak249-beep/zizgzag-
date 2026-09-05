"""
config.py — Configuración del Sweep Reversal Map Bot.

Todo se lee de variables de entorno (Railway → Variables). Los valores
por defecto reproducen los que aparecen en el log de arranque actual.
"""
import os


def _bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _str(name, default=""):
    """Quita espacios/saltos de línea accidentales: pegar variables en
    Railway suele dejar un '\\n' al final, y eso rompe cabeceras HTTP
    como X-BX-APIKEY con un ValueError críptico."""
    return os.getenv(name, default).strip()


def _int(name, default):
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return int(default)


def _float(name, default):
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return float(default)


# Nombres de variable que se encontraron y cuáles no: se imprime al
# arrancar. Sin esto, una variable con otro nombre en Railway hace que
# el bot coja el default en silencio -- y si el default es "no operar",
# el bot parece roto sin estarlo.
VARIABLES_ENCONTRADAS = {}


def _primera(nombres, default, tipo="str"):
    """Lee la primera variable de entorno que exista de la lista.

    La flota ha usado nombres distintos para el mismo interruptor
    (LIVE_TRADING, AUTO_TRADE, MODE=LIVE...). En vez de fallar en
    silencio con el default, se prueban todos y se deja constancia de
    cuál se usó.
    """
    for nombre in nombres:
        crudo = os.getenv(nombre)
        if crudo is None or not crudo.strip():
            continue
        crudo = crudo.strip()
        VARIABLES_ENCONTRADAS[nombres[0]] = f"{nombre}={crudo}"
        if tipo == "bool":
            return crudo.lower() in ("1", "true", "yes", "on", "live", "real")
        if tipo == "int":
            try:
                return int(float(crudo))
            except ValueError:
                return default
        if tipo == "float":
            try:
                return float(crudo)
            except ValueError:
                return default
        return crudo
    VARIABLES_ENCONTRADAS[nombres[0]] = f"(ninguna encontrada, default={default})"
    return default


class Config:
    # --- BingX ---
    BINGX_API_KEY = _str("BINGX_API_KEY")
    BINGX_API_SECRET = _str("BINGX_API_SECRET")
    BINGX_BASE_URL = _str("BINGX_BASE_URL", "https://open-api.bingx.com")
    BINGX_RECV_WINDOW_MS = _int("BINGX_RECV_WINDOW_MS", 5000)

    # DEMO_MODE opera contra el saldo de práctica (sufijo -VST).
    DEMO_MODE = _primera(["DEMO_MODE", "BINGX_DEMO"], False, "bool")

    # Interruptor real de ejecución. Con LIVE_TRADING=false el bot calcula
    # señales y avisa por Telegram, pero no manda ninguna orden.
    # Se aceptan los nombres que la flota ha usado para lo mismo. Si
    # ninguno existe, NO se opera: el default seguro es no mandar órdenes.
    LIVE_TRADING = _primera(
        ["LIVE_TRADING", "AUTO_TRADE", "TRADING_ACTIVO", "MODE", "LIVE_CONFIRMED"],
        False, "bool")

    # --- Telegram ---
    TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID")

    # --- Universo y ritmo ---
    SYMBOLS = _primera(["SYMBOLS", "SYMBOL_WHITELIST"], "BTC-USDT")  # "ALL" = todos los USDT-M
    TIMEFRAME = _str("TIMEFRAME", "5m")
    POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 60)
    # El escaneo va por lotes para no abrir cientos de conexiones a la vez
    # ni comerse el rate limit de BingX.
    SYMBOL_BATCH_SIZE = _int("SYMBOL_BATCH_SIZE", 20)
    SYMBOL_BATCH_DELAY_SECONDS = _float("SYMBOL_BATCH_DELAY_SECONDS", 1.0)

    # --- Motor de señal (sweep_engine) ---
    SWING_LENGTH = _int("SWING_LENGTH", 5)
    STRUCTURE_LENGTH = _int("STRUCTURE_LENGTH", 3)
    MAX_CONFIRMATION_BARS = _int("MAX_CONFIRMATION_BARS", 12)
    MIN_DISPLACEMENT_ATR = _float("MIN_DISPLACEMENT_ATR", 0.2)
    ATR_LENGTH = _int("ATR_LENGTH", 14)

    # --- Salidas ---
    # El SL va al nivel barrido con un colchón en ATR (si se pone justo en
    # el nivel, el mismo ruido que provocó el barrido lo toca enseguida).
    SL_ATR_BUFFER = _float("SL_ATR_BUFFER", 0.3)
    TP_RR = _float("TP_RR", 2.0)

    # --- Riesgo / cuenta ---
    # OJO: QTY_PCT es porcentaje del equity en NOCIONAL, no riesgo. El
    # riesgo real de cada operación depende de dónde caiga el nivel
    # barrido y varía entre entradas.
    QTY_PCT = _primera(["QTY_PCT", "RISK_PCT", "RISK_PCT_PER_TRADE"], 10.0, "float")
    # Suelo de tamaño: valor MÍNIMO de la posición en USDT (qty × precio),
    # NO margen. Con LEVERAGE=10, 9 USDT de nocional inmovilizan 0.9 de
    # margen. Evita posiciones de céntimos en cuentas pequeñas, donde las
    # comisiones se comen cualquier resultado. 0 lo desactiva.
    MIN_NOTIONAL_USDT = _float("MIN_NOTIONAL_USDT", 9.0)
    # Freno del suelo anterior: si forzar el mínimo supera este % del
    # equity, se descarta la señal en vez de abrir algo desproporcionado.
    # 40% y no 25%: con MIN_NOTIONAL_USDT=9 y un tope del 25%, cualquier
    # equity por debajo de 36 USDT rechazaba TODAS las señales sin que
    # fuera evidente por qué. A 40% el corte baja a 22.5 USDT.
    MAX_NOTIONAL_PCT = _float("MAX_NOTIONAL_PCT", 40.0)
    LEVERAGE = _primera(["LEVERAGE"], 10, "int")
    MAX_CONCURRENT_POSITIONS = _primera(
        ["MAX_CONCURRENT_POSITIONS", "MAX_CONCURRENT", "MAX_TOTAL_POSITIONS"], 5, "int")
    MIN_BALANCE_USDT = _float("MIN_BALANCE_USDT", 0.0)
    # No abrir en un símbolo que ya tiene posición (propia o ajena): en
    # hedge se fusionarían y ningún bot sabría cuál es la suya.
    SKIP_IF_SYMBOL_HAS_POSITION = _bool("SKIP_IF_SYMBOL_HAS_POSITION", "true")

    # --- Infra ---
    HEALTH_PORT = _int("PORT", 8080)
    LOG_LEVEL = _str("LOG_LEVEL", "INFO")

    # ------------------------------------------------------------------ #
    @classmethod
    def validate(cls) -> None:
        """Falla al arrancar en vez de a mitad del primer ciclo."""
        errores = []

        if cls.LIVE_TRADING and not (cls.BINGX_API_KEY and cls.BINGX_API_SECRET):
            errores.append("LIVE_TRADING=true pero faltan BINGX_API_KEY/BINGX_API_SECRET")

        if cls.TIMEFRAME[-1].lower() not in ("m", "h", "d"):
            errores.append(f"TIMEFRAME no soportado: {cls.TIMEFRAME}")

        if cls.MAX_CONCURRENT_POSITIONS < 1:
            errores.append("MAX_CONCURRENT_POSITIONS debe ser >= 1")

        if cls.QTY_PCT <= 0 or cls.QTY_PCT > 100:
            errores.append(f"QTY_PCT fuera de rango: {cls.QTY_PCT}")

        if cls.TP_RR <= 0:
            errores.append(f"TP_RR debe ser > 0: {cls.TP_RR}")

        # Aviso, no error: es una configuración legítima pero arriesgada, y
        # conviene que quede escrito en el log del arranque.
        exposicion = cls.QTY_PCT * cls.MAX_CONCURRENT_POSITIONS
        if exposicion > 100:
            print(f"⚠️  AVISO: QTY_PCT={cls.QTY_PCT}% × {cls.MAX_CONCURRENT_POSITIONS} "
                  f"posiciones = {exposicion}% del equity en nocional simultáneo.")

        if errores:
            raise SystemExit("Configuración inválida:\n  - " + "\n  - ".join(errores))

    @classmethod
    def diagnostico(cls) -> str:
        """Por qué el bot podría no estar abriendo operaciones.

        Se imprime al arrancar. Recorre todas las puertas en el mismo
        orden en que las evalúa _handle_entry, para que la causa sea
        visible en el log en vez de haber que deducirla.
        """
        lineas = ["── Diagnóstico: ¿puede abrir operaciones? ──"]

        if not cls.LIVE_TRADING:
            lineas.append("  ❌ NO. Trading desactivado -> no se manda ninguna orden.")
            lineas.append("     Define LIVE_TRADING=true (o AUTO_TRADE=true) en Railway.")
        else:
            lineas.append("  ✅ Trading activado.")

        if not (cls.BINGX_API_KEY and cls.BINGX_API_SECRET):
            lineas.append("  ❌ Faltan BINGX_API_KEY / BINGX_API_SECRET.")

        if cls.DEMO_MODE:
            lineas.append("  ⚠️  DEMO_MODE activo: órdenes contra saldo de práctica (-VST).")

        corte = cls.MIN_NOTIONAL_USDT / (cls.MAX_NOTIONAL_PCT / 100.0) if cls.MAX_NOTIONAL_PCT else 0
        if corte:
            lineas.append(
                f"  ℹ️  Suelo de nocional {cls.MIN_NOTIONAL_USDT} USDT con tope "
                f"{cls.MAX_NOTIONAL_PCT}%: con equity < {corte:.2f} USDT se "
                f"rechazan TODAS las señales.")

        if cls.MIN_BALANCE_USDT:
            lineas.append(f"  ℹ️  MIN_BALANCE_USDT={cls.MIN_BALANCE_USDT}: por debajo no se opera.")

        lineas.append("  Variables leídas del entorno:")
        for clave, valor in sorted(VARIABLES_ENCONTRADAS.items()):
            lineas.append(f"    {clave}: {valor}")

        return "\n".join(lineas)

    @classmethod
    def summary(cls) -> str:
        modo = "DEMO/VST" if cls.DEMO_MODE else "REAL"
        trading = "ACTIVO (envía órdenes reales)" if cls.LIVE_TRADING else "SOLO SEÑALES"
        return (
            "Sweep Reversal Map — BingX\n"
            f"Modo cuenta: {modo} | Trading: {trading}\n"
            f"Símbolos: {cls.SYMBOLS} | Timeframe: {cls.TIMEFRAME}\n"
            f"qty_pct={cls.QTY_PCT}% | leverage={cls.LEVERAGE}x | "
            f"max_posiciones_simultaneas={cls.MAX_CONCURRENT_POSITIONS}\n"
            f"swing={cls.SWING_LENGTH} | structure={cls.STRUCTURE_LENGTH} | "
            f"max_confirmation_bars={cls.MAX_CONFIRMATION_BARS} | "
            f"min_displacement={cls.MIN_DISPLACEMENT_ATR}x ATR\n"
            f"SL: nivel barrido ± {cls.SL_ATR_BUFFER}x ATR | TP: RR {cls.TP_RR}x"
        )
