"""
Spot trading engine.

Two modes (chosen via config.spot.strategy):
  - "rsi" / "ema" / "combined": signal-driven, market-buy on BUY,
    market-sell entire position on SELL. Tracks SL/TP per position.
  - "grid": maintains a symmetric grid of limit orders around mid price.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from binance.exceptions import BinanceAPIException

from .exchange import Exchange
from .logger import get_logger
from .risk import KillSwitch, stop_loss_price, take_profit_price


@dataclass
class SpotPosition:
    symbol: str
    qty: float
    entry: float
    sl: float
    tp: float


class SpotBot:
    def __init__(self, cfg: dict, use_testnet: bool):
        self.cfg = cfg
        self.spot_cfg = cfg["spot"]
        self.risk_cfg = cfg["risk"]
        self.symbols = cfg["symbols"]
        self.interval = cfg["interval"]
        self.verbose = bool(cfg.get("verbose", False))
        self.exchange = Exchange("spot", use_testnet)
        self.log = get_logger("SPOT")
        self.positions: dict[str, SpotPosition] = {}
        self.kill = KillSwitch(self.risk_cfg["daily_loss_limit_pct"])
        self.grid_initialised: dict[str, bool] = {s: False for s in self.symbols}

    # ---------- main tick ----------
    def tick(self) -> None:
        usdt_balance = self.exchange.spot_balance("USDT")
        self.kill.reset_if_new_day(usdt_balance)

        if not self.kill.can_trade():
            self.log.warning("Kill-switch tripped — skipping tick")
            return

        mode = self.spot_cfg["strategy"]
        for symbol in self.symbols:
            try:
                if mode == "grid":
                    self._tick_grid(symbol)
                else:
                    self._tick_signal(symbol, mode)
            except BinanceAPIException as e:
                self.log.error(f"{symbol} API error: {e}")
            except Exception as e:
                self.log.exception(f"{symbol} tick failed: {e}")

    # ---------- signal-driven ----------
    def _tick_signal(self, symbol: str, mode: str) -> None:
        from .strategy import evaluate, diagnostics

        df = self.exchange.get_klines(symbol, self.interval, limit=100)
        sig = evaluate(mode, df, self.spot_cfg)
        price = self.exchange.get_price(symbol)

        if self.verbose:
            d = diagnostics(df, self.spot_cfg)
            if d:
                self.log.info(
                    f"{symbol} px={price:.2f} sig={sig} "
                    f"ST={d['st_dir'].upper()}({d['st_val']:.2f}) "
                    f"RSI={d['rsi']:.1f} "
                    f"BB[{d['bb_lower']:.2f}..{d['bb_upper']:.2f}]"
                )

        pos = self.positions.get(symbol)

        # SL/TP check first
        if pos:
            if price <= pos.sl or price >= pos.tp:
                reason = "SL" if price <= pos.sl else "TP"
                self._close_position(symbol, pos, price, reason)
                return

        if sig == "BUY" and pos is None:
            if len(self.positions) >= self.risk_cfg["max_open_positions"]:
                self.log.info(f"{symbol} BUY signal but max positions reached")
                return
            self._open_long(symbol, price)
        elif sig == "SELL" and pos is not None:
            self._close_position(symbol, pos, price, "SIGNAL")

    def _open_long(self, symbol: str, price: float) -> None:
        quote = self.risk_cfg["order_size_usdt"]
        order = self.exchange.spot_market_buy(symbol, quote)
        # Compute filled qty + avg fill price from the order response.
        fills = order.get("fills", [])
        if fills:
            qty = sum(float(f["qty"]) for f in fills)
            spent = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            avg = spent / qty if qty else price
        else:
            qty = float(order.get("executedQty", 0))
            avg = price
        sl = stop_loss_price(avg, self.risk_cfg["stop_loss_pct"], "BUY")
        tp = take_profit_price(avg, self.risk_cfg["take_profit_pct"], "BUY")
        self.positions[symbol] = SpotPosition(symbol, qty, avg, sl, tp)
        self.log.info(f"{symbol} LONG entry={avg:.4f} qty={qty} SL={sl:.4f} TP={tp:.4f}")

    def _close_position(self, symbol: str, pos: SpotPosition, price: float, reason: str) -> None:
        self.exchange.spot_market_sell(symbol, pos.qty)
        pnl = (price - pos.entry) * pos.qty
        self.kill.record_pnl(pnl)
        self.positions.pop(symbol, None)
        self.log.info(f"{symbol} EXIT {reason} exit={price:.4f} pnl={pnl:+.4f} USDT")

    # ---------- grid mode ----------
    def _tick_grid(self, symbol: str) -> None:
        from .strategy import build_grid

        if self.grid_initialised.get(symbol):
            return  # grid orders are GTC; nothing to do until they fill or get cancelled

        center = self.exchange.get_price(symbol)
        g = self.spot_cfg["grid"]
        levels = build_grid(center, g["levels"], g["step_pct"], g["quote_per_level"])

        # Cancel any prior orders so the grid is consistent
        self.exchange.spot_cancel_all(symbol)

        for lvl in levels:
            try:
                if lvl.side == "BUY":
                    qty = lvl.quote / lvl.price
                else:
                    base = symbol.replace("USDT", "")
                    free = self.exchange.spot_balance(base)
                    qty = min(free, lvl.quote / lvl.price)
                    if qty <= 0:
                        continue
                self.exchange.spot_limit(symbol, lvl.side, qty, lvl.price)
            except BinanceAPIException as e:
                self.log.warning(f"{symbol} grid {lvl.side}@{lvl.price:.4f} rejected: {e.message}")

        self.grid_initialised[symbol] = True
        self.log.info(f"{symbol} grid placed center={center:.4f} levels={g['levels']*2}")
