"""Shared state between bot and dashboard API.

The bot writes BOT_STATE -> bot_state.json each cycle.
The dashboard API reads it. Config goes the other way.
"""
import json
import os
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).parent.resolve()
STATE_FILE = ROOT / "bot_state.json"
CONFIG_FILE = ROOT / "bot_config.json"

_lock = Lock()

BOT_STATE = {
    "balance": 1000,
    "pnl": 0,
    "win_rate": 0,
    "trades": 0,
    "wins": 0,
    "bot_status": "IDLE",
    "last_signal": None,
    "last_result": None,
    "consecutive_losses": 0,
    "daily_loss": 0,
    "mode": "DEMO",
    "symbol": "EURUSD_otc",
    "session_started_at": None,
}

BOT_CONFIG = {
    "mode": "DEMO",
    "trade_amount": 5,
    "trade_duration_sec": 60,
    "confidence_threshold": 0.65,
    "max_consecutive_losses": 3,
    "max_daily_loss": 50,
    "bypass_risk_in_demo": True,
    "min_candles_required": 50,
    "candle_interval_sec": 5,
}


def write_state(state: dict) -> None:
    with _lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, default=str, indent=2))
        tmp.replace(STATE_FILE)


def read_state() -> dict:
    if not STATE_FILE.exists():
        return BOT_STATE.copy()
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return BOT_STATE.copy()


def write_config(config: dict) -> None:
    with _lock:
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, indent=2))
        tmp.replace(CONFIG_FILE)


def read_config() -> dict:
    if not CONFIG_FILE.exists():
        write_config(BOT_CONFIG)
        return BOT_CONFIG.copy()
    try:
        cfg = BOT_CONFIG.copy()
        cfg.update(json.loads(CONFIG_FILE.read_text()))
        return cfg
    except Exception:
        return BOT_CONFIG.copy()


# Ensure config file exists on import
if not CONFIG_FILE.exists():
    write_config(BOT_CONFIG)
if not STATE_FILE.exists():
    write_state(BOT_STATE)
