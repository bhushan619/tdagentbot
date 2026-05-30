"""FastAPI backend for the dashboard.

Reads bot_state.json (written by the bot), serves config GET/POST,
returns last 20 trades of the current session, and tails /tmp/bot.log.
"""

from __future__ import annotations

import json
import sqlite3
import os
import pandas as pd
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent.parent

STATE_FILE = ROOT / "bot_state.json"
CONFIG_FILE = ROOT / "bot_config.json"

DB_FILE = ROOT / "logs" / "trades.db"

BOT_LOG = Path("/tmp/bot.log")

DEFAULT_CONFIG = {
    "mode": "DEMO",
    "trade_amount": 5,
    "trade_duration_sec": 60,
    "duration_mode": "MANUAL",
    "trade_duration_label": "S15",
    "confidence_threshold": 0.65,
    "max_consecutive_losses": 3,
    "max_daily_loss": 50,
    "bypass_risk_in_demo": True,
    "min_candles_required": 50,
    "candle_interval_sec": 5,
}

ALLOWED_DURATIONS = {
    3,
    15,
    30,
    60,
    180,
    300,
    1800,
    3600,
}

app = FastAPI(title="TD Agent Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _read_json(path: Path, default: dict) -> dict:

    if not path.exists():
        return default.copy()

    try:

        data = json.loads(path.read_text())

        if isinstance(data, dict):

            merged = default.copy()

            merged.update(data)

            return merged

    except Exception:
        pass

    return default.copy()


def _as_float(
    data: dict,
    key: str,
    lo: float,
    hi: float,
) -> float:

    try:

        val = float(data[key])

    except Exception:

        raise HTTPException(
            400,
            f"{key} must be a number",
        )

    if not (lo <= val <= hi):

        raise HTTPException(
            400,
            f"{key} must be between {lo} and {hi}",
        )

    return val


def _as_int(
    data: dict,
    key: str,
    lo: int,
    hi: int,
) -> int:

    val = _as_float(data, key, lo, hi)

    if int(val) != val:

        raise HTTPException(
            400,
            f"{key} must be a whole number",
        )

    return int(val)


def _validate_duration(value: int) -> int:

    if value not in ALLOWED_DURATIONS:

        raise HTTPException(
            400,
            f"Unsupported duration: {value}",
        )

    return value


def _clean_config(
    payload: dict[str, Any],
) -> dict:

    current = _read_json(
        CONFIG_FILE,
        DEFAULT_CONFIG,
    )

    data = DEFAULT_CONFIG.copy()

    data.update(current)

    data.update(payload or {})

    mode = str(
        data.get("mode", "DEMO")
    ).upper()

    duration_mode = str(
        data.get("duration_mode", "MANUAL")
    ).upper()

    if duration_mode not in ("AUTO", "MANUAL"):

        raise HTTPException(
            400,
            "duration_mode must be AUTO or MANUAL",
        )

    if mode not in ("DEMO", "LIVE"):

        raise HTTPException(
            400,
            "mode must be DEMO or LIVE",
        )

    return {

        "mode": mode,

        "trade_amount": _as_float(
            data,
            "trade_amount",
            0.01,
            10000,
        ),

        "duration_mode": duration_mode,

        "trade_duration_sec": _validate_duration(
            _as_int(
                data,
                "trade_duration_sec",
                3,
                3600,
            )
        ),

        "confidence_threshold": _as_float(
            data,
            "confidence_threshold",
            0,
            1,
        ),

        "max_consecutive_losses": _as_int(
            data,
            "max_consecutive_losses",
            1,
            100,
        ),

        "max_daily_loss": _as_float(
            data,
            "max_daily_loss",
            0,
            100000,
        ),

        "bypass_risk_in_demo": bool(
            data.get(
                "bypass_risk_in_demo",
                True,
            )
        ),

        "min_candles_required": _as_int(
            data,
            "min_candles_required",
            5,
            5000,
        ),

        "candle_interval_sec": _as_int(
            data,
            "candle_interval_sec",
            1,
            300,
        ),
    }


@app.get("/status")
def status() -> dict:

    return _read_json(
        STATE_FILE,
        {},
    )


@app.get("/config")
def get_config() -> dict:

    return _read_json(
        CONFIG_FILE,
        DEFAULT_CONFIG,
    )


@app.post("/config")
async def post_config(
    request: Request,
) -> dict:

    try:

        payload = await request.json()

    except Exception:

        raise HTTPException(
            400,
            "invalid JSON",
        )

    if not isinstance(payload, dict):

        raise HTTPException(
            400,
            "config must be an object",
        )

    data = _clean_config(payload)

    tmp = CONFIG_FILE.with_suffix(".tmp")

    tmp.write_text(
        json.dumps(
            data,
            indent=2,
        )
    )

    tmp.replace(CONFIG_FILE)

    return {
        "ok": True,
        "config": data,
    }


@app.get("/logs")
def logs() -> list[dict]:

    if not DB_FILE.exists():
        return []

    state = _read_json(
        STATE_FILE,
        {},
    )

    session_id = state.get("session_id")

    conn = sqlite3.connect(DB_FILE)

    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT
            ts,
            symbol,
            direction,
            amount,
            duration,
            confidence,
            result,
            pnl,
            profit_percent,
            balance_after,
            mode,
            session_id
        FROM trades
        WHERE session_id = ?
        ORDER BY id DESC
        LIMIT 500
        """,
        (session_id,),
    ).fetchall()

    conn.close()

    return [dict(r) for r in rows]


@app.get("/bot-logs")
def bot_logs(
    lines: int = 300,
) -> dict:

    if not BOT_LOG.exists():

        return {
            "lines": [],
        }

    try:

        with BOT_LOG.open("rb") as f:

            f.seek(0, 2)

            size = f.tell()

            chunk = min(size, 200_000)

            f.seek(-chunk, 2)

            data = f.read().decode(
                "utf-8",
                errors="ignore",
            )

        out = data.splitlines()[-lines:]

        return {
            "lines": out,
        }

    except Exception as e:

        raise HTTPException(
            500,
            str(e),
        )


@app.get("/trades/export")
def export_trades():

    if not DB_FILE.exists():

        raise HTTPException(
            404,
            "No trades database found",
        )

    state = _read_json(
        STATE_FILE,
        {},
    )

    session_id = state.get("session_id")

    conn = sqlite3.connect(DB_FILE)

    query = """
    SELECT
        ts,
        symbol,
        direction,
        amount,
        duration,
        confidence,
        result,
        pnl,
        profit_percent,
        balance_after,
        mode,
        session_id
    FROM trades
    WHERE session_id = ?
    ORDER BY id DESC
    """

    df = pd.read_sql_query(
        query,
        conn,
        params=(session_id,),
    )

    conn.close()

    export_dir = ROOT / "storage" / "exports"

    export_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    export_file = (
        export_dir /
        f"trades_{session_id}.csv"
    )

    df.to_csv(
        export_file,
        index=False,
    )

    return FileResponse(
        path=str(export_file),
        media_type="text/csv",
        filename=export_file.name,
    )