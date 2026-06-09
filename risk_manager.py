"""
Risk Manager — validates signals, computes position sizes.
"""
import logging

log = logging.getLogger("qfjp.risk")

TIER_RANK = {"NONE": -1, "PRE": 0, "STD": 1, "FUEL": 2, "SUP": 3}


class RiskManager:
    def __init__(self, settings):
        self.settings           = settings
        self.last_reject_reason = ""
        self.last_qty           = 0.0

    def can_trade(self, state, signal: dict) -> bool:
        tier     = signal.get("tier", "NONE")
        min_tier = self.settings.MIN_TIER

        if tier == "NONE":
            self.last_reject_reason = "No signal"
            return False

        if TIER_RANK.get(tier, -1) < TIER_RANK.get(min_tier, 1):
            self.last_reject_reason = f"Tier {tier} below minimum {min_tier}"
            return False

        if tier == "PRE" and not self.settings.TRADE_PRE_SIGNALS:
            self.last_reject_reason = "PRE signals disabled"
            return False

        if state.open_trades >= self.settings.MAX_OPEN_TRADES:
            self.last_reject_reason = f"Max open trades ({self.settings.MAX_OPEN_TRADES}) reached"
            return False

        if state.daily_trades >= self.settings.MAX_DAILY_TRADES:
            self.last_reject_reason = f"Max daily trades ({self.settings.MAX_DAILY_TRADES}) reached"
            return False

        if state.circuit_broken:
            self.last_reject_reason = "Circuit breaker active"
            return False

        # Avoid duplicate symbol trade
        sym = signal.get("symbol", "")
        if sym in state.open_symbols:
            self.last_reject_reason = f"Already in trade for {sym}"
            return False

        self.last_reject_reason = ""
        return True

    def compute_qty(self, price: float, contract_value: float) -> float:
        capital  = self.settings.CAPITAL
        risk_pct = self.settings.RISK_PCT / 100.0
        leverage = self.settings.LEVERAGE
        notional = capital * risk_pct * leverage
        qty = notional / (price * max(contract_value, 0.001))
        return max(0.01, round(qty, 2))
