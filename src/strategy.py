"""
Signal logic.

Active indicators  : Supertrend, RSI, Bollinger Bands
Removed            : MACD (redundant with EMA-family; see commit notes)
EMA                : kept for standalone mode only, not used in majority/confluence

Each indicator returns: "BUY" | "SELL" | "HOLD"

Evaluation is always on the most recently CLOSED candle (index -2),
never the still-forming current candle (index -1).

Strategy modes
--------------
  supertrend  → single Supertrend signal
  rsi         → single RSI signal
  bb          → single BB signal
  ema         → single EMA crossover signal (standalone, not in voting)
  combined    → RSI + EMA must agree (legacy mode)
  majority    → 2 of 3 (Supertrend + RSI + BB) agree
  confluence  → all 3 (Supertrend + RSI + BB) agree   ← strictest
  mean_revert → counter-trend: buy oversold bounces in downtrends,
                sell overbought rejections in uptrends             ← active

Supertrend acts as a trend FILTER (zone-based):
  - votes BUY  continuously while price is in bullish zone (above band)
  - votes SELL continuously while price is in bearish zone (below band)
  RSI + BB provide the entry TIMING on top of that context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

Signal = Literal["BUY", "SELL", "HOLD"]


# ================================================================
# Supertrend
# ================================================================

def _compute_supertrend(
    df: pd.DataFrame, period: int, multiplier: float
) -> tuple[pd.Series, pd.Series]:
    """
    Returns (direction, line) Series.
    direction: 'up' = bullish zone, 'down' = bearish zone
    line     : the Supertrend value (support/resistance level)
    """
    atr = AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=period
    ).average_true_range()

    hl2 = (df["high"] + df["low"]) / 2
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    n = len(df)
    final_upper = basic_upper.copy().values.astype(float)
    final_lower = basic_lower.copy().values.astype(float)
    close = df["close"].values.astype(float)
    st_line = np.full(n, np.nan)
    direction = np.full(n, "", dtype=object)

    for i in range(1, n):
        # Final upper band
        if basic_upper.values[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper.values[i]
        else:
            final_upper[i] = final_upper[i - 1]

        # Final lower band
        if basic_lower.values[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower.values[i]
        else:
            final_lower[i] = final_lower[i - 1]

        # Direction
        if i == 1:
            st_line[i] = final_upper[i]
            direction[i] = "down"
        elif st_line[i - 1] == final_upper[i - 1]:
            if close[i] <= final_upper[i]:
                st_line[i] = final_upper[i]
                direction[i] = "down"
            else:
                st_line[i] = final_lower[i]
                direction[i] = "up"
        else:  # was on lower (bullish)
            if close[i] >= final_lower[i]:
                st_line[i] = final_lower[i]
                direction[i] = "up"
            else:
                st_line[i] = final_upper[i]
                direction[i] = "down"

    return (
        pd.Series(direction, index=df.index),
        pd.Series(st_line, index=df.index),
    )


def supertrend_signal(df: pd.DataFrame, period: int, multiplier: float) -> Signal:
    """
    Zone-based vote: BUY while price is above Supertrend (bullish zone),
    SELL while price is below it (bearish zone).
    This makes Supertrend a continuous TREND FILTER rather than a one-shot crossover.
    """
    if len(df) < period + 2:
        return "HOLD"
    direction, _ = _compute_supertrend(df, period, multiplier)
    curr = direction.iloc[-2]   # last CLOSED candle
    if curr == "up":
        return "BUY"
    if curr == "down":
        return "SELL"
    return "HOLD"


# ================================================================
# RSI
# ================================================================

def rsi_signal(df: pd.DataFrame, period: int, oversold: float, overbought: float) -> Signal:
    """Crossover: buy when RSI crosses UP through oversold, sell when crosses DOWN through overbought."""
    rsi = RSIIndicator(close=df["close"], window=period).rsi()
    if len(rsi) < 3:
        return "HOLD"
    prev, curr = rsi.iloc[-3], rsi.iloc[-2]
    if prev <= oversold < curr:
        return "BUY"
    if prev >= overbought > curr:
        return "SELL"
    return "HOLD"


# ================================================================
# Bollinger Bands
# ================================================================

def bb_signal(df: pd.DataFrame, period: int, std_dev: float) -> Signal:
    """
    Mean-reversion: buy when price bounces back above lower band,
    sell when price falls back below upper band.
    """
    bb = BollingerBands(close=df["close"], window=period, window_dev=std_dev)
    lower = bb.bollinger_lband()
    upper = bb.bollinger_hband()
    close = df["close"]
    if len(close) < 3:
        return "HOLD"
    prev_c, curr_c = close.iloc[-3], close.iloc[-2]
    prev_l, curr_l = lower.iloc[-3], lower.iloc[-2]
    prev_u, curr_u = upper.iloc[-3], upper.iloc[-2]
    if prev_c < prev_l and curr_c >= curr_l:
        return "BUY"
    if prev_c > prev_u and curr_c <= curr_u:
        return "SELL"
    return "HOLD"


# ================================================================
# EMA crossover  (standalone mode only — not used in majority/confluence)
# ================================================================

def ema_signal(df: pd.DataFrame, fast: int, slow: int) -> Signal:
    ef = EMAIndicator(close=df["close"], window=fast).ema_indicator()
    es = EMAIndicator(close=df["close"], window=slow).ema_indicator()
    if len(ef) < 3 or len(es) < 3:
        return "HOLD"
    prev_diff = ef.iloc[-3] - es.iloc[-3]
    curr_diff = ef.iloc[-2] - es.iloc[-2]
    if prev_diff <= 0 < curr_diff:
        return "BUY"
    if prev_diff >= 0 > curr_diff:
        return "SELL"
    return "HOLD"


# ================================================================
# Composite modes
# ================================================================

def combined_signal(df: pd.DataFrame, rsi_cfg: dict, ema_cfg: dict) -> Signal:
    """Legacy: RSI + EMA must agree."""
    r = rsi_signal(df, rsi_cfg["period"], rsi_cfg["oversold"], rsi_cfg["overbought"])
    e = ema_signal(df, ema_cfg["fast"], ema_cfg["slow"])
    if r == e and r != "HOLD":
        return r
    return "HOLD"


def majority_signal(df: pd.DataFrame, cfg: dict) -> Signal:
    """
    2 of 3 must agree: Supertrend (trend filter) + RSI (timing) + BB (volatility).

    How it works in practice:
      Supertrend = BUY  (bullish zone)
      RSI        = BUY  (bouncing out of oversold)   → 2/3 → BUY ✅
      BB         = HOLD (no band touch needed)

      Supertrend = SELL (bearish zone)
      RSI        = BUY  (oversold bounce)             → 1/3 → HOLD ✅
      BB         = HOLD                                  (don't buy into downtrend)
    """
    st = cfg["supertrend"]
    sigs = [
        supertrend_signal(df, st["period"], st["multiplier"]),
        rsi_signal(df, cfg["rsi"]["period"], cfg["rsi"]["oversold"], cfg["rsi"]["overbought"]),
        bb_signal(df, cfg["bb"]["period"], cfg["bb"]["std_dev"]),
    ]
    if sigs.count("BUY") >= 2:
        return "BUY"
    if sigs.count("SELL") >= 2:
        return "SELL"
    return "HOLD"


def mean_revert_signal(df: pd.DataFrame, cfg: dict) -> Signal:
    """
    Counter-trend mean reversion with inverted Supertrend trend gate.

    Trend gate (PERMISSION):
      Supertrend = down → only BUY allowed (fade extreme dips in downtrend)
      Supertrend = up   → only SELL allowed (fade extreme rallies in uptrend)

    Entry timing (TRIGGER): RSI extreme cross OR BB band bounce.
    Either trigger fires the trade, as long as the trend gate permits it.

    Rationale: pure mean reversion in a directional market gets run over.
    Requiring an established trend on the wrong side means we only fade
    after price has stretched far enough to be statistically reverting,
    not on minor wiggles around the mean.
    """
    st = cfg["supertrend"]
    direction, _ = _compute_supertrend(df, st["period"], st["multiplier"])
    if len(direction) < 3:
        return "HOLD"
    st_dir = direction.iloc[-2]

    rsi_sig = rsi_signal(df, cfg["rsi"]["period"], cfg["rsi"]["oversold"], cfg["rsi"]["overbought"])
    bb_sig = bb_signal(df, cfg["bb"]["period"], cfg["bb"]["std_dev"])

    if st_dir == "down" and (rsi_sig == "BUY" or bb_sig == "BUY"):
        return "BUY"
    if st_dir == "up" and (rsi_sig == "SELL" or bb_sig == "SELL"):
        return "SELL"
    return "HOLD"


def confluence_signal(df: pd.DataFrame, cfg: dict) -> Signal:
    """All 3 must agree — strictest mode."""
    st = cfg["supertrend"]
    sigs = {
        supertrend_signal(df, st["period"], st["multiplier"]),
        rsi_signal(df, cfg["rsi"]["period"], cfg["rsi"]["oversold"], cfg["rsi"]["overbought"]),
        bb_signal(df, cfg["bb"]["period"], cfg["bb"]["std_dev"]),
    }
    if sigs == {"BUY"}:
        return "BUY"
    if sigs == {"SELL"}:
        return "SELL"
    return "HOLD"


# ================================================================
# Dispatch
# ================================================================

def evaluate(mode: str, df: pd.DataFrame, cfg: dict) -> Signal:
    """
    mode options:
      supertrend | rsi | bb | ema   → single indicator
      combined                      → RSI + EMA (legacy)
      majority                      → 2/3: Supertrend + RSI + BB  ← recommended
      confluence                    → 3/3: Supertrend + RSI + BB
    """
    if mode == "supertrend":
        c = cfg["supertrend"]
        return supertrend_signal(df, c["period"], c["multiplier"])
    if mode == "rsi":
        c = cfg["rsi"]
        return rsi_signal(df, c["period"], c["oversold"], c["overbought"])
    if mode == "bb":
        c = cfg["bb"]
        return bb_signal(df, c["period"], c["std_dev"])
    if mode == "ema":
        c = cfg["ema"]
        return ema_signal(df, c["fast"], c["slow"])
    if mode == "combined":
        return combined_signal(df, cfg["rsi"], cfg["ema"])
    if mode == "majority":
        return majority_signal(df, cfg)
    if mode == "confluence":
        return confluence_signal(df, cfg)
    if mode == "mean_revert":
        return mean_revert_signal(df, cfg)
    raise ValueError(f"Unknown strategy mode: {mode!r}")


def current_atr(df: pd.DataFrame, period: int) -> float:
    """ATR value on the last CLOSED candle. Returns 0 if not enough data."""
    if len(df) < period + 2:
        return 0.0
    atr = AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=period
    ).average_true_range()
    val = atr.iloc[-2]
    return float(val) if pd.notna(val) else 0.0


# ================================================================
# Diagnostics (verbose logging)
# ================================================================

def diagnostics(df: pd.DataFrame, cfg: dict) -> dict:
    """
    Returns current indicator readings for the last CLOSED candle.
    Used by verbose logging in bot engines.
    """
    if len(df) < 3:
        return {}

    rsi_val = RSIIndicator(close=df["close"], window=cfg["rsi"]["period"]).rsi().iloc[-2]
    bb = BollingerBands(close=df["close"], window=cfg["bb"]["period"], window_dev=cfg["bb"]["std_dev"])
    bb_l = bb.bollinger_lband().iloc[-2]
    bb_u = bb.bollinger_hband().iloc[-2]

    st_cfg = cfg["supertrend"]
    try:
        direction, st_line = _compute_supertrend(df, st_cfg["period"], st_cfg["multiplier"])
        st_dir = direction.iloc[-2]
        st_val = float(st_line.iloc[-2])
    except Exception:
        st_dir, st_val = "?", float("nan")

    return {
        "close":    float(df["close"].iloc[-2]),
        "rsi":      float(rsi_val),
        "bb_lower": float(bb_l),
        "bb_upper": float(bb_u),
        "st_dir":   str(st_dir),      # "up" or "down"
        "st_val":   st_val,           # Supertrend line value
    }


# ================================================================
# Grid (spot only)
# ================================================================

@dataclass
class GridLevel:
    side: str    # "BUY" or "SELL"
    price: float
    quote: float  # USDT to spend (BUY) or notional to receive (SELL)


def build_grid(
    center_price: float, levels: int, step_pct: float, quote_per_level: float
) -> list[GridLevel]:
    """
    Symmetric grid: `levels` limit BUYs below center and `levels` limit SELLs above.
    Step is multiplicative.
    """
    out: list[GridLevel] = []
    for i in range(1, levels + 1):
        out.append(GridLevel("BUY",  center_price * (1 - step_pct) ** i, quote_per_level))
        out.append(GridLevel("SELL", center_price * (1 + step_pct) ** i, quote_per_level))
    return out
