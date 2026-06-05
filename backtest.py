"""
backtest.py — Motor de Backtest QF×JP v3.4
==========================================
Simula la estrategia sobre datos históricos de BingX.
Calcula: Win Rate, Profit Factor, Sharpe, Max Drawdown, Expectancy.
Permite analizar TODOS los símbolos y rankearlos por rentabilidad.
"""
from __future__ import annotations
import logging, time
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    symbol:    str
    direction: str
    level:     str
    score:     int
    entry:     float
    sl:        float
    tp1:       float
    tp0:       float       # partial TP
    atr:       float
    entry_bar: int
    exit_price: float  = 0.0
    exit_bar:  int    = 0
    result:    str    = ""   # "TP" | "SL" | "TP0_BE" | "OPEN"
    pnl_pct:   float  = 0.0
    bars_held: int    = 0


@dataclass
class BacktestResult:
    symbol:         str
    total_trades:   int   = 0
    wins:           int   = 0
    losses:         int   = 0
    win_rate:       float = 0.0
    profit_factor:  float = 0.0
    expectancy:     float = 0.0   # % por trade promedio
    total_pnl:      float = 0.0
    max_drawdown:   float = 0.0
    sharpe:         float = 0.0
    avg_bars:       float = 0.0
    best_trade:     float = 0.0
    worst_trade:    float = 0.0
    std_levels:     int   = 0
    fuel_levels:    int   = 0
    sup_levels:     int   = 0
    score_avg:      float = 0.0
    trades:         list  = field(default_factory=list)
    error:          str   = ""

    @property
    def is_profitable(self) -> bool:
        return self.profit_factor > 1.2 and self.win_rate > 0.45 and self.total_trades >= 5

    def summary_line(self) -> str:
        icon = "✅" if self.is_profitable else ("⚠️" if self.profit_factor > 1.0 else "❌")
        return (
            f"{icon} `{self.symbol:15}` "
            f"WR:`{self.win_rate*100:.0f}%` "
            f"PF:`{self.profit_factor:.2f}` "
            f"PnL:`{self.total_pnl:+.1f}%` "
            f"DD:`{self.max_drawdown:.1f}%` "
            f"T:`{self.total_trades}`"
        )


