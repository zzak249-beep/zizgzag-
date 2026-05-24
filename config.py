"""
Configuración Central — QF Machine × JP Fusion Bot v3.2
CAMBIOS v3.2:
  - scan_concurrency reducido a 8 (evita error 100410 de BingX)
  - scan_interval_s aumentado a 300s (5 min — igual al TF pero seguro)
  - CVD es ahora filtro OBLIGATORIO en signals.py, no solo puntuación
  - hunt_decay_thr subido a 0.50 (más selectivo = menos ruido)
"""

# ── Temporalidades ────────────────────────────────────────────
HTF = "15m"

# ── Símbolos fijos ────────────────────────────────────────────
SYMBOLS = [
    "BTC-USDT",
    "ETH-USDT",
]

# ── Señal: parámetros del indicador ──────────────────────────
SIGNAL_CFG = {
    # L1 / General
    "smo":      3,
    "atr_len":  10,

    # L2 Factores
    "mom":      20,
    "rev":      8,
    "vol_len":  14,
    "w1":       0.40,
    "w2":       0.30,
    "w3":       0.30,

    # L3 Decaimiento
    "dlen":     40,
    "dthr":     0.50,   # Umbral vida media (VIVA si ≥50%)

    # L4 Dark Pool
    "dpm":      2.5,
    "dpb":      20,
    "spl":      5,

    # L5 Ejecución
    "exl":      12,
    "bpt":      0.18,

    # L6 Asimetría
    "asl":      10,
    "arr":      1.40,
    "abr":      1.40,

    # L7 Trendline
    "tlb":      30,
    "tll":      5,
    "tlr":      3,
    "tlm":      0.15,

    # L8 Swing Analysis
    "pll":      5,
    "plr":      3,
    "phl":      5,
    "phr":      3,
    "hlc":      2,
    "hhc":      2,
    "hlw":      40,

    # L9 FVG
    "fvg_min":  0.3,
    "fvg_bars": 40,

    # L10 Order Blocks
    "ob_imp":   1.5,
    "ob_bars":  50,

    # L11 CVD Delta — FILTRO OBLIGATORIO v3.2
    # cvd_rising:   CVD > EMA → presión compradora  → ✅ LONG
    # cvd_bull_div: precio↓ pero CVD↑ → ACUM oculta → ✅ LONG fuerte
    # cvd_bear_div: precio↑ pero CVD↓ → DISTRIBUCIÓN → ❌ NUNCA LONG
    "cvd_len":  20,
    "cvd_div":  5,

    # L12 Squeeze
    "sq_len":   20,
    "sq_bbm":   2.0,
    "sq_kcm":   1.5,
}

# ── Riesgo ────────────────────────────────────────────────────
RISK_CFG = {
    "initial_equity": 500.0,
    "leverage": 5,

    "risk_pct_suprema": 1.5,
    "risk_pct_fuel":    1.0,
    "risk_pct_std":     0.5,
    "risk_pct_max":     2.0,

    "rr_suprema": 3.0,
    "rr_fuel":    2.5,
    "rr_std":     2.0,

    "max_daily_loss_pct":      3.0,
    "max_drawdown_pct":       15.0,
    "max_consecutive_losses":   4,
    "max_daily_trades":        10,
}

# ── HUNT MODE ────────────────────────────────────────────────
# v3.2: hunt_decay_thr subido a 0.50 (igual que dthr) — más selectivo
# El CVD también se exige en HUNT (ver signals.py)
HUNT_CFG = {
    "hunt_score_thr":      0.08,   # ~8/100
    "hunt_decay_thr":      0.50,   # 50% — igual al umbral VIVA/MUERTA
    "hunt_min_conviction": 1,
}

SIGNAL_CFG["hunt_score_thr"] = HUNT_CFG["hunt_score_thr"]
SIGNAL_CFG["hunt_decay_thr"] = HUNT_CFG["hunt_decay_thr"]

# ── Scanner de mercado ────────────────────────────────────────
# IMPORTANTE v3.2:
#   scan_concurrency=8  → BingX tiene límite ~20 req/s en klines;
#                          con 8 pares en paralelo + throttle en exchange.py
#                          evitamos el error 100410 ("disabled period")
#   scan_interval_s=300 → 5 minutos entre scans completos (3 velas de 3m)
#                          da tiempo a confirmar señal antes del siguiente scan
#
# Variables de entorno para Railway:
# MIN_VOLUME=200000
# SCAN_CONCURRENCY=8          ← REDUCIDO de 20 a 8
# SCAN_INTERVAL_S=300         ← AUMENTADO de 180 a 300
# SCAN_MAX_NOTIFY=3
# SUMMARY_EVERY=10
# MAX_OPEN_POSITIONS=3
# MIN_TIER_TO_TRADE=HUNT_LONG
# SCAN_COOLDOWN_MIN=15
# HUNT_MIN_CONVICTION=1
