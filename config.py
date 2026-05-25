"""
Configuración Central — QF Machine × JP Fusion Bot v3.1
"""
import os

HTF = "15m"

SYMBOLS = [
    "BTC-USDT",
    "ETH-USDT",
]

SIGNAL_CFG = {
    "smo":      3,
    "atr_len":  10,
    "mom":      20,
    "rev":      8,
    "vol_len":  14,
    "w1":       0.40,
    "w2":       0.30,
    "w3":       0.30,
    "dlen":     40,
    "dthr":     0.50,
    "dpm":      2.5,
    "dpb":      20,
    "spl":      5,
    "exl":      12,
    "bpt":      0.18,
    "asl":      10,
    "arr":      1.40,
    "abr":      1.40,
    "tlb":      30,
    "tll":      5,
    "tlr":      3,
    "tlm":      0.15,
    "pll":      5,
    "plr":      3,
    "phl":      5,
    "phr":      3,
    "hlc":      2,
    "hhc":      2,
    "hlw":      40,
    "fvg_min":  0.3,
    "fvg_bars": 40,
    "ob_imp":   1.5,
    "ob_bars":  50,
    "cvd_len":  20,
    "cvd_div":  5,
    "sq_len":   20,
    "sq_bbm":   2.0,
    "sq_kcm":   1.5,

    # ── HUNT MODE ──────────────────────────────────────────
    # Score normalizado mínimo: 0.20 = 20/100 en el dashboard
    # Sube a 0.30 para ser más selectivo, baja a 0.15 para más señales
    "hunt_score_thr": float(os.getenv("HUNT_SCORE_THR", "0.20")),

    # Decay mínimo: 0.57 = 57% (tu umbral observado en el dashboard)
    "hunt_decay_thr": float(os.getenv("HUNT_DECAY_THR", "0.57")),
}

RISK_CFG = {
    # Sincroniza con tu capital real en BingX
    "initial_equity": float(os.getenv("INITIAL_EQUITY", "500.0")),

    "leverage": int(os.getenv("LEVERAGE", "5")),

    # Riesgo por operación según tier
    "risk_pct_suprema": 1.5,
    "risk_pct_fuel":    1.0,
    "risk_pct_std":     0.5,
    "risk_pct_max":     2.0,

    # R:R por tier
    "rr_suprema": 3.0,
    "rr_fuel":    2.5,
    "rr_std":     2.0,

    # ── Circuit Breakers ───────────────────────────────────
    # Pérdida diaria máxima (% equity) — 3% de 500 = 15 USDT
    "max_daily_loss_pct":    float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0")),

    # Drawdown máximo desde pico
    "max_drawdown_pct":      float(os.getenv("MAX_DRAWDOWN_PCT", "15.0")),

    # Pérdidas consecutivas antes de pausa
    "max_consecutive_losses": int(os.getenv("MAX_CONSEC_LOSSES", "6")),

    # ⚠️ CRÍTICO: Este es el límite real de TRADES EJECUTADOS por día
    # No confundir con "intentos del scanner" — solo cuenta órdenes reales
    "max_daily_trades": int(os.getenv("MAX_DAILY_TRADES", "20")),
}
