"""
bot/markov.py
Motor de cadenas de Markov con ventana deslizante.

Replica exactamente la lógica del Pine Script [V48.7]:
  - 3 estados: 0=BULL, 1=BEAR, 2=NEUTRAL
  - Matriz de transición 3×3 (9 celdas)
  - Ventana de memoria configurable (lookback_markov velas)
  - Calcula prob_bull y prob_bear desde el estado actual
"""
import numpy as np
import pandas as pd
from collections import deque


class MarkovEngine:
    """
    Procesa la serie de slopes y devuelve probabilidades de transición.
    """

    BULL    = 0
    BEAR    = 1
    NEUTRAL = 2

    def __init__(self, lookback: int = 200):
        self.lookback = lookback
        # 9 celdas: matrix[from_state * 3 + to_state]
        self._matrix  = np.zeros(9, dtype=float)
        # Cola FIFO para mantener la ventana: (prev_state, curr_state)
        self._window: deque = deque()

    def _classify(self, slope: float, threshold: float) -> int:
        if slope > threshold:
            return self.BULL
        if slope < -threshold:
            return self.BEAR
        return self.NEUTRAL

    def _add(self, prev_s: int, curr_s: int) -> None:
        idx = prev_s * 3 + curr_s
        self._matrix[idx] += 1.0
        self._window.append((prev_s, curr_s))

    def _remove_oldest(self) -> None:
        if self._window:
            prev_s, curr_s = self._window.popleft()
            idx = prev_s * 3 + curr_s
            self._matrix[idx] = max(0.0, self._matrix[idx] - 1.0)

    def update(self, slope: float, prev_slope: float,
               threshold: float) -> tuple[float, float]:
        """
        Actualiza la matriz con el nuevo par (prev_slope, slope).
        Devuelve (prob_bull, prob_bear) dado el estado actual.
        """
        curr_s = self._classify(slope, threshold)
        prev_s = self._classify(prev_slope, threshold)

        self._add(prev_s, curr_s)
        if len(self._window) > self.lookback:
            self._remove_oldest()

        return self._get_probs(curr_s)

    def _get_probs(self, curr_s: int) -> tuple[float, float]:
        base      = curr_s * 3
        total_obs = self._matrix[base] + self._matrix[base + 1] + self._matrix[base + 2]
        if total_obs == 0:
            return 0.0, 0.0
        prob_bull = (self._matrix[base + self.BULL]  / total_obs) * 100
        prob_bear = (self._matrix[base + self.BEAR]  / total_obs) * 100
        return round(prob_bull, 2), round(prob_bear, 2)

    def reset(self) -> None:
        self._matrix[:] = 0
        self._window.clear()


# ──────────────────────────────────────────────
# Función vectorizada para backtesting / warmup
# ──────────────────────────────────────────────

def compute_markov_probs(slopes: pd.Series, thresholds: pd.Series,
                         lookback: int = 200) -> pd.DataFrame:
    """
    Procesa toda la serie histórica y devuelve un DataFrame con
    columnas ['prob_bull', 'prob_bear'] por cada barra.
    Útil para el warmup inicial antes de entrar en el loop en vivo.
    """
    engine = MarkovEngine(lookback)
    prob_bulls, prob_bears = [], []

    for i in range(len(slopes)):
        if i == 0:
            prob_bulls.append(0.0)
            prob_bears.append(0.0)
            continue
        pb, pr = engine.update(
            float(slopes.iloc[i]),
            float(slopes.iloc[i - 1]),
            float(thresholds.iloc[i])
        )
        prob_bulls.append(pb)
        prob_bears.append(pr)

    return pd.DataFrame({
        "prob_bull": prob_bulls,
        "prob_bear": prob_bears
    }, index=slopes.index)
