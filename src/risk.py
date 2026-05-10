"""
Risk management:
- compute SL / TP price levels
- track daily PnL and trip a kill-switch when exceeded
- enforce max open positions
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


def stop_loss_price(entry: float, sl_pct: float, side: str) -> float:
    if side == "BUY":  # long
        return entry * (1 - sl_pct)
    return entry * (1 + sl_pct)


def take_profit_price(entry: float, tp_pct: float, side: str) -> float:
    if side == "BUY":
        return entry * (1 + tp_pct)
    return entry * (1 - tp_pct)


def stop_loss_price_atr(entry: float, atr: float, mult: float, side: str) -> float:
    """ATR-multiple stop. Adapts to per-symbol volatility."""
    if side == "BUY":
        return entry - mult * atr
    return entry + mult * atr


def take_profit_price_atr(entry: float, atr: float, mult: float, side: str) -> float:
    if side == "BUY":
        return entry + mult * atr
    return entry - mult * atr


def position_qty(quote_amount: float, price: float) -> float:
    return quote_amount / price


@dataclass
class KillSwitch:
    """
    Tracks realized PnL for the current day. When loss exceeds the limit
    (as a fraction of the day's starting balance), trips and blocks new entries.
    """
    daily_loss_limit_pct: float
    starting_balance: float = 0.0
    realized_pnl: float = 0.0
    today: date = field(default_factory=date.today)
    tripped: bool = False

    def reset_if_new_day(self, balance_now: float) -> None:
        if date.today() != self.today:
            self.today = date.today()
            self.starting_balance = balance_now
            self.realized_pnl = 0.0
            self.tripped = False

    def record_pnl(self, pnl: float) -> None:
        self.realized_pnl += pnl
        if self.starting_balance <= 0:
            return
        loss_frac = -self.realized_pnl / self.starting_balance
        if loss_frac >= self.daily_loss_limit_pct:
            self.tripped = True

    def can_trade(self) -> bool:
        return not self.tripped
