"""
USDT-M Futures trading engine.

On BUY signal: open LONG with MARKET order, place STOP_MARKET (SL) and
TAKE_PROFIT_MARKET (TP) closePosition orders.
On SELL signal: open SHORT with the same SL/TP logic mirrored.
If a position already exists in the opposite direction, close it first.
"""
from __future__ import annotations

from binance.exceptions import BinanceAPIException

from .exchange import Exchange
from .logger import get_logger
from .risk import KillSwitch, stop_loss_price, take_profit_price


class FuturesBot:
    def __init__(self, cfg: dict, use_testnet: bool):
        self.cfg = cfg
        self.fut_cfg = cfg["futures"]
        self.risk_cfg = cfg["risk"]
        self.symbols = cfg["symbols"]
        self.interval = cfg["interval"]
        self.verbose = bool(cfg.get("verbose", False))
        self.exchange = Exchange("futures", use_testnet)
        self.log = get_logger("FUTURES")
        self.kill = KillSwitch(self.risk_cfg["daily_loss_limit_pct"])
        self._configured: set[str] = set()

    def _configure_symbol(self, symbol: str) -> None:
        if symbol in self._configured:
            return
        try:
            self.exchange.futures_set_margin_type(symbol, self.fut_cfg["margin_type"])
            self.exchange.futures_set_leverage(symbol, self.fut_cfg["leverage"])
            self._configured.add(symbol)
        except BinanceAPIException as e:
            self.log.warning(f"{symbol} configure failed: {e.message}")

    def tick(self) -> None:
        bal = self.exchange.futures_balance_usdt()
        self.kill.reset_if_new_day(bal)

        if not self.kill.can_trade():
            self.log.warning("Kill-switch tripped — skipping tick")
            return

        mode = self.fut_cfg["strategy"]
        for symbol in self.symbols:
            try:
                self._configure_symbol(symbol)
                self._tick_symbol(symbol, mode)
            except BinanceAPIException as e:
                self.log.error(f"{symbol} API error: {e}")
            except Exception as e:
                self.log.exception(f"{symbol} tick failed: {e}")

    def _tick_symbol(self, symbol: str, mode: str) -> None:
        from .strategy import evaluate, diagnostics

        df = self.exchange.get_klines(symbol, self.interval, limit=100)
        sig = evaluate(mode, df, self.fut_cfg)
        price = self.exchange.get_price(symbol)

        if self.verbose:
            d = diagnostics(df, self.fut_cfg)
            if d:
                self.log.info(
                    f"{symbol} px={price:.2f} sig={sig} "
                    f"ST={d['st_dir'].upper()}({d['st_val']:.2f}) "
                    f"RSI={d['rsi']:.1f} "
                    f"BB[{d['bb_lower']:.2f}..{d['bb_upper']:.2f}]"
                )

        if sig == "HOLD":
            return
        pos = self.exchange.futures_position(symbol)
        pos_amt = float(pos["positionAmt"]) if pos else 0.0

        # Already long and signal still BUY (or already short on SELL) → no-op
        if (pos_amt > 0 and sig == "BUY") or (pos_amt < 0 and sig == "SELL"):
            return

        # Opposite-side position open → close it first (SL/TP brackets will be left dangling, cancel them).
        if pos_amt != 0:
            close_side = "SELL" if pos_amt > 0 else "BUY"
            self.exchange.futures_market_order(symbol, close_side, abs(pos_amt))
            self.exchange.futures_cancel_all(symbol)
            self.log.info(f"{symbol} flipped — closed {pos_amt} before re-entry")

        # Position cap (across symbols)
        open_count = sum(
            1 for s in self.symbols
            if (p := self.exchange.futures_position(s)) and float(p["positionAmt"]) != 0
        )
        if open_count >= self.risk_cfg["max_open_positions"]:
            self.log.info(f"{symbol} {sig} signal but max positions reached")
            return

        self._open(symbol, sig, price)

    def _open(self, symbol: str, side: str, price: float) -> None:
        notional = self.risk_cfg["order_size_usdt"] * self.fut_cfg["leverage"]
        qty = notional / price
        order_side = "BUY" if side == "BUY" else "SELL"
        self.exchange.futures_market_order(symbol, order_side, qty)

        sl = stop_loss_price(price, self.risk_cfg["stop_loss_pct"], side)
        tp = take_profit_price(price, self.risk_cfg["take_profit_pct"], side)
        close_side = "SELL" if side == "BUY" else "BUY"

        try:
            self.exchange.futures_stop_market(symbol, close_side, sl)
            self.exchange.futures_take_profit_market(symbol, close_side, tp)
        except BinanceAPIException as e:
            self.log.error(f"{symbol} bracket order failed: {e.message} — closing position")
            self.exchange.futures_market_order(symbol, close_side, qty)
            return

        direction = "LONG" if side == "BUY" else "SHORT"
        self.log.info(
            f"{symbol} {direction} entry={price:.4f} qty={qty:.6f} "
            f"SL={sl:.4f} TP={tp:.4f} lev={self.fut_cfg['leverage']}x"
        )
