# TD Agent Bot — Pocket Option (LOCAL ONLY)

Automated trading bot for Pocket Option with a real-time monitoring
dashboard. Runs entirely on your Mac.

> **Disclaimer:** Binary options are extremely risky and banned for
> retail traders in many jurisdictions (EU/UK). This software is for
> educational/research use only. You are 100% responsible for any
> losses, account bans, or legal issues. Start in **DEMO** mode.

## Architecture

- `main.py` — Playwright (headed) bot. Listens to PO's Socket.IO
  websocket frames at the Python level, builds 5s candles, votes a
  signal, executes trades, writes `bot_state.json` each cycle.
- `dashboard/app.py` — FastAPI backend on :8000. Reads
  `bot_state.json`, serves config GET/POST, last 20 trades from
  SQLite, and tails `/tmp/bot.log`.
- `dashboard/src/App.jsx` — React 19 + Vite + Tailwind dashboard on
  :5173. Polls every 2s. Single-screen layout.
- `bot_state.json` / `bot_config.json` — shared state on disk.
- `logs/trades.db` — SQLite trade history.
- `screenshots/` — screenshot before each trade.

## Requirements

- macOS, Python 3.9+ (3.10/3.11 fine), Node 18+
- A Pocket Option account

## One-time setup

```bash
cd tdagentbot

# Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# Frontend
cd dashboard && npm install && cd ..
```

## Run

```bash
source .venv/bin/activate     # if not already active
./start.sh
```

Then:

1. The Chromium window opens at the Pocket Option login page.
2. **Log in manually.** Bot waits up to 180s for `cabinet` in the URL.
3. Open <http://localhost:5173> to watch the dashboard.
4. Edit config from the dashboard — bot reloads it every cycle.

Stop everything:

```bash
./stop.sh
```

## Files written / read

| File | By | Purpose |
|---|---|---|
| `bot_state.json` | bot → api | live metrics |
| `bot_config.json` | api → bot | live config |
| `logs/trades.db` | bot → api | trade history |
| `/tmp/bot.log` | bot → api | live log tail |
| `/tmp/backend.log` | uvicorn | backend output |
| `/tmp/frontend.log` | vite | frontend output |

## Notes / gotchas (matches original spec)

- Per-connection WS `pending = []` lives inside `on_websocket()`.
- React inputs filled via `click(click_count=3) + keyboard.type(delay=80)`.
- LOSS PnL forced to `-trade_amount` (PO reports `profit=0`).
- Trade lock `_trade_in_progress` prevents overlapping trades.
- URL mode guard re-navigates if PO redirects to the wrong account.
- Config hot-reloaded at the start of every `strategy_cycle`.

## Troubleshooting

- **Login times out**: log in faster, or restart `./start.sh`.
- **No ticks in logs**: PO sometimes serves a "Waiting for next session"
  state on weekends; OTC pairs (`EURUSD_otc`) trade 24/7.
- **Amount/Time not set**: PO sometimes A/B-tests the trade panel DOM.
  Inspect the input and update the xpath in `_set_react_input`.
- **`pandas_ta` import error**: with NumPy 2.x it breaks — pin
  `numpy==1.26.4` (already in requirements).
