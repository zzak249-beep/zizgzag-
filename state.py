"""
Estado en memoria del bot.
Railway reinicia el proceso si cae, así que guardamos
lo mínimo necesario para no duplicar posiciones.
"""
from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class Estado:
    posicion_abierta: bool = False
    simbolo_activo: Optional[str] = None
    lado_activo: Optional[str] = None
    precio_entrada: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    cantidad_usdt: float = 0.0
    ts_apertura: float = 0.0

    def abrir(self, simbolo: str, lado: str, orden: dict):
        self.posicion_abierta = True
        self.simbolo_activo   = simbolo
        self.lado_activo      = lado
        self.precio_entrada   = orden["precio_entrada"]
        self.stop_loss        = orden["stop_loss"]
        self.take_profit      = orden["take_profit"]
        self.cantidad_usdt    = orden["cantidad_usdt"]
        self.ts_apertura      = time.time()

    def limpiar(self):
        self.posicion_abierta = False
        self.simbolo_activo   = None
        self.lado_activo      = None
        self.precio_entrada   = 0.0
        self.stop_loss        = 0.0
        self.take_profit      = 0.0
        self.cantidad_usdt    = 0.0
        self.ts_apertura      = 0.0

    def resumen(self) -> dict:
        return {
            "posicion_abierta": self.posicion_abierta,
            "simbolo": self.simbolo_activo,
            "lado": self.lado_activo,
            "entrada": self.precio_entrada,
            "sl": self.stop_loss,
            "tp": self.take_profit,
            "cantidad_usdt": self.cantidad_usdt,
        }


# Singleton global
estado = Estado()
