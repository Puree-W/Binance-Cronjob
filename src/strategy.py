"""
Signal logic.

Each indicator strategy returns one of: "BUY", "SELL", "HOLD".
Signals are evaluated on the most recently CLOSED candle, so we look at
indicator values on rows -2 (prev) and -3 (prev-prev) to detect crossovers
without picking up the still-forming current candle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands

Signal = Literal["BUY", "SELL", "HOLD"]


# ---------- RSI ----------
def rsi_signal(df: pd.DataFrame, period: int, oversold: float, overbought: float) -> Signal:
    rsi = RSIIndicator(close=df["close"], window=period).rsi()
    if len(rsi) < 3:
        return "HOLD"
    prev, curr = rsi.iloc[-3], rsi.iloc[-2]
    # Cross UP through oversold → buy
    if prev <= oversold < curr:
        return "BUY"
    # Cross DOWN through overbought → sell
    if prev >= overbought > curr:
        return "SELL"
    return "HOLD"


# ---------- EMA crossover ----------
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


# ---------- Bollinger Bands (mean reversion) ----------
def bb_signal(df: pd.DataFrame, period: int, std_dev: float) -> Signal:
    """
    Mean-reversion: buy when price closes back ABOVE lower band after dipping below
    (oversold bounce), sell when price closes back BELOW upper band after poking above.
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
    # Bounce up off lower band
    if prev_c < prev_l and curr_c >= curr_l:
        return "BUY"
    # Rejection off upper band
    if prev_c > prev_u and curr_c <= curr_u:
        return "SELL"
    return "HOLD"


# ---------- MACD (signal-line crossover) ----------
def macd_signal(df: pd.DataFrame, fast: int, slow: int, signal: int) -> Signal:
    m = MACD(close=df["close"], window_fast=fast, window_slow=slow, window_sign=signal)
    line = m.macd()
    sig = m.macd_signal()
    if len(line) < 3 or len(sig) < 3:
        return "HOLD"
    prev_diff = line.iloc[-3] - sig.iloc[-3]
    curr_diff = line.iloc[-2] - sig.iloc[-2]
    if prev_diff <= 0 < curr_diff:
        return "BUY"
    if prev_diff >= 0 > curr_diff:
        return "SELL"
    return "HOLD"


# ---------- Combined (RSI + EMA must agree) ----------
def combined_signal(df: pd.DataFrame, rsi_cfg: dict, ema_cfg: dict) -> Signal:
    r = rsi_signal(df, rsi_cfg["period"], rsi_cfg["oversold"], rsi_cfg["overbought"])
    e = ema_signal(df, ema_cfg["fast"], ema_cfg["slow"])
    if r == e and r != "HOLD":
        return r
    return "HOLD"


# ---------- Confluence (all 4 indicators must agree) ----------
def confluence_signal(df: pd.DataFrame, cfg: dict) -> Signal:
    """
    High-conviction: RSI, EMA, BB, and MACD must ALL fire the same direction.
    Rare signals, but stronger.
    """
    r = rsi_signal(df, cfg["rsi"]["period"], cfg["rsi"]["oversold"], cfg["rsi"]["overbought"])
    e = ema_signal(df, cfg["ema"]["fast"], cfg["ema"]["slow"])
    b = bb_signal(df, cfg["bb"]["period"], cfg["bb"]["std_dev"])
    m = macd_signal(df, cfg["macd"]["fast"], cfg["macd"]["slow"], cfg["macd"]["signal"])
    sigs = {r, e, b, m}
    if sigs == {"BUY"}:
        return "BUY"
    if sigs == {"SELL"}:
        return "SELL"
    return "HOLD"


