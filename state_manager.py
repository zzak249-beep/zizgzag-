"""
state_manager.py — mismo patrón que el bot wavelet: nada se persiste
en disco, las posiciones abiertas se reconstruyen siempre desde BingX.
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
    unit = timeframe[-1].lower()
    value = int(timeframe[:-1])
    mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(unit)
    if mult is None:
        raise ValueError(f"Timeframe no soportado: {timeframe}")
    return value * mult
