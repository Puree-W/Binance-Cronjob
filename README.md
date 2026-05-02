# Binance Auto-Trading Bot

Automated trading on Binance Spot and USDT-M Futures.
Strategies: **RSI**, **EMA crossover**, **combined (RSI + EMA)**, and **Grid** (spot only).

> Defaults to **testnet**. Flip `USE_TESTNET=false` only after you've verified behavior on testnet.

## Project layout

```
Binance/
├── src/
│   ├── main.py          # entrypoint
│   ├── exchange.py      # Binance client wrapper (spot + futures)
│   ├── strategy.py      # RSI / EMA / combined / grid
│   ├── risk.py          # SL/TP, sizing, daily loss kill-switch
│   ├── spot_bot.py      # spot engine
│   ├── futures_bot.py   # futures engine
│   └── logger.py
├── config.yaml          # strategy + risk parameters
├── .env.example         # API keys template
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## 1. Get testnet API keys

- Spot:    https://testnet.binance.vision  → "Generate HMAC API Key"
- Futures: https://testnet.binancefuture.com  → log in with GitHub, then API Key

The two testnets are separate accounts with separate keys.

## 2. Setup

```bash
cp .env.example .env
# Edit .env and paste your testnet keys
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix:    source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Edit `config.yaml`

Key settings:

| Field | Meaning |
|---|---|
| `risk.stop_loss_pct` | SL as fraction of entry (`0.05` = 5%) |
| `risk.take_profit_pct` | TP as fraction of entry |
| `risk.order_size_usdt` | notional per trade |
| `risk.daily_loss_limit_pct` | kill-switch threshold |
| `spot.strategy` | `rsi` / `ema` / `combined` / `grid` |
| `futures.strategy` | `rsi` / `ema` / `combined` |
| `futures.leverage` | leverage multiplier |

> **Risk warning:** the shipped defaults are `SL 50% / TP 20%` because that's what was requested.
> That is a 2.5:1 risk-to-reward AGAINST you and a 50% SL is unreachable on 3x+ futures (you'd liquidate first). **Adjust before live trading.**

## 4. Test the connection (recommended before running)

```bash
python -m src.check_connection
```

This verifies:
- `.env` keys are loaded
- Spot + Futures testnet are reachable
- API keys have trade permission
- Configured symbols return prices
- Wallet balances (warns if zero — go fund the testnet)

Sample output when healthy:
```
[ OK ] .env loaded — all 4 API keys present
[ OK ] Spot server time OK (drift 12 ms)
[ OK ] Spot account reachable — canTrade=True
[ OK ] Spot price BTCUSDT = 67234.50
[ OK ] Futures wallet: total=15000 USDT  available=15000 USDT
[ OK ] All checks passed — bot is ready to run
```

## 5. Run

```bash
# both bots
python -m src.main

# spot only
python -m src.main --spot

# futures only
python -m src.main --futures
```

Stop with `Ctrl+C` — it finishes the current tick and exits cleanly.

## 6. Deploy on Fly.io (recommended free cloud)

Fly.io runs the Dockerfile as a 24/7 worker. API keys live in Fly's encrypted
secrets — never committed to the repo.

```bash
# 1. Install flyctl
#    Windows: iwr https://fly.io/install.ps1 -useb | iex
#    Mac:     brew install flyctl
#    Linux:   curl -L https://fly.io/install.sh | sh

# 2. Login
flyctl auth login

# 3. Pick a unique app name and create the app (does NOT deploy yet)
#    Either edit `app = "..."` in fly.toml first, or:
flyctl launch --no-deploy --copy-config --name <your-unique-name>

# 4. Set secrets (replace with your testnet keys)
flyctl secrets set \
  BINANCE_SPOT_API_KEY=xxx \
  BINANCE_SPOT_API_SECRET=xxx \
  BINANCE_FUTURES_API_KEY=xxx \
  BINANCE_FUTURES_API_SECRET=xxx \
  USE_TESTNET=true

# 5. Deploy
flyctl deploy

# 6. Watch logs
flyctl logs

# Useful ops
flyctl status                  # is it running?
flyctl secrets list            # what secrets are set (values hidden)
flyctl scale count 1           # ensure exactly 1 VM
flyctl ssh console             # shell into the running VM
flyctl apps destroy <name>     # tear it down
```

**Updating config / strategy:** edit `config.yaml`, commit, then `flyctl deploy` again. The new image rolls out with the new config.

**Cost:** the `shared-cpu-1x / 256mb` VM in `fly.toml` is the cheapest size. Fly used to include this in a free allowance; current pricing is roughly $2/mo. Confirm at https://fly.io/docs/about/pricing/

## 7. Docker (local or other cloud)

```bash
docker compose build
docker compose up -d              # both bots
docker compose logs -f spot       # tail logs
docker compose down               # stop
```

To run only one service: `docker compose up -d spot` or `docker compose up -d futures`.

Logs persist to `./logs/trades.log` (rotated at 10 MB, kept 30 days).

## Strategy notes

- **RSI** — buy when RSI crosses up through `oversold`, sell when it crosses down through `overbought`. Signals are evaluated on the most recently closed candle.
- **EMA** — buy on fast EMA crossing above slow, sell on cross below.
- **Combined** — both RSI and EMA must agree on the same direction; otherwise HOLD.
- **Grid (spot only)** — places `levels` limit BUYs below and `levels` limit SELLs above the current price, spaced by `step_pct`. Re-runs cancel and re-seed; restart the bot to refresh.

## Going live

1. Run on testnet for at least a few days. Watch `logs/trades.log` and verify entries/exits make sense.
2. Set `SL` and `TP` to sane values (e.g., `0.02` / `0.05`).
3. Lower `order_size_usdt` to a small live amount.
4. Set `USE_TESTNET=false` in `.env`.
5. Run — the bot prompts you to type `I UNDERSTAND` before starting in live mode.

Never commit `.env`.