# ---------- Majority (at least 3 of 4 indicators agree) ----------
def majority_signal(df: pd.DataFrame, cfg: dict) -> Signal:
    """Practical confluence: 3 out of 4 indicators must agree."""
    sigs = [
        rsi_signal(df, cfg["rsi"]["period"], cfg["rsi"]["oversold"], cfg["rsi"]["overbought"]),
        ema_signal(df, cfg["ema"]["fast"], cfg["ema"]["slow"]),
        bb_signal(df, cfg["bb"]["period"], cfg["bb"]["std_dev"]),
        macd_signal(df, cfg["macd"]["fast"], cfg["macd"]["slow"], cfg["macd"]["signal"]),
    ]
    if sigs.count("BUY") >= 3:
        return "BUY"
    if sigs.count("SELL") >= 3:
        return "SELL"
    return "HOLD"


def evaluate(mode: str, df: pd.DataFrame, cfg: dict) -> Signal:
    """Dispatch. mode in {rsi, ema, bb, macd, combined, majority, confluence}."""
    if mode == "rsi":
        c = cfg["rsi"]
        return rsi_signal(df, c["period"], c["oversold"], c["overbought"])
    if mode == "ema":
        c = cfg["ema"]
        return ema_signal(df, c["fast"], c["slow"])
    if mode == "bb":
        c = cfg["bb"]
        return bb_signal(df, c["period"], c["std_dev"])
    if mode == "macd":
        c = cfg["macd"]
        return macd_signal(df, c["fast"], c["slow"], c["signal"])
    if mode == "combined":
        return combined_signal(df, cfg["rsi"], cfg["ema"])
    if mode == "majority":
        return majority_signal(df, cfg)
    if mode == "confluence":
        return confluence_signal(df, cfg)
    raise ValueError(f"Unknown strategy mode: {mode}")


def diagnostics(df: pd.DataFrame, cfg: dict) -> dict:
    """
    Compute current indicator readings for verbose logging.
    Uses the most recently CLOSED candle (index -2).
    """
    if len(df) < 3:
        return {}

    rsi = RSIIndicator(close=df["close"], window=cfg["rsi"]["period"]).rsi().iloc[-2]
    ef = EMAIndicator(close=df["close"], window=cfg["ema"]["fast"]).ema_indicator().iloc[-2]
    es = EMAIndicator(close=df["close"], window=cfg["ema"]["slow"]).ema_indicator().iloc[-2]
    bb = BollingerBands(close=df["close"], window=cfg["bb"]["period"], window_dev=cfg["bb"]["std_dev"])
    bb_l = bb.bollinger_lband().iloc[-2]
    bb_u = bb.bollinger_hband().iloc[-2]
    m = MACD(close=df["close"], window_fast=cfg["macd"]["fast"],
             window_slow=cfg["macd"]["slow"], window_sign=cfg["macd"]["signal"])
    macd_v = m.macd().iloc[-2]
    macd_s = m.macd_signal().iloc[-2]
    return {
        "close": float(df["close"].iloc[-2]),
        "rsi": float(rsi),
        "ema_fast": float(ef),
        "ema_slow": float(es),
        "bb_lower": float(bb_l),
        "bb_upper": float(bb_u),
        "macd": float(macd_v),
        "macd_signal": float(macd_s),
    }


# ---------- Grid (spot only) ----------
@dataclass
class GridLevel:
    side: str   # "BUY" or "SELL"
    price: float
    quote: float  # USDT to spend (BUY) or notional to receive (SELL)


def build_grid(center_price: float, levels: int, step_pct: float, quote_per_level: float) -> list[GridLevel]:
    """
    Returns a symmetric grid: `levels` BUY orders below center_price and
    `levels` SELL orders above. Step is multiplicative (compounds).
    """
    out: list[GridLevel] = []
    for i in range(1, levels + 1):
        buy_price = center_price * (1 - step_pct) ** i
        sell_price = center_price * (1 + step_pct) ** i
        out.append(GridLevel("BUY", buy_price, quote_per_level))
        out.append(GridLevel("SELL", sell_price, quote_per_level))
    return out
