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


# Parámetros del motor con nombres alternativos. sweep_engine.py usa el
# prefijo SWEEP_ para algunos (SWEEP_ATR_LENGTH), y no todos los módulos
# coinciden. En vez de ir descubriéndolos uno a uno con un crash por
# cada deploy, Config resuelve el prefijo automáticamente.
_PREFIJOS = ("SWEEP_", "WAVELET_")

# Parámetros del motor que no estaban en config y hubo que inventar un
# valor. NO son tuyos: son el valor NEUTRO (filtro desactivado), elegido
# para no alterar la estrategia con un umbral inventado. Si tu Pine usa
# otro, defínelo en Railway.
#
# La alternativa era que el bot siguiera cascando un símbolo tras otro.
_DEFAULTS_NEUTROS = {
    "MIN_PENETRATION_ATR": 0.0,      # 0 = no se exige penetración mínima
    "MIN_DISPLACEMENT_ATR": 0.2,     # este sí sale de tu log de arranque
    "MAX_PENETRATION_ATR": 999.0,    # sin techo
    "MIN_SWEEP_ATR": 0.0,
    "MIN_BODY_ATR": 0.0,
    "MIN_WICK_RATIO": 0.0,
    "BUFFER_ATR": 0.0,
}

# Nombres a los que se les aplicó un default neutro, para avisar al
# arrancar en vez de que pase desapercibido.
DEFAULTS_APLICADOS = {}


class _ConfigMeta(type):
    """Resuelve atributos que no existen literalmente en Config.

    Orden: variable de entorno con ese nombre exacto -> mismo nombre sin
    prefijo SWEEP_/WAVELET_ -> mismo nombre CON prefijo. Si nada encaja,
    lanza AttributeError con un mensaje que dice qué falta y dónde
    definirlo, en vez del críptico "type object 'Config' has no
    attribute".
    """

    def __getattr__(cls, name):
        # Solo se resuelven nombres de CONSTANTE (mayúsculas). Sin este
        # filtro, el metaclass intercepta también métodos y atributos
        # internos de Python y devuelve un AttributeError con un mensaje
        # sobre variables de Railway que no viene a cuento.
        if name.startswith("_") or not name.isupper():
            raise AttributeError(name)

        crudo = os.getenv(name)
        if crudo is not None and crudo.strip():
            crudo = crudo.strip()
            VARIABLES_ENCONTRADAS[name] = f"{name}={crudo} (resuelta al vuelo)"
            for conv in (int, float):
                try:
                    return conv(crudo)
                except ValueError:
                    pass
            if crudo.lower() in ("true", "false"):
                return crudo.lower() == "true"
            return crudo

        candidatos = []
        for pref in _PREFIJOS:
            if name.startswith(pref):
                candidatos.append(name[len(pref):])
            else:
                candidatos.append(pref + name)

        for alt in candidatos:
            valor = cls.__dict__.get(alt)
            if valor is not None:
                VARIABLES_ENCONTRADAS[name] = f"-> {alt} (alias)"
                return valor

        base = name
        for pref in _PREFIJOS:
            if base.startswith(pref):
                base = base[len(pref):]
                break
        if base in _DEFAULTS_NEUTROS:
            valor = _DEFAULTS_NEUTROS[base]
            DEFAULTS_APLICADOS[name] = valor
            return valor

        raise AttributeError(
            f"Config no tiene '{name}' ni un equivalente ({', '.join(candidatos)}). "
            f"Defínelo como variable de entorno en Railway o añádelo a config.py."
        )