class BacktestEngine:
    """
    Corre el backtest de QF×JP v3.4 sobre datos históricos de BingX.

    Uso:
        engine  = BacktestEngine(cfg)
        result  = engine.run("BTC-USDT", candles=1500)
        results = engine.run_all(symbols, candles=1000)
    """

    def __init__(self, cfg, indicators_cls, scorer_cls):
        self.cfg  = cfg
        self.IND  = indicators_cls
        self.SCR  = scorer_cls

    # ─── FETCH HISTÓRICO ──────────────────────────────────────────────────────

    def _fetch(self, symbol: str, tf: str, limit: int) -> list:
        """Fetch klines de BingX (sin firma — datos públicos)."""
        try:
            r = requests.get(
                "https://open-api.bingx.com/openApi/swap/v2/quote/klines",
                params={"symbol": symbol, "interval": tf, "limit": min(limit, 1440)},
                timeout=20,
            )
            r.raise_for_status()
            j = r.json()
            return j.get("data", []) if j.get("code") == 0 else []
        except Exception as e:
            logger.warning(f"[BT] fetch {symbol}: {e}")
            return []

    def _parse(self, raw: list) -> pd.DataFrame:
        from client import parse_klines
        return parse_klines(raw)

    # ─── SIMULACIÓN DE TRADE ──────────────────────────────────────────────────

    def _simulate_trade(
        self,
        trade: TradeRecord,
        df: pd.DataFrame,
        start_bar: int,
        max_bars: int = 100,
    ) -> TradeRecord:
        """Simula la ejecución de un trade barra a barra."""
        atr_sl_mult = self.cfg.ATR_SL_MULT
        is_long = trade.direction == "LONG"

        for i in range(start_bar + 1, min(start_bar + max_bars, len(df))):
            row_hi = float(df["high"].iloc[i])
            row_lo = float(df["low"].iloc[i])
            row_cl = float(df["close"].iloc[i])

            if is_long:
                # SL tocado
                if row_lo <= trade.sl:
                    trade.exit_price = trade.sl
                    trade.result     = "SL"
                    trade.pnl_pct    = (trade.sl - trade.entry) / trade.entry * 100
                    break
                # Partial TP
                if not trade.__dict__.get("_ptp_done") and row_hi >= trade.tp0:
                    trade.__dict__["_ptp_done"] = True
                    # Mover SL a breakeven
                    trade.sl = trade.entry
                # TP1
                if row_hi >= trade.tp1:
                    trade.exit_price = trade.tp1
                    trade.result     = "TP"
                    trade.pnl_pct    = (trade.tp1 - trade.entry) / trade.entry * 100
                    break
            else:
                # SL corto
                if row_hi >= trade.sl:
                    trade.exit_price = trade.sl
                    trade.result     = "SL"
                    trade.pnl_pct    = (trade.entry - trade.sl) / trade.entry * 100
                    break
                # Partial TP
                if not trade.__dict__.get("_ptp_done") and row_lo <= trade.tp0:
                    trade.__dict__["_ptp_done"] = True
                    trade.sl = trade.entry
                # TP1
                if row_lo <= trade.tp1:
                    trade.exit_price = trade.tp1
                    trade.result     = "TP"
                    trade.pnl_pct    = (trade.entry - trade.tp1) / trade.entry * 100
                    break
        else:
            # Tiempo máximo agotado — cerrar al precio actual
            trade.exit_price = float(df["close"].iloc[min(start_bar + max_bars - 1, len(df)-1)])
            trade.result     = "TIMEOUT"
            if is_long:
                trade.pnl_pct = (trade.exit_price - trade.entry) / trade.entry * 100
            else:
                trade.pnl_pct = (trade.entry - trade.exit_price) / trade.entry * 100

        trade.exit_bar  = i if trade.result else start_bar
        trade.bars_held = trade.exit_bar - start_bar
        return trade

    # ─── BACKTEST SINGLE SYMBOL ───────────────────────────────────────────────

    def run(
        self,
        symbol:  str,
        candles: int = 1000,
        tf:      str = "3m",
        min_score: int = None,
    ) -> BacktestResult:
        """
        Corre el backtest de QF×JP v3.4 sobre `candles` velas de `symbol`.
        """
        result = BacktestResult(symbol=symbol)
        min_score = min_score or self.cfg.SCORE_STD

        raw = self._fetch(symbol, tf, candles)
        if len(raw) < 100:
            result.error = f"Solo {len(raw)} velas — insuficiente"
            return result

        df = self._parse(raw)
        if df is None or len(df) < 100:
            result.error = "DataFrame vacío"
            return result

        # También fetch HTF para indicators
        raw15 = self._fetch(symbol, "15m", 100)
        raw1h = self._fetch(symbol, "1h",  50)
        df15  = self._parse(raw15) if raw15 else None
        df1h  = self._parse(raw1h) if raw1h else None

        calc   = self.IND(self.cfg)
        scorer = self.SCR(self.cfg)

        trades    = []
        in_trade  = False
        warmup    = 100   # barras de warmup para indicadores

        for bar in range(warmup, len(df) - 1):
            # Slice hasta esta barra (simular tiempo real)
            df_slice = df.iloc[:bar+1]
            df15_sl  = df15.iloc[:min(bar//5+1, len(df15))] if df15 is not None else None
            df1h_sl  = df1h.iloc[:min(bar//20+1, len(df1h))] if df1h is not None else None

            # Si hay trade abierto, no abrir otro (simplificado: 1 trade a la vez)
            if in_trade:
                continue

            try:
                ind = calc.compute(df_slice, df15_sl, df1h_sl, None, None)
                sig = scorer.score(symbol, ind, balance=1000)
            except Exception:
                continue

            if not sig.is_valid or sig.score < min_score:
                continue

            # Abrir trade en la siguiente barra (bar+1)
            entry_bar = bar + 1
            if entry_bar >= len(df):
                break

            trade = TradeRecord(
                symbol    = symbol,
                direction = sig.direction,
                level     = sig.level,
                score     = sig.score,
                entry     = float(df["open"].iloc[entry_bar]),
                sl        = sig.sl_price,
                tp1       = sig.tp1_price,
                tp0       = sig.tp0_price,
                atr       = sig.atr,
                entry_bar = entry_bar,
            )

            trade = self._simulate_trade(trade, df, entry_bar, max_bars=60)
            trades.append(trade)
            in_trade = False  # reset para siguiente señal

        if not trades:
            result.error = "0 trades generados"
            return result

        # ─── MÉTRICAS ────────────────────────────────────────────────────────
        pnls    = [t.pnl_pct for t in trades]
        wins    = [p for p in pnls if p > 0]
        losses  = [p for p in pnls if p <= 0]

        result.total_trades  = len(trades)
        result.wins          = len(wins)
        result.losses        = len(losses)
        result.win_rate      = len(wins) / len(trades) if trades else 0
        result.total_pnl     = sum(pnls)
        result.expectancy    = np.mean(pnls) if pnls else 0
        result.best_trade    = max(pnls) if pnls else 0
        result.worst_trade   = min(pnls) if pnls else 0
        result.avg_bars      = np.mean([t.bars_held for t in trades])
        result.score_avg     = np.mean([t.score for t in trades])
        result.std_levels    = sum(1 for t in trades if t.level == "STD")
        result.fuel_levels   = sum(1 for t in trades if t.level == "FUEL")
        result.sup_levels    = sum(1 for t in trades if t.level == "SUP")

        # Profit Factor
        gross_wins  = sum(wins)   if wins   else 0
        gross_loss  = abs(sum(losses)) if losses else 0.001
        result.profit_factor = gross_wins / gross_loss

        # Max Drawdown
        equity = np.cumsum([0] + pnls)
        running_max = np.maximum.accumulate(equity)
        dd = running_max - equity
        result.max_drawdown = float(np.max(dd)) if len(dd) > 1 else 0

        # Sharpe (simplificado, sin tasa libre de riesgo)
        if len(pnls) > 1:
            result.sharpe = float(np.mean(pnls) / np.std(pnls) * np.sqrt(252)) if np.std(pnls) > 0 else 0

        result.trades = trades
        return result

    # ─── BACKTEST MULTI-SYMBOL ────────────────────────────────────────────────

    def run_all(
        self,
        symbols:   list[str],
        candles:   int = 800,
        tf:        str = "3m",
        delay:     float = 0.5,   # delay entre fetches para no saturar API
        on_progress = None,       # callback(symbol, i, total)
    ) -> list[BacktestResult]:
        """
        Corre el backtest en todos los símbolos y devuelve resultados ordenados.
        """
        results = []
        total   = len(symbols)

        logger.info(f"[BT] Iniciando backtest de {total} símbolos ({candles} velas cada uno)")

        for i, symbol in enumerate(symbols):
            logger.info(f"[BT] {i+1}/{total} — {symbol}")
            if on_progress:
                on_progress(symbol, i+1, total)

            r = self.run(symbol, candles, tf)
            results.append(r)
            time.sleep(delay)

        # Ordenar: primero rentables, luego por profit factor
        results.sort(key=lambda r: (
            -int(r.is_profitable),
            -r.profit_factor,
        ))

        return results

    # ─── REPORT TELEGRAM ─────────────────────────────────────────────────────

    @staticmethod
    def format_telegram_report(results: list[BacktestResult], top_n: int = 20) -> list[str]:
        """
        Genera mensajes Telegram con el ranking de símbolos.
        Devuelve lista de strings (un mensaje por cada 10 símbolos).
        """
        profitable = [r for r in results if r.is_profitable]
        marginal   = [r for r in results if r.profit_factor > 1.0 and not r.is_profitable]
        losing     = [r for r in results if r.profit_factor <= 1.0 and r.total_trades >= 5]

        msgs = []

        # Header
        header = (
            f"📊 *BACKTEST QF×JP v3.4 — {len(results)} símbolos*\n"
            f"✅ Rentables: `{len(profitable)}`  "
            f"⚠️ Marginal: `{len(marginal)}`  "
            f"❌ Perdedores: `{len(losing)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        )

        # Top símbolos rentables
        if profitable:
            lines = [header, "🏆 *TOP RENTABLES:*\n"]
            for r in profitable[:top_n]:
                lines.append(r.summary_line())
            msgs.append("\n".join(lines))

        # Detalles del top 3
        for r in profitable[:3]:
            detail = (
                f"⭐ *{r.symbol} — Detalle*\n"
                f"Win Rate:  `{r.win_rate*100:.1f}%`\n"
                f"Prof. Factor: `{r.profit_factor:.2f}`\n"
                f"Expectancy: `{r.expectancy:+.2f}%`/trade\n"
                f"Max DD:    `{r.max_drawdown:.1f}%`\n"
                f"Sharpe:    `{r.sharpe:.2f}`\n"
                f"Trades:    `{r.total_trades}` "
                f"(STD:{r.std_levels} FUEL:{r.fuel_levels} SUP:{r.sup_levels})\n"
                f"Score avg: `{r.score_avg:.0f}`\n"
                f"Best: `{r.best_trade:+.2f}%` | Worst: `{r.worst_trade:+.2f}%`"
            )
            msgs.append(detail)

        # Símbolos a evitar
        if losing:
            lines = ["❌ *EVITAR (pérdida consistente):*\n"]
            for r in losing[:10]:
                lines.append(r.summary_line())
            msgs.append("\n".join(lines))

        return msgs
