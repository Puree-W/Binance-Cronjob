"""
USDT-M Futures trading engine.

On BUY signal: open LONG with MARKET order, then attempt to place STOP_MARKET (SL)
and TAKE_PROFIT_MARKET (TP) reduceOnly orders on the exchange.
On SELL signal: open SHORT with the same SL/TP logic mirrored.

If a position already exists for the symbol — in either direction — the bot
ignores the new signal and lets the SL/TP do the exit. Auto-flipping on the
opposite signal was removed: it caused effective R:R to collapse to ~1:1
because positions were closed before TP was reached.

SL/TP are ATR-based (`risk.stop_loss_atr_mult` × ATR, mirrored for TP),
which adapts to per-symbol volatility instead of a fixed % that's either
too tight or too loose depending on the symbol.

Cooldown: after an SL hit, the symbol is blocked from re-entry for
`risk.cooldown_candles` × interval seconds. Prevents re-entering directly
into a continuing trend that just stopped us out.

Kill-switch: realized PnL from each close is recorded so the daily loss
cap (`risk.daily_loss_limit_pct`) actually trips on futures.

Bracket-order resilience:
  Some Binance accounts/testnets reject STOP_MARKET / TAKE_PROFIT_MARKET on the
  /fapi/v1/order endpoint. When that happens we keep the position open and
  rely on bot-side SL/TP monitoring (`_check_sl_tp_local`).

Direction filter (config.futures.allow_long / allow_short):
  - allow_long=true,  allow_short=true   → both directions (default)
  - allow_long=true,  allow_short=false  → long-only
  - allow_long=false, allow_short=true   → short-only
"""
from __future__ import annotations

import time

from binance.exceptions import BinanceAPIException

from .exchange import Exchange
from .logger import get_logger
from .risk import KillSwitch, stop_loss_price_atr, take_profit_price_atr

