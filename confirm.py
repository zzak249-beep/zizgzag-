"""
confirm.py — filtro de régimen para la flota. Modo REGISTRO por defecto.

═══════════════════════════════════════════════════════════════════════
QUÉ HACE
═══════════════════════════════════════════════════════════════════════
Calcula el ratio de varianzas de Lo-MacKinlay con el estadístico ROBUSTO
a heterocedasticidad y con pesos exponenciales, y dice si el símbolo está
en régimen tendencial (terreno del wavelet, que es un motor de ruptura),
reversivo (su peor terreno) o indeterminado.

    VR(q) = Var(retorno de q velas) / (q · Var(retorno de 1 vela))
    VR > 1 -> los movimientos se acumulan  -> tendencial
    VR < 1 -> el precio se come sus movimientos -> reversivo

═══════════════════════════════════════════════════════════════════════
POR QUÉ EL ROBUSTO Y NO EL CLÁSICO
═══════════════════════════════════════════════════════════════════════
La varianza asintótica clásica 2(2q-1)(q-1)/(3qn) SUPONE volatilidad
constante. Simulando paseos aleatorios con volatilidad GARCH, donde por
construcción NO hay régimen, el z clásico declaraba régimen el 9,3% de las
veces cuando debería ser el 5%. El robusto queda en 5,2%.

    delta(j) = Σ(r-μ)²(r_{-j}-μ)² / [Σ(r-μ)²]²
    phi2(q)  = Σ_{j=1}^{q-1} [2(q-j)/q]² · delta(j)
    z*       = (VR-1) / √phi2

OJO: circula una versión de delta(j) multiplicada por n. Es INCORRECTA.
El control es que bajo iid phi2 debe coincidir con phi1: sin la n da 1.01,
con ella se va x242 y el test no rechaza jamás.

═══════════════════════════════════════════════════════════════════════
EL LÍMITE QUE DECIDE CÓMO SE USA
═══════════════════════════════════════════════════════════════════════
Medido contra regímenes conocidos de 400 barras:

    método                     se moja  acierta  retraso
    VR plana W=240              12,2%    87,8%     240
    VR plana W=120               9,0%    91,2%     145
    VR ponderada lambda=0,985    8,7%    93,4%     132
    VR ponderada lambda=0,975    6,2%    93,6%     128

NINGÚN método bajó de ~128 velas. Con regímenes de 400, eso es un TERCIO
del movimiento. El retraso es estructural: confirmar un régimen exige
verlo, y verlo lleva tiempo.

CONSECUENCIA PRÁCTICA, y es la razón de ser de este módulo:
    NO sirve como disparador de entrada. Si esperas la confirmación, te
    pierdes el primer tercio de cada régimen.
    SÍ sirve como VETO: no operar ruptura cuando el símbolo lleva rato
    demostrando que es reversivo. Vetar tarde sigue valiendo; entrar
    tarde, no.

Y todo esto sale de UNA simulación con una duración de régimen concreta,
no de tus datos. La dirección (el retraso existe y es grande) es mecánica
y sí es general; los números exactos no.

═══════════════════════════════════════════════════════════════════════
POTENCIA: ESPERA SILENCIO LA MAYOR PARTE DEL TIEMPO
═══════════════════════════════════════════════════════════════════════
Los pesos exponenciales recortan la muestra: con lambda=0,985 sobre 240
velas la muestra EFECTIVA es de 125. Menos muestra, menos potencia.
Medido sobre series de 400 velas:

    umbral z*   falsos+ (iid)   detecta phi=+0,15   detecta phi=-0,15
        1,5         10,4%             30,0%               28,4%
        2,0          3,6%             19,2%                5,2%
        2,5          1,2%             10,0%                0,4%

Con umbral 2,0 el módulo dirá "indeterminado" la mayor parte del tiempo, y
apenas detectará el lado reversivo (5,2%). Eso NO es un fallo: es lo que
da la muestra.

POR ESO EL DEFECTO ES 1,5 Y NO 2,0. Como el módulo solo puede VETAR, un
falso positivo cuesta una entrada que no se toma; un falso negativo deja
pasar una entrada en mal terreno. Los dos errores no cuestan lo mismo, así
que el umbral no tiene por qué ser el de un contraste académico. Si
prefieres ser estricto, sube CONFIRM_Z a 2,0 sabiendo que casi nunca
vetará.

═══════════════════════════════════════════════════════════════════════
ESTADO
═══════════════════════════════════════════════════════════════════════
CONFIRM_BLOQUEAR viene en False. El módulo calcula, devuelve y se apunta
en el diario, pero NO impide ninguna entrada. Dos semanas de registro y
después miras si tus perdedoras se concentran en régimen reversivo. Si no
se concentran, el filtro no vale para tu símbolo y se apaga.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from statistics import fmean
from typing import Any, Iterable, Sequence

log = logging.getLogger("confirm")

DEFAULTS = {
    "CONFIRM_ENABLED": True,
    "CONFIRM_BLOQUEAR": False,   # arranca en REGISTRO a propósito
    "CONFIRM_Q": 8,
    "CONFIRM_WIN": 240,
    "CONFIRM_LAMBDA": 0.985,     # 1.0 = ventana plana
    "CONFIRM_Z": 1.5,
    "CONFIRM_MIN_VELAS": 120,
}


def cfg(config: Any, key: str):
    return getattr(config, key, DEFAULTS[key])


@dataclass
class Regimen:
    ok: bool = False
    motivo: str = "sin datos"
    vr: float = 0.0
    z: float = 0.0
    etiqueta: str = "indeterminado"   # tendencial | reversivo | indeterminado
    n: int = 0

    @property
    def tendencial(self) -> bool:
        return self.ok and self.etiqueta == "tendencial"

    @property
    def reversivo(self) -> bool:
        return self.ok and self.etiqueta == "reversivo"


def _retornos(cierres: Sequence[float]) -> list[float]:
    out = []
    for i in range(1, len(cierres)):
        a, b = cierres[i - 1], cierres[i]
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def _pesos(n: int, lam: float) -> list[float]:
    """Pesos exponenciales normalizados; el último dato es el más pesado."""
    if lam >= 1.0:
        return [1.0 / n] * n
    w = [lam ** (n - 1 - i) for i in range(n)]
    s = sum(w)
    return [x / s for x in w]


def calcular(config: Any, cierres: Iterable[float]) -> Regimen:
    """Nunca lanza. Si algo falla devuelve ok=False y el motivo."""
    r = Regimen()
    try:
        if not cfg(config, "CONFIRM_ENABLED"):
            r.motivo = "desactivado"
            return r

        c = [float(x) for x in cierres if x and float(x) > 0]
        win = int(cfg(config, "CONFIRM_WIN"))
        c = c[-(win + 1):]
        rets = _retornos(c)
        r.n = len(rets)
        if r.n < int(cfg(config, "CONFIRM_MIN_VELAS")):
            r.motivo = f"pocas velas ({r.n})"
            return r

        q = int(cfg(config, "CONFIRM_Q"))
        if r.n <= q * 2:
            r.motivo = "ventana menor que q"
            return r

        lam = float(cfg(config, "CONFIRM_LAMBDA"))
        n = r.n
        w = _pesos(n, lam)
        n_eff = 1.0 / sum(x * x for x in w)

        mu = sum(w[i] * rets[i] for i in range(n))
        dev = [rets[i] - mu for i in range(n)]
        d2 = [x * x for x in dev]
        v1 = sum(w[i] * d2[i] for i in range(n))
        if v1 <= 1e-18:
            r.motivo = "varianza nula"
            return r

        # Retornos agregados de q velas, con sus propios pesos.
        agg = [sum(rets[i - q + 1:i + 1]) for i in range(q - 1, n)]
        wq_raw = w[q - 1:]
        sq = sum(wq_raw)
        if sq <= 0 or len(agg) < 5:
            r.motivo = "muestra agregada insuficiente"
            return r
        wq = [x / sq for x in wq_raw]
        muq = sum(wq[i] * agg[i] for i in range(len(agg)))
        vq = sum(wq[i] * (agg[i] - muq) ** 2 for i in range(len(agg)))

        vr = vq / (q * v1)
        r.vr = vr

        # phi2 robusto, versión ponderada. md es la media ponderada de d2;
        # n_eff sustituye a n porque los pesos reducen la muestra efectiva.
        md = v1
        phi2 = 0.0
        for j in range(1, q):
            wj_raw = w[j:]
            sj = sum(wj_raw)
            if sj <= 0:
                continue
            num = sum((wj_raw[k] / sj) * d2[j + k] * d2[k] for k in range(n - j))
            phi2 += (2.0 * (q - j) / q) ** 2 * (num / (n_eff * md * md))
        if phi2 <= 1e-30:
            r.motivo = "phi2 no positiva"
            return r

        z = (vr - 1.0) / math.sqrt(phi2)
        zthr = float(cfg(config, "CONFIRM_Z"))
        r.z = z
        r.ok = True
        r.motivo = "ok"
        r.etiqueta = "tendencial" if z >= zthr else "reversivo" if z <= -zthr else "indeterminado"
        return r
    except Exception as exc:  # noqa: BLE001
        r.ok = False
        r.motivo = f"error: {type(exc).__name__}"
        log.debug("confirm.calcular falló", exc_info=True)
        return r


def veta(config: Any, reg: Regimen, lado: str) -> tuple[bool, str]:
    """
    ¿Hay que VETAR esta entrada? Nunca abre nada: solo puede impedir.

    Se veta la ruptura (que es lo que hace el wavelet) cuando el símbolo
    está en régimen REVERSIVO. No se veta por "indeterminado": el filtro
    ya llega tarde de por sí, y exigirle además que se moje lo dejaría
    mudo casi siempre.

    Con CONFIRM_BLOQUEAR=False devuelve siempre (False, "") y solo registra.
    """
    if not cfg(config, "CONFIRM_BLOQUEAR"):
        return False, ""
    if not reg.ok or not reg.reversivo:
        return False, ""
    return True, f"régimen reversivo (z* {reg.z:.2f}): terreno malo para ruptura"


def columnas(reg: Regimen) -> dict:
    """Campos para el diario. Se apuntan siempre, se bloquee o no."""
    return {
        "conf_ok": int(reg.ok),
        "conf_z": round(reg.z, 3) if reg.ok else None,
        "conf_vr": round(reg.vr, 4) if reg.ok else None,
        "conf_regimen": reg.etiqueta if reg.ok else reg.motivo,
        "conf_n": reg.n,
    }


def texto(reg: Regimen) -> str:
    """Una línea para acompañar la señal, al estilo de funding.sesgo()."""
    if not reg.ok:
        return f"régimen: {reg.motivo}"
    marca = "✅" if reg.tendencial else "⚠️" if reg.reversivo else "·"
    return f"{marca} {reg.etiqueta} (z* {reg.z:+.2f}, VR {reg.vr:.3f})"
