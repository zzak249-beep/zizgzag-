"""
state_manager.py — Estado del Sweep Reversal Map Bot.

OJO: este archivo NO es el state_manager.py del bot joyful-art. Aquel
importa `config` como módulo y persiste en JSON usando config.STATE_FILE;
este es una dataclass en memoria y el config del sweep es una CLASE
(Config), sin STATE_FILE de módulo. Copiar uno sobre otro revienta el
arranque con AttributeError.

Nada se persiste en disco: las posiciones abiertas se reconstruyen
siempre desde BingX (main.py las reconcilia en cada ciclo), así que lo
único que se pierde en un redeploy es el cooldown de señales y el
registro de qué símbolos ya tienen el apalancamiento fijado. Ambas cosas
se rehacen solas en pocos minutos.
"""
from dataclasses import dataclass, field


@dataclass
class StateManager:
    last_signal_time: dict = field(default_factory=dict)
    leverage_set: set = field(default_factory=set)
    known_positions: dict = field(default_factory=dict)

    def can_signal(self, symbol: str, candle_time_ms: int, cooldown_bars: int, timeframe_ms: int) -> bool:
        last = self.last_signal_time.get(symbol)
        if last is None:
            return True
        return (candle_time_ms - last) / timeframe_ms >= cooldown_bars

    def mark_signal(self, symbol: str, candle_time_ms: int) -> None:
        self.last_signal_time[symbol] = candle_time_ms

    def leverage_already_set(self, symbol: str) -> bool:
        return symbol in self.leverage_set

    def mark_leverage_set(self, symbol: str) -> None:
        self.leverage_set.add(symbol)


def timeframe_to_ms(timeframe: str) -> int:
    """Se mantiene por compatibilidad con quien la importe de aquí.
    main.py ya no depende de ella: tiene su propia copia, para que el bot
    no deje de arrancar según qué versión de este módulo haya en el repo.
    """
    unit = timeframe[-1].lower()
    value = int(timeframe[:-1])
    mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(unit)
    if mult is None:
        raise ValueError(f"Timeframe no soportado: {timeframe}")
    return value * mult