# Map config interval string → seconds (used to compute cooldown duration)
_INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400,
}


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
        # symbol → unix timestamp when re-entry is allowed (after SL hit)
        self._cooldown_until: dict[str, float] = {}

    def _cooldown_seconds(self) -> int:
        candles = int(self.risk_cfg.get("cooldown_candles", 0))
        return candles * _INTERVAL_SECONDS.get(self.interval, 3600)

    def _in_cooldown(self, symbol: str) -> bool:
        until = self._cooldown_until.get(symbol, 0.0)
        return time.time() < until

    def _start_cooldown(self, symbol: str) -> None:
        secs = self._cooldown_seconds()
        if secs > 0:
            self._cooldown_until[symbol] = time.time() + secs
            self.log.info(f"{symbol} cooldown started — re-entry blocked for {secs}s")

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

    def _check_sl_tp_local(self, symbol: str, price: float, df) -> bool:
        """
        Bot-side SL/TP backstop. Reads entryPrice from the exchange,
        computes ATR-based SL/TP from current klines, closes via market
        order if breached.

        ATR is recomputed each tick — slight drift on candle close is
        accepted (mirrors a coarse trailing-ATR stop, monotonic within
        the same candle).

        On close: records realized PnL to the kill-switch and (if the
        exit was an SL hit) starts the symbol cooldown.

        Returns True if a close was executed (caller should skip signal logic).
        """
        from .strategy import current_atr

        pos = self.exchange.futures_position(symbol)
        if not pos:
            return False
        pos_amt = float(pos["positionAmt"])
        if pos_amt == 0:
            return False
        entry = float(pos["entryPrice"])
        if entry <= 0:
            return False

        atr = current_atr(df, self.risk_cfg["atr_period"])
        if atr <= 0:
            return False

        side = "BUY" if pos_amt > 0 else "SELL"
        sl = stop_loss_price_atr(entry, atr, self.risk_cfg["stop_loss_atr_mult"], side)
        tp = take_profit_price_atr(entry, atr, self.risk_cfg["take_profit_atr_mult"], side)

        hit = None
        if pos_amt > 0:  # LONG
            if price <= sl:
                hit = "SL"
            elif price >= tp:
                hit = "TP"
        else:  # SHORT
            if price >= sl:
                hit = "SL"
            elif price <= tp:
                hit = "TP"

        if not hit:
            return False

        close_side = "SELL" if pos_amt > 0 else "BUY"
        self.exchange.futures_market_order(symbol, close_side, abs(pos_amt))
        self.exchange.futures_cancel_all(symbol)
        old_dir = "LONG" if pos_amt > 0 else "SHORT"
        pnl_usdt = (price - entry) * pos_amt  # signed: + for winning long, - for losing long
        pnl_pct = ((price - entry) / entry) * (1 if pos_amt > 0 else -1) * 100

        self.kill.record_pnl(pnl_usdt)
        if hit == "SL":
            self._start_cooldown(symbol)

        self.log.info(
            f"{symbol} {hit} hit (bot-side) — closed {old_dir} {abs(pos_amt):.6f} "
            f"entry={entry:.4f} exit={price:.4f} pnl={pnl_pct:+.2f}% "
            f"(${pnl_usdt:+.2f})"
        )
        return True

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

        # Bot-side SL/TP backstop — runs every tick regardless of signal.
        # Catches positions whose exchange-side bracket orders failed to place.
        if self._check_sl_tp_local(symbol, price, df):
            return

        if sig == "HOLD":
            return

        # Direction filter — defaults to both enabled if not set
        allow_long = self.fut_cfg.get("allow_long", True)
        allow_short = self.fut_cfg.get("allow_short", True)

        pos = self.exchange.futures_position(symbol)
        pos_amt = float(pos["positionAmt"]) if pos else 0.0

        # Already positioned (either direction) → ignore signal, let SL/TP exit.
        # Auto-flipping was removed: it collapsed effective R:R because
        # positions exited on opposite signals long before TP was reached.
        if pos_amt != 0:
            return

        # Cooldown after a recent stop-out
        if self._in_cooldown(symbol):
            return

        # Direction filter — block entries into disabled direction
        if sig == "BUY" and not allow_long:
            return
        if sig == "SELL" and not allow_short:
            return

        # Position cap (across symbols)
        open_count = sum(
            1 for s in self.symbols
            if (p := self.exchange.futures_position(s)) and float(p["positionAmt"]) != 0
        )
        if open_count >= self.risk_cfg["max_open_positions"]:
            self.log.info(f"{symbol} {sig} signal but max positions reached ({open_count})")
            return

        self._open(symbol, sig, price, df)

    def _open(self, symbol: str, side: str, price: float, df) -> None:
        from .strategy import current_atr

        atr = current_atr(df, self.risk_cfg["atr_period"])
        if atr <= 0:
            self.log.warning(f"{symbol} ATR unavailable — skipping entry")
            return

        notional = self.risk_cfg["order_size_usdt"] * self.fut_cfg["leverage"]
        qty = notional / price
        order_side = "BUY" if side == "BUY" else "SELL"

        # Clear any orphan SL/TP left over after the previous position closed via trigger
        self.exchange.futures_cancel_all(symbol)

        self.exchange.futures_market_order(symbol, order_side, qty)

        sl = stop_loss_price_atr(price, atr, self.risk_cfg["stop_loss_atr_mult"], side)
        tp = take_profit_price_atr(price, atr, self.risk_cfg["take_profit_atr_mult"], side)
        close_side = "SELL" if side == "BUY" else "BUY"

        bracket_ok = True
        try:
            self.exchange.futures_stop_market(symbol, close_side, sl, qty)
            self.exchange.futures_take_profit_market(symbol, close_side, tp, qty)
        except BinanceAPIException as e:
            bracket_ok = False
            # DO NOT close the position here — that round-trips for nothing.
            # Cancel any partial brackets that did succeed, then rely on
            # _check_sl_tp_local to monitor and exit on next ticks.
            self.log.warning(
                f"{symbol} bracket order failed: {e.message} — "
                f"falling back to bot-side SL/TP monitoring"
            )
            try:
                self.exchange.futures_cancel_all(symbol)
            except BinanceAPIException:
                pass

        direction = "LONG" if side == "BUY" else "SHORT"
        guard = "exchange" if bracket_ok else "bot-side"
        sl_pct = abs(sl - price) / price * 100
        tp_pct = abs(tp - price) / price * 100
        self.log.info(
            f"{symbol} {direction} entry={price:.4f} qty={qty:.6f} "
            f"SL={sl:.4f}(-{sl_pct:.2f}%) TP={tp:.4f}(+{tp_pct:.2f}%) "
            f"ATR={atr:.4f} lev={self.fut_cfg['leverage']}x guard={guard}"
        )
