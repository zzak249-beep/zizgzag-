"""
Analytics & Trade Journal — Sniper Bot V50.6
Persists all trades to JSON, computes running stats,
generates a daily performance report.
"""
import json
import logging
import os
from datetime import datetime, date
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class TradeJournal:
    """
    Lightweight JSON-backed trade log.
    Each entry: {id, symbol, direction, entry, exit, tp, sl,
                 pnl_usdt, win, open_ts, close_ts, reason, indicators}
    """

    def __init__(self, filepath: str = "logs/trades.json"):
        self.filepath = filepath
        self._trades: List[Dict] = []
        self._load()

    def _load(self):
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath) as f:
                    self._trades = json.load(f)
                logger.info(f"Loaded {len(self._trades)} historical trades")
            except Exception as exc:
                logger.warning(f"Could not load trade journal: {exc}")
                self._trades = []

    def _save(self):
        with open(self.filepath, "w") as f:
            json.dump(self._trades, f, indent=2, default=str)

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def open_trade(self, trade_id: str, symbol: str, direction: str,
                   entry: float, tp: float, sl: float,
                   size_pct: float, indicators: Dict) -> Dict:
        record = {
            "id":         trade_id,
            "symbol":     symbol,
            "direction":  direction,
            "entry":      entry,
            "tp":         tp,
            "sl":         sl,
            "size_pct":   size_pct,
            "pnl_usdt":   None,
            "win":        None,
            "open_ts":    datetime.utcnow().isoformat(),
            "close_ts":   None,
            "exit":       None,
            "reason":     "",
            "indicators": indicators,
        }
        self._trades.append(record)
        self._save()
        return record

    def close_trade(self, trade_id: str, exit_price: float,
                    pnl_usdt: float, reason: str) -> Optional[Dict]:
        for t in self._trades:
            if t["id"] == trade_id and t["close_ts"] is None:
                t["exit"]     = exit_price
                t["pnl_usdt"] = round(pnl_usdt, 4)
                t["win"]      = pnl_usdt > 0
                t["close_ts"] = datetime.utcnow().isoformat()
                t["reason"]   = reason
                self._save()
                logger.info(f"Trade closed: {trade_id} pnl={pnl_usdt:.2f}")
                return t
        logger.warning(f"Trade not found for close: {trade_id}")
        return None

    # ── Analytics ─────────────────────────────────────────────────────────────

    def closed_trades(self) -> List[Dict]:
        return [t for t in self._trades if t["close_ts"] is not None]

    def open_trades(self) -> List[Dict]:
        return [t for t in self._trades if t["close_ts"] is None]

    def today_trades(self) -> List[Dict]:
        today = date.today().isoformat()
        return [t for t in self.closed_trades()
                if t["close_ts"] and t["close_ts"][:10] == today]

    def stats(self, trades: Optional[List[Dict]] = None) -> Dict:
        trades = trades or self.closed_trades()
        if not trades:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                    "total_pnl": 0, "avg_win": 0, "avg_loss": 0,
                    "profit_factor": 0, "best": 0, "worst": 0}

        pnls  = [t["pnl_usdt"] for t in trades if t["pnl_usdt"] is not None]
        wins  = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        gross_win  = sum(wins) if wins else 0
        gross_loss = abs(sum(losses)) if losses else 0
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

        return {
            "total":          len(pnls),
            "wins":           len(wins),
            "losses":         len(losses),
            "win_rate":       len(wins) / len(pnls) if pnls else 0,
            "total_pnl":      round(sum(pnls), 2),
            "avg_win":        round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss":       round(sum(losses) / len(losses), 2) if losses else 0,
            "profit_factor":  round(pf, 3),
            "best":           round(max(pnls), 2) if pnls else 0,
            "worst":          round(min(pnls), 2) if pnls else 0,
        }

    def daily_summary(self) -> Dict:
        today = self.today_trades()
        st = self.stats(today)
        # best/worst label
        def _label(t):
            return f"{t['symbol']} {t['direction']} {'+' if t['pnl_usdt']>0 else ''}{t['pnl_usdt']:.2f}"

        best_t  = max(today, key=lambda x: x["pnl_usdt"], default=None)
        worst_t = min(today, key=lambda x: x["pnl_usdt"], default=None)
        st["best_str"]  = _label(best_t)  if best_t  else "—"
        st["worst_str"] = _label(worst_t) if worst_t else "—"
        return st

    def symbol_performance(self) -> Dict[str, Dict]:
        """Per-symbol breakdown."""
        by_sym: Dict[str, List] = {}
        for t in self.closed_trades():
            by_sym.setdefault(t["symbol"], []).append(t)
        return {sym: self.stats(trades) for sym, trades in by_sym.items()}

    def best_symbols(self, top_n: int = 5) -> List[str]:
        """Return symbols sorted by total PnL descending."""
        perf = self.symbol_performance()
        ranked = sorted(perf.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
        return [sym for sym, _ in ranked[:top_n]]

    def print_summary(self):
        st = self.stats()
        logger.info("=" * 50)
        logger.info(f"  SNIPER BOT — TRADE SUMMARY")
        logger.info(f"  Total trades  : {st['total']}")
        logger.info(f"  Win rate      : {st['win_rate']*100:.1f}%")
        logger.info(f"  Total PnL     : {st['total_pnl']:.2f} USDT")
        logger.info(f"  Profit factor : {st['profit_factor']}")
        logger.info(f"  Best trade    : {st['best']:.2f}")
        logger.info(f"  Worst trade   : {st['worst']:.2f}")
        logger.info("=" * 50)