class Config(metaclass=_ConfigMeta):
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
    # Default "ALL" (todos los perpetuos USDT-M), que es como venía
    # operando el bot. SYMBOL_WHITELIST no se acepta como alias: en tu
    # Railway está vacío, y una lista vacía no significa "solo BTC".
    SYMBOLS = _primera(["SYMBOLS"], "ALL")
    TIMEFRAME = _str("TIMEFRAME", "5m")
    POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 60)
    # El escaneo va por lotes para no abrir cientos de conexiones a la vez
    # ni comerse el rate limit de BingX.
    SYMBOL_BATCH_SIZE = _int("SYMBOL_BATCH_SIZE", 20)
    SYMBOL_BATCH_DELAY_SECONDS = _float("SYMBOL_BATCH_DELAY_SECONDS", 1.0)

    # --- Motor de señal (sweep_engine) ---
    # Los nombres son EXACTAMENTE los que lee sweep_engine.replay_signal
    # y compute_sweep_sl_tp. Los valores por defecto son los que aparecen
    # en tu log de arranque original.
    SWING_LENGTH = _int("SWING_LENGTH", 5)
    STRUCTURE_LENGTH = _int("STRUCTURE_LENGTH", 3)
    MAX_CONFIRMATION_BARS = _int("MAX_CONFIRMATION_BARS", 12)
    MIN_DISPLACEMENT_ATR = _float("MIN_DISPLACEMENT_ATR", 0.2)
    SWEEP_ATR_LENGTH = _int("SWEEP_ATR_LENGTH", _int("ATR_LENGTH", 14))
    ATR_LENGTH = SWEEP_ATR_LENGTH   # alias, mismo valor

    # Penetración mínima por encima del swing para considerar que hubo
    # barrido, en múltiplos de ATR. Con 0.0 basta con TOCAR el nivel:
    #   high[i] >= swing_high + 0
    # Es el valor neutro (filtro desactivado). Subirlo exige que el
    # barrido sea más profundo y reduce los falsos disparos por un
    # roce del nivel. No sé cuál usa tu Pine: si era otro, ponlo en
    # Railway como MIN_PENETRATION_ATR.
    MIN_PENETRATION_ATR = _float("MIN_PENETRATION_ATR", 0.0)

    # --- Salidas ---
    # El SL va al nivel barrido con un colchón en ATR (si se pone justo en
    # el nivel, el mismo ruido que provocó el barrido lo toca enseguida).
    # El TP es un múltiplo de esa distancia de riesgo (RR).
    #
    # Nombres tal cual los pide compute_sweep_sl_tp: SWEEP_SL_ATR_BUFFER
    # y SWEEP_RR_RATIO. Ojo, SWEEP_RR_RATIO NO se resolvía por el alias
    # de prefijo (habría buscado "RR_RATIO", que no existía) -- habría
    # sido el siguiente crash.
    SWEEP_SL_ATR_BUFFER = _float("SWEEP_SL_ATR_BUFFER", _float("SL_ATR_BUFFER", 0.3))
    SWEEP_RR_RATIO = _float("SWEEP_RR_RATIO", _float("TP_RR", 2.0))
    SL_ATR_BUFFER = SWEEP_SL_ATR_BUFFER   # alias para summary()
    TP_RR = SWEEP_RR_RATIO                # alias para summary()

    # --- Riesgo / cuenta ---
    # OJO: QTY_PCT es porcentaje del equity en NOCIONAL, no riesgo. El
    # riesgo real de cada operación depende de dónde caiga el nivel
    # barrido y varía entre entradas.
    # SOLO QTY_PCT. NO se acepta RISK_PCT como alias: en otros bots de la
    # flota RISK_PCT es el % de equity ARRIESGADO en la distancia al stop,
    # mientras que aquí QTY_PCT es el % puesto en NOCIONAL. Son magnitudes
    # distintas y mezclarlas hizo que un RISK_PCT=0.5 se leyera como
    # "0.5% de nocional" -> posiciones de 0.33 USDT.
    QTY_PCT = _primera(["QTY_PCT"], 10.0, "float")
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
    # Sin alias, por lo mismo: MAX_CONCURRENT de otro bot vale 1 y aquí
    # dejaba el aforo en 1 en vez de 5.
    MAX_CONCURRENT_POSITIONS = _primera(["MAX_CONCURRENT_POSITIONS"], 5, "int")
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
            f"min_displacement={cls.MIN_DISPLACEMENT_ATR}x ATR | "
            f"min_penetration={cls.MIN_PENETRATION_ATR}x ATR\n"
            f"SL: nivel barrido ± {cls.SL_ATR_BUFFER}x ATR | TP: RR {cls.TP_RR}x"
        )


# --------------------------------------------------------------------- #
def parametros_que_pide(ruta_modulo: str = "sweep_engine.py"):
    """Lee el CÓDIGO de sweep_engine y saca todos los `params.X` que usa.

    Sirve para dejar de descubrir parámetros que faltan de uno en uno,
    con un crash por deploy. Se ejecuta al arrancar y reporta la lista
    completa: los que Config resuelve, los que caen a un default neutro
    y los que no existen.

    Devuelve (resueltos, con_default, ausentes).
    """
    import ast
    import os.path

    if not os.path.exists(ruta_modulo):
        return [], [], []

    try:
        arbol = ast.parse(open(ruta_modulo, encoding="utf-8").read())
    except Exception:
        return [], [], []

    nombres = set()
    for nodo in ast.walk(arbol):
        # params.X / cfg.X / config.X / Config.X
        if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name):
            if nodo.value.id in ("params", "cfg", "config", "Config"):
                if nodo.attr.isupper():
                    nombres.add(nodo.attr)

    resueltos, con_default, ausentes = [], [], []
    for nombre in sorted(nombres):
        antes = dict(DEFAULTS_APLICADOS)
        try:
            getattr(Config, nombre)
        except AttributeError:
            ausentes.append(nombre)
            continue
        if nombre in DEFAULTS_APLICADOS and nombre not in antes:
            con_default.append(nombre)
        else:
            resueltos.append(nombre)
    return resueltos, con_default, ausentes
