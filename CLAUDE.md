# Binance Auto-Trading Bot

USDT-M futures + spot trading bot. Strategy logic in [src/strategy.py](src/strategy.py),
exchange wrapper in [src/exchange.py](src/exchange.py), bot engines in
[src/futures_bot.py](src/futures_bot.py) and [src/spot_bot.py](src/spot_bot.py).
Config lives in [config.yaml](config.yaml); secrets in `.env` (never commit).

Active futures strategy: `mean_revert` — counter-trend mean reversion gated by
inverted Supertrend, ATR-based SL/TP, post-stop cooldown, kill-switch wired.

---

## Skill: validate Python compilation

After editing any `.py` file in this project, run a syntax + import check
**before reporting the task as done**. The bot is deployed to fly.io —
a syntax error pushed to prod means the container crash-loops with no trades.

There is no test suite, so syntax/import validation is the minimum bar.

### Step 1 — AST syntax check (fast, no deps required)

Run on every Python file you touched. Pure-stdlib, ~50 ms.

```powershell
python -c "import ast; [ast.parse(open(f, encoding='utf-8').read(), filename=f) for f in ['src/strategy.py', 'src/risk.py', 'src/futures_bot.py']]; print('OK')"
```

Replace the file list with whichever files you edited. `OK` means parsed
cleanly. A `SyntaxError` traceback shows the exact line.

### Step 2 — YAML config check (when `config.yaml` was touched)

```powershell
python -c "import yaml; yaml.safe_load(open('config.yaml', encoding='utf-8')); print('YAML OK')"
```

### Step 3 — Import check (catches missing symbols / circular imports)

Step 1 only validates grammar; it won't catch `from .risk import does_not_exist`.
Run an import smoke test for the modules you changed:

```powershell
python -c "import sys; sys.path.insert(0, '.'); import os; os.environ.setdefault('BINANCE_FUTURES_API_KEY','x'); os.environ.setdefault('BINANCE_FUTURES_API_SECRET','x'); from src.futures_bot import FuturesBot; from src.strategy import evaluate, current_atr; from src.risk import stop_loss_price_atr, take_profit_price_atr; print('imports OK')"
```

The `os.environ.setdefault` lines are required because [src/exchange.py](src/exchange.py)
raises at import time if `BINANCE_FUTURES_API_KEY` / `_SECRET` are missing.
Use dummy values (`'x'`) — the import path doesn't make any API calls.

### Step 4 (optional) — strategy smoke test on synthetic data

Useful when changing strategy logic. Generates 60 candles of random walk and
runs `evaluate('mean_revert', df, cfg)` end-to-end:

```powershell
python -c "
import sys; sys.path.insert(0, '.')
import pandas as pd, numpy as np, yaml
from src.strategy import evaluate, current_atr
cfg = yaml.safe_load(open('config.yaml', encoding='utf-8'))
n = 60; rng = np.random.default_rng(0)
close = 100 + rng.normal(0, 1, n).cumsum()
df = pd.DataFrame({'open': close, 'high': close+0.5, 'low': close-0.5, 'close': close, 'volume': np.ones(n)})
print('signal:', evaluate('mean_revert', df, cfg['futures']))
print('ATR:', current_atr(df, cfg['risk']['atr_period']))
"
```

### When to skip

- Documentation-only changes (`*.md`)
- `.env` / config comment changes that don't alter values

### When NOT to skip

- Any `.py` edit, no matter how trivial
- Renaming a function or symbol (Step 3 catches missed call sites)
- Changing config keys that the bot reads (Step 2 + Step 3)
- Strategy logic edits (Step 4 is cheap insurance)

---

## Project conventions

- **No comment for what the code does** — names should explain it. Comments are reserved for *why* (non-obvious constraints, past incidents).
- **Stateless across restarts** — the bot is designed to be killable and resumable. Position state is fetched from the exchange, not held in process memory. The cooldown dict is the one in-memory exception (acceptable: a restart releases the cooldown, which is harmless).
- **Indicators evaluate on the last CLOSED candle** (`iloc[-2]`), never the still-forming current candle (`iloc[-1]`).
- **No exception swallowing** at strategy / order layer. Let `BinanceAPIException` bubble to `tick()` so the per-symbol loop logs and continues.

## Deploy notes

- Runs on fly.io as a long-lived process (no scheduler — internal loop with `loop_seconds`).
- Secrets via `flyctl secrets set BINANCE_FUTURES_API_KEY=… BINANCE_FUTURES_API_SECRET=…`.
- Testnet is controlled by `USE_TESTNET=true|false` env var.
