"""
Thin wrapper around python-binance for Spot and USDT-M Futures.
Handles testnet endpoints, candle fetching, order placement,
and position queries with retries on transient errors.
"""
from __future__ import annotations

import os
from decimal import Decimal, ROUND_DOWN
from typing import Literal

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

Market = Literal["spot", "futures"]

SPOT_TESTNET_URL = "https://testnet.binance.vision/api"
FUTURES_TESTNET_URL = "https://testnet.binancefuture.com"


class Exchange:
    def __init__(self, market: Market, use_testnet: bool):
        self.market = market
        self.testnet = use_testnet

        if market == "spot":
            key = os.getenv("BINANCE_SPOT_API_KEY", "")
            secret = os.getenv("BINANCE_SPOT_API_SECRET", "")
        else:
            key = os.getenv("BINANCE_FUTURES_API_KEY", "")
            secret = os.getenv("BINANCE_FUTURES_API_SECRET", "")

        if not key or not secret:
            env_key = f"BINANCE_{market.upper()}_API_KEY"
            env_secret = f"BINANCE_{market.upper()}_API_SECRET"
            raise RuntimeError(
                f"Missing API credentials for {market}. "
                f"Expected env vars {env_key} and {env_secret}. "
                f"Local: set in .env file. "
                f"Fly.io: run `flyctl secrets set {env_key}=... {env_secret}=...`"
            )

        self.client = Client(key, secret, testnet=use_testnet)

        if market == "spot" and use_testnet:
            self.client.API_URL = SPOT_TESTNET_URL
        if market == "futures" and use_testnet:
            self.client.FUTURES_URL = FUTURES_TESTNET_URL + "/fapi"

        self._symbol_filters: dict[str, dict] = {}
        self._last_kline_close: dict[str, pd.Timestamp] = {}  # stale-data guard

    # ---------- market data ----------
    @retry(
        retry=retry_if_exception_type(BinanceAPIException),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        if self.market == "spot":
            raw = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        else:
            raw = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        cols = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbav", "tqav", "ignore",
        ]
        df = pd.DataFrame(raw, columns=cols)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")

        # Stale-data guard: if the last closed candle is the same as the
        # previous call, the exchange returned a cached response — retry once.
        last_closed = df["open_time"].iloc[-2]
        cache_key = f"{symbol}_{interval}"
        if self._last_kline_close.get(cache_key) == last_closed:
            import time as _time
            _time.sleep(2)
            if self.market == "spot":
                raw = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
            else:
                raw = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            df = pd.DataFrame(raw, columns=cols)
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = df[c].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        self._last_kline_close[cache_key] = df["open_time"].iloc[-2]
        return df

    def get_price(self, symbol: str) -> float:
        if self.market == "spot":
            return float(self.client.get_symbol_ticker(symbol=symbol)["price"])
        return float(self.client.futures_symbol_ticker(symbol=symbol)["price"])

    # ---------- exchange filters / quantization ----------
    def _filters(self, symbol: str) -> dict:
        if symbol in self._symbol_filters:
            return self._symbol_filters[symbol]
        if self.market == "spot":
            info = self.client.get_symbol_info(symbol)
            filters = {f["filterType"]: f for f in info["filters"]}
        else:
            info = self.client.futures_exchange_info()
            sym = next(s for s in info["symbols"] if s["symbol"] == symbol)
            filters = {f["filterType"]: f for f in sym["filters"]}
        self._symbol_filters[symbol] = filters
        return filters

    def quantize_qty(self, symbol: str, qty: float) -> float:
        f = self._filters(symbol)
        step = Decimal(f.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(q)

    def quantize_price(self, symbol: str, price: float) -> float:
        f = self._filters(symbol)
        tick = Decimal(f.get("PRICE_FILTER", {}).get("tickSize", "0.01"))
        p = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return float(p)

    # ---------- spot orders ----------
    def spot_market_buy(self, symbol: str, quote_amount: float) -> dict:
        # quoteOrderQty buys whatever qty the given USDT amount can fill.
        return self.client.order_market_buy(symbol=symbol, quoteOrderQty=quote_amount)

    def spot_market_sell(self, symbol: str, qty: float) -> dict:
        qty = self.quantize_qty(symbol, qty)
        return self.client.order_market_sell(symbol=symbol, quantity=qty)

    def spot_limit(self, symbol: str, side: str, qty: float, price: float) -> dict:
        qty = self.quantize_qty(symbol, qty)
        price = self.quantize_price(symbol, price)
        return self.client.create_order(
            symbol=symbol,
            side=side,
            type="LIMIT",
            timeInForce="GTC",
            quantity=qty,
            price=str(price),
        )

    def spot_open_orders(self, symbol: str) -> list[dict]:
        return self.client.get_open_orders(symbol=symbol)

    def spot_cancel_all(self, symbol: str) -> None:
        for o in self.spot_open_orders(symbol):
            try:
                self.client.cancel_order(symbol=symbol, orderId=o["orderId"])
            except BinanceAPIException:
                pass

    def spot_balance(self, asset: str) -> float:
        bal = self.client.get_asset_balance(asset=asset) or {}
        return float(bal.get("free", 0))

    # ---------- futures setup ----------
    def futures_set_leverage(self, symbol: str, leverage: int) -> None:
        self.client.futures_change_leverage(symbol=symbol, leverage=leverage)

    def futures_set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
        except BinanceAPIException as e:
            # -4046: no need to change margin type (already set)
            if e.code != -4046:
                raise

    # ---------- futures orders ----------
    def futures_market_order(self, symbol: str, side: str, qty: float) -> dict:
        qty = self.quantize_qty(symbol, qty)
        return self.client.futures_create_order(
            symbol=symbol, side=side, type="MARKET", quantity=qty
        )

    def futures_stop_market(self, symbol: str, side: str, stop_price: float, qty: float) -> dict:
        stop_price = self.quantize_price(symbol, stop_price)
        qty = self.quantize_qty(symbol, qty)
        return self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=str(stop_price),
            quantity=qty,
            reduceOnly="true",
            workingType="MARK_PRICE",
        )

    def futures_take_profit_market(self, symbol: str, side: str, stop_price: float, qty: float) -> dict:
        stop_price = self.quantize_price(symbol, stop_price)
        qty = self.quantize_qty(symbol, qty)
        return self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=str(stop_price),
            quantity=qty,
            reduceOnly="true",
            workingType="MARK_PRICE",
        )

    def futures_position(self, symbol: str) -> dict | None:
        positions = self.client.futures_position_information(symbol=symbol)
        for p in positions:
            if float(p["positionAmt"]) != 0:
                return p
        return None

    def futures_cancel_all(self, symbol: str) -> None:
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
        except BinanceAPIException:
            pass

    def futures_balance_usdt(self) -> float:
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["balance"])
        return 0.0
