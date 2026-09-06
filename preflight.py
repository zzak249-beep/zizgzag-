"""
preflight.py — Comprobaciones que BLOQUEAN el modo LIVE.

═══════════════════════════════════════════════════════════════════════
POR QUÉ EXISTE
═══════════════════════════════════════════════════════════════════════
El historial de operaciones mostró tres cosas que la configuración
decía que eran imposibles:

  · MARGIN_MODE="ISOLATED" y TODAS las posiciones en Cruzado.
    set_margin_mode() fallaba y el except se lo tragaba en silencio.
  · MAX_CONCURRENT="1" y seis posiciones abiertas, diez de ellas
    abiertas en doce minutos.
  · LEVERAGE="10" y una operación ejecutada a 20x, la que más perdió.

En los tres casos el bot creía una cosa y el exchange hacía otra. Un
try/except que ignora el fallo no es tolerancia a errores: es operar a
ciegas con dinero real.

REGLA DE ESTE MÓDULO: si el estado REAL de la cuenta no coincide con
lo que dice la configuración, no se opera. No se avisa y se sigue —
se para. Un bot que no puede verificar sus propias condiciones de
riesgo no tiene derecho a mandar órdenes.
"""
from __future__ import annotations

import logging

import config

log = logging.getLogger("preflight")

# Claves que este bot entiende. Si aparecen otras en el entorno, el
# despliegue está mezclado: variables de OTRO bot sobre este código.
CLAVES_AJENAS = {
    "MARGIN_PER_TRADE_USDT", "MAX_MARGIN_PCT", "MIN_NOTIONAL_USDT",
    "MAX_NOTIONAL_PCT", "MIN_PENETRATION_ATR", "REQUIRE_ST_BULL",
    "RSI_LEN", "ST_MULT", "ER_SHORT", "ER_LONG", "MAX_ER_LONG",
    "STRETCH_ATR", "MIN_COMPRESSION_ATR", "MIN_EXPANSION_ATR",
    "MAX_STRETCH_AT_ENTRY",
}


async def verificar(api, tg) -> tuple[bool, list[str]]:
    """
    Devuelve (puede_operar, lista_de_problemas).

    Con puede_operar=False el bot DEBE caer a SIGNAL, no continuar.
    """
    problemas: list[str] = []
    avisos: list[str] = []

    # ── 1. Configuración mezclada ─────────────────────────────────────
    import os
    ajenas = sorted(k for k in CLAVES_AJENAS if os.getenv(k) is not None)
    if ajenas:
        problemas.append(
            "CONFIGURACIÓN MEZCLADA: el entorno tiene variables que este "
            "código NO lee (" + ", ".join(ajenas[:6])
            + (f" y {len(ajenas)-6} más" if len(ajenas) > 6 else "") + "). "
            "Eso significa que crees estar configurando algo que no existe, "
            "o que estas variables son de otro bot."
        )

    # ── 2. Apalancamiento coherente ───────────────────────────────────
    if config.LEVERAGE > 5:
        avisos.append(
            f"Apalancamiento {config.LEVERAGE}x. Con stops de "
            f"{config.SL_ATR} ATR, una mecha del tamaño del stop es "
            f"{config.LEVERAGE * 100 // 100}x más cara. Nada lo prohíbe, "
            f"pero no es lo que se midió."
        )

    if not config.is_live():
        return True, avisos   # en SIGNAL no hay nada que verificar

    # ── 3. MODO DE MARGEN REAL, no el que dice la config ──────────────
    try:
        posiciones = await api.open_positions()
    except Exception as exc:  # noqa: BLE001
        problemas.append(f"No se pueden leer las posiciones: {exc}. "
                         "Sin esa lectura no hay control de riesgo.")
        posiciones = []

    cruzadas = []
    for p in posiciones or []:
        modo = str(p.get("marginType") or p.get("marginMode") or "").upper()
        if modo and "CROSS" in modo:
            cruzadas.append(str(p.get("symbol", "?")))
    if cruzadas and config.MARGIN_MODE == "ISOLATED":
        problemas.append(
            f"MARGEN CRUZADO en {len(cruzadas)} posición(es) "
            f"({', '.join(cruzadas[:4])}) con MARGIN_MODE=ISOLATED. "
            "En cruzado toda la cuenta respalda cada posición: en una "
            "cascada la liquidación llega ANTES que el stop y no pierdes "
            "el riesgo previsto, pierdes el saldo."
        )

    # ── 4. Posiciones abiertas por encima del límite ──────────────────
    abiertas = len(posiciones or [])
    if abiertas > config.MAX_TOTAL_POSITIONS:
        problemas.append(
            f"{abiertas} posiciones abiertas y el límite es "
            f"{config.MAX_TOTAL_POSITIONS}. Alguien —este bot u otro— no "
            "está respetando el tope."
        )
    elif abiertas > config.MAX_CONCURRENT:
        avisos.append(
            f"{abiertas} posiciones en la cuenta y MAX_CONCURRENT="
            f"{config.MAX_CONCURRENT}. Si no son de otro bot, el límite "
            "no se está aplicando."
        )

    # ── 5. Apalancamiento real de las posiciones ──────────────────────
    for p in posiciones or []:
        try:
            lev = int(float(p.get("leverage", 0) or 0))
        except (TypeError, ValueError):
            continue
        if lev > config.LEVERAGE:
            problemas.append(
                f"{p.get('symbol')} está a {lev}x y LEVERAGE={config.LEVERAGE}. "
                "El apalancamiento no se está fijando de verdad."
            )
            break

    return (len(problemas) == 0), (problemas + avisos)


def formatear(problemas: list[str], bloqueado: bool) -> str:
    cab = ("🚫 <b>ARRANQUE BLOQUEADO — no se opera</b>" if bloqueado
           else "⚠️ <b>Avisos de arranque</b>")
    cuerpo = "\n\n".join(f"· {p}" for p in problemas)
    pie = ("\n\n<i>El bot sigue vivo en modo SIGNAL: escanea y avisa, pero no "
           "manda órdenes. Corrige lo de arriba y redepliega.</i>"
           if bloqueado else "")
    return f"{cab}\n\n{cuerpo}{pie}"
