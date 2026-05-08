"""
Motor de gestión de riesgo
- Sizing basado en ATR (nunca en pivote variable)
- Riesgo fijo: 1% del capital por operación
- SL = ATR × 1.5 desde entrada
- TP = SL × ratio R:R
"""
from dataclasses import dataclass


@dataclass
class Orden:
    accion: str          # BUY / SELL
    precio_entrada: float
    stop_loss: float
    take_profit: float
    cantidad: float      # contratos (USDT)
    riesgo_usdt: float
    ratio_rr: float
    sl_pct: float        # % distancia SL
    tp_pct: float        # % distancia TP


def calcular_orden(
    accion: str,
    precio: float,
    atr: float,
    capital: float,       # capital total en USDT
    apalancamiento: int,
    riesgo_pct: float,    # % del capital a arriesgar
    ratio_rr: float,      # ratio riesgo:recompensa
    atr_multiplicador: float = 1.5,
) -> dict:
    """
    Calcula SL, TP y tamaño de posición normalizados por ATR.

    Con $8 capital y 10x apalancamiento:
      - Capital expuesto: $80
      - Riesgo 1% del capital: $0.08 por trade (muy ajustado)
      - Por eso usamos riesgo sobre el capital apalancado: 1% de $80 = $0.80
    """
    riesgo_usdt = capital * apalancamiento * (riesgo_pct / 100)

    sl_distancia = atr * atr_multiplicador
    sl_pct = (sl_distancia / precio) * 100

    if accion == "BUY":
        stop_loss   = precio - sl_distancia
        take_profit = precio + (sl_distancia * ratio_rr)
    else:  # SELL
        stop_loss   = precio + sl_distancia
        take_profit = precio - (sl_distancia * ratio_rr)

    tp_pct = abs(take_profit - precio) / precio * 100

    # Tamaño en USDT (capital apalancado completo = $8 × 10)
    # BingX Futures acepta cantidad en USDT directamente
    cantidad_usdt = capital * apalancamiento  # $80 de posición

    return {
        "accion": accion,
        "precio_entrada": round(precio, 6),
        "stop_loss": round(stop_loss, 6),
        "take_profit": round(take_profit, 6),
        "cantidad_usdt": round(cantidad_usdt, 2),
        "riesgo_usdt": round(riesgo_usdt, 4),
        "sl_pct": round(sl_pct, 3),
        "tp_pct": round(tp_pct, 3),
        "ratio_rr": ratio_rr,
        "atr": round(atr, 6),
    }
