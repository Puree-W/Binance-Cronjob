"""
Entrypoint. Loads .env + config.yaml, starts spot and/or futures loops.

Run:
    python -m src.main              # both bots (whichever are enabled)
    python -m src.main --spot       # spot only
    python -m src.main --futures    # futures only
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .logger import get_logger

ROOT = Path(__file__).resolve().parent.parent
log = get_logger("MAIN")

_running = True


def _stop(*_):
    global _running
    _running = False
    log.info("Shutdown signal received — finishing current tick and exiting")


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spot", action="store_true", help="Run spot bot only")
    parser.add_argument("--futures", action="store_true", help="Run futures bot only")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = load_config()
    use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"

    if not use_testnet:
        log.warning("LIVE MODE — real funds at risk. Type 'I UNDERSTAND' within 10s to continue.")
        try:
            line = sys.stdin.readline().strip()
        except KeyboardInterrupt:
            return
        if line != "I UNDERSTAND":
            log.error("Aborted")
            return

    bots = []
    run_spot = args.spot or (not args.spot and not args.futures)
    run_futures = args.futures or (not args.spot and not args.futures)

    if run_spot and cfg["spot"]["enabled"]:
        from .spot_bot import SpotBot
        bots.append(SpotBot(cfg, use_testnet))
        log.info(f"Spot bot ready — strategy={cfg['spot']['strategy']} symbols={cfg['symbols']}")

    if run_futures and cfg["futures"]["enabled"]:
        from .futures_bot import FuturesBot
        bots.append(FuturesBot(cfg, use_testnet))
        log.info(
            f"Futures bot ready — strategy={cfg['futures']['strategy']} "
            f"lev={cfg['futures']['leverage']}x symbols={cfg['symbols']}"
        )

    if not bots:
        log.error("No bots enabled — check config.yaml")
        return

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    interval = cfg["loop_seconds"]
    verbose = bool(cfg.get("verbose", False))
    log.info(f"Starting loop — testnet={use_testnet} tick={interval}s verbose={verbose}")

    tick_n = 0
    while _running:
        tick_n += 1
        start = time.time()
        log.info(f"Tick #{tick_n} — evaluating {len(cfg['symbols'])} symbol(s) on {cfg['interval']}")
        for bot in bots:
            bot.tick()
        elapsed = time.time() - start
        log.info(f"Tick #{tick_n} done in {elapsed:.2f}s — next in {max(1, int(interval - elapsed))}s")
        sleep_for = max(1, interval - elapsed)
        for _ in range(int(sleep_for)):
            if not _running:
                break
            time.sleep(1)

    log.info("Stopped cleanly")


if __name__ == "__main__":
    main()
