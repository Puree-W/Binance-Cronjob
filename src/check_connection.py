"""
Connection sanity check.

Verifies that:
  - .env keys load correctly
  - Spot testnet connects, account is reachable, server time syncs
  - Futures testnet connects, account is reachable
  - Configured symbols return valid prices
  - Balances are readable

Run:
    python -m src.check_connection
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def _print(tag: str, msg: str) -> None:
    print(f"{tag} {msg}")


def check_env() -> bool:
    needed = [
        "BINANCE_SPOT_API_KEY", "BINANCE_SPOT_API_SECRET",
        "BINANCE_FUTURES_API_KEY", "BINANCE_FUTURES_API_SECRET",
    ]
    missing = [k for k in needed if not os.getenv(k)]
    if missing:
        _print(FAIL, f".env missing keys: {', '.join(missing)}")
        return False
    _print(OK, ".env loaded — all 4 API keys present")
    return True


def check_spot(symbols: list[str], use_testnet: bool) -> bool:
    from .exchange import Exchange

    try:
        ex = Exchange("spot", use_testnet)
    except Exception as e:
        _print(FAIL, f"Spot client init failed: {e}")
        return False

    try:
        server_time = ex.client.get_server_time()["serverTime"]
        local_time = int(time.time() * 1000)
        drift = abs(server_time - local_time)
        _print(OK, f"Spot server time OK (drift {drift} ms)")
        if drift > 5000:
            _print(WARN, "Clock drift > 5s — may cause signature errors. Sync your clock.")
    except Exception as e:
        _print(FAIL, f"Spot server time failed: {e}")
        return False

    try:
        acct = ex.client.get_account()
        can_trade = acct.get("canTrade", False)
        _print(OK, f"Spot account reachable — canTrade={can_trade}")
        if not can_trade:
            _print(WARN, "Spot key does NOT have trade permission")
    except Exception as e:
        _print(FAIL, f"Spot account fetch failed: {e}")
        return False

    # Balances (non-zero only)
    nonzero = [
        (b["asset"], float(b["free"]), float(b["locked"]))
        for b in acct.get("balances", [])
        if float(b["free"]) > 0 or float(b["locked"]) > 0
    ]
    if nonzero:
        _print(OK, "Spot balances:")
        for asset, free, locked in nonzero[:10]:
            print(f"        {asset:<8} free={free:<14} locked={locked}")
    else:
        _print(WARN, "Spot account has zero balance — fund the testnet wallet at https://testnet.binance.vision")

    # Symbol prices
    for sym in symbols:
        try:
            price = ex.get_price(sym)
            _print(OK, f"Spot price {sym} = {price}")
        except Exception as e:
            _print(FAIL, f"Spot price {sym} failed: {e}")
            return False

    return True


def check_futures(symbols: list[str], use_testnet: bool) -> bool:
    from .exchange import Exchange

    try:
        ex = Exchange("futures", use_testnet)
    except Exception as e:
        _print(FAIL, f"Futures client init failed: {e}")
        return False

    try:
        server_time = ex.client.futures_time()["serverTime"]
        local_time = int(time.time() * 1000)
        drift = abs(server_time - local_time)
        _print(OK, f"Futures server time OK (drift {drift} ms)")
    except Exception as e:
        _print(FAIL, f"Futures server time failed: {e}")
        return False

    try:
        acct = ex.client.futures_account()
        can_trade = acct.get("canTrade", False)
        total_wallet = acct.get("totalWalletBalance", "0")
        avail = acct.get("availableBalance", "0")
        _print(OK, f"Futures account reachable — canTrade={can_trade}")
        _print(OK, f"Futures wallet: total={total_wallet} USDT  available={avail} USDT")
        if not can_trade:
            _print(WARN, "Futures key does NOT have trade permission")
        if float(total_wallet) == 0:
            _print(WARN, "Futures wallet empty — fund at https://testnet.binancefuture.com")
    except Exception as e:
        _print(FAIL, f"Futures account fetch failed: {e}")
        return False

    for sym in symbols:
        try:
            price = ex.get_price(sym)
            _print(OK, f"Futures price {sym} = {price}")
        except Exception as e:
            _print(FAIL, f"Futures price {sym} failed: {e}")
            return False

    # Open positions
    try:
        positions = [
            p for p in ex.client.futures_position_information()
            if float(p["positionAmt"]) != 0
        ]
        if positions:
            _print(OK, f"Open futures positions: {len(positions)}")
            for p in positions:
                print(f"        {p['symbol']:<10} amt={p['positionAmt']:<10} entry={p['entryPrice']}")
        else:
            _print(OK, "No open futures positions")
    except Exception as e:
        _print(WARN, f"Could not list positions: {e}")

    return True


def main() -> int:
    load_dotenv(ROOT / ".env")

    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"
    symbols = cfg["symbols"]

    print("=" * 60)
    print(f"Binance connection check — testnet={use_testnet}")
    print(f"Symbols: {symbols}")
    print("=" * 60)

    if not check_env():
        return 1

    print("\n--- SPOT ---")
    spot_ok = check_spot(symbols, use_testnet)

    print("\n--- FUTURES ---")
    fut_ok = check_futures(symbols, use_testnet)

    print("\n" + "=" * 60)
    if spot_ok and fut_ok:
        print(f"{OK} All checks passed — bot is ready to run")
        return 0
    print(f"{FAIL} Some checks failed — fix the issues above before running the bot")
    return 1


if __name__ == "__main__":
    sys.exit(main())
