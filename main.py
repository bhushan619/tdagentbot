"""Pocket Option AI Trading Bot.

Playwright-driven, headed Chromium. Listens to PO's binary Socket.IO
frames at the Python level (per-connection pending queue), builds 5s
OHLCV candles, computes indicators, votes on a signal, and executes
trades. Writes state to bot_state.json each cycle; reads bot_config.json
each cycle so the dashboard can edit config live.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

# pandas_ta 0.3.14 still calls Series.append/DataFrame.append, which were
# removed in pandas 2.x. Re-add the small compatibility shim before importing
# pandas_ta so the bot works with the pinned modern pandas version.
if not hasattr(pd.Series, "append"):
    def _series_append(self, to_append, ignore_index=False, verify_integrity=False):
        return pd.concat(
            [self, to_append],
            ignore_index=ignore_index,
            verify_integrity=verify_integrity,
        )

    pd.Series.append = _series_append

if not hasattr(pd.DataFrame, "append"):
    def _dataframe_append(self, other, ignore_index=False, verify_integrity=False, sort=False):
        if isinstance(other, dict):
            other = pd.DataFrame([other])
        return pd.concat(
            [self, other],
            ignore_index=ignore_index,
            verify_integrity=verify_integrity,
            sort=sort,
        )

    pd.DataFrame.append = _dataframe_append

import pandas_ta as ta
from playwright.async_api import async_playwright, Page, BrowserContext

from shared_state import (
    BOT_STATE, read_config, write_state, ROOT,
)
from ml.exporter import append_training_row
# ---------- constants ----------
DEMO_URL = "https://pocketoption.com/en/cabinet/demo-quick-high-low/"
LIVE_URL = "https://pocketoption.com/en/cabinet/quick-high-low/"

LOG_DIR = ROOT / "logs"
SCREENSHOT_DIR = ROOT / "screenshots"
DB_FILE = LOG_DIR / "trades.db"
LOG_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR.mkdir(exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


# ---------- SQLite ----------
def db_init() -> None:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT NOT NULL,
            symbol TEXT,
            direction TEXT,
            amount REAL,
            duration INTEGER,
            confidence REAL,
            result TEXT,
            pnl REAL,
            balance_after REAL,
            mode TEXT
        )
    """)
    conn.commit()
    conn.close()


def db_insert_trade(row: dict) -> None:
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """INSERT INTO trades
           (ts, session_id, symbol, direction, amount, duration,
            confidence, result, pnl, balance_after, mode)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["ts"], row["session_id"], row["symbol"], row["direction"],
            row["amount"], row["duration"], row["confidence"], row["result"],
            row["pnl"], row["balance_after"], row["mode"],
        ),
    )
    conn.commit()
    conn.close()


# ---------- Market data ----------
class MarketDataCollector:
    """Builds N-second OHLCV candles from raw ticks."""

    def __init__(self, interval_sec: int = 5, maxlen: int = 500) -> None:
        self.interval = interval_sec
        self.candles: deque[dict] = deque(maxlen=maxlen)
        self._current: Optional[dict] = None

    def add_tick(self, symbol: str, ts: float, price: float) -> None:
        bucket = int(ts // self.interval) * self.interval
        if self._current is None or self._current["t"] != bucket:
            if self._current is not None:
                self.candles.append(self._current)
                log(f"New Candle {symbol} O={self._current['o']:.5f} "
                    f"H={self._current['h']:.5f} L={self._current['l']:.5f} "
                    f"C={self._current['c']:.5f}")
            self._current = {
                "t": bucket, "symbol": symbol,
                "o": price, "h": price, "l": price, "c": price, "v": 1,
            }
        else:
            self._current["h"] = max(self._current["h"], price)
            self._current["l"] = min(self._current["l"], price)
            self._current["c"] = price
            self._current["v"] += 1

    def dataframe(self) -> pd.DataFrame:
        if not self.candles:
            return pd.DataFrame()
        df = pd.DataFrame(list(self.candles))
        df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                                "c": "close", "v": "volume"})
        return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["ema_fast"] = ta.ema(df["close"], length=9)
    df["ema_slow"] = ta.ema(df["close"], length=21)
    df["rsi"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"])
    if macd is not None and not macd.empty:
        df["macd"] = macd.iloc[:, 0]
        df["macd_signal"] = macd.iloc[:, 2]
    bb = ta.bbands(df["close"], length=20)
    if bb is not None and not bb.empty:
        df["bb_lower"] = bb.iloc[:, 0]
        df["bb_mid"] = bb.iloc[:, 1]
        df["bb_upper"] = bb.iloc[:, 2]
    df["ret"] = df["close"].pct_change()
    df["vol10"] = df["ret"].rolling(10).std()

    df["momentum"] = (
        df["close"] -
        df["close"].shift(5)
    )

    df["momentum_accel"] = (
        df["momentum"] -
        df["momentum"].shift(1)
    )

    df["candle_size"] = (
        df["high"] - df["low"]
    )

    df["body_size"] = (
        df["close"] - df["open"]
    ).abs()
    # ---------- Phase 1 ML Features ----------
    atr = ta.atr(
        df["high"],
        df["low"],
        df["close"],
        length=14
    )

    df["atr"] = atr

    df["ema_distance"] = (
        (df["ema_fast"] - df["ema_slow"]).abs()
    )

    candle_range = (
        df["high"] - df["low"]
    ).replace(0, 1e-9)

    upper_wick = (
        df["high"] - df[["open", "close"]].max(axis=1)
    )

    lower_wick = (
        df[["open", "close"]].min(axis=1) - df["low"]
    )

    df["upper_wick_ratio"] = (
        upper_wick / candle_range
    )

    df["lower_wick_ratio"] = (
        lower_wick / candle_range
    )

    return df


def vote_signal(df: pd.DataFrame) -> tuple[str, float]:
    """Return (direction, confidence). direction in {BUY, SELL, NONE}.

    Confidence blends *agreement* (how many indicators point the same way)
    with *strength* (how strong each indicator's reading is). This prevents
    the strategy from emitting conf=1.00 the moment all three indicators
    trivially agree on flat or barely-moving data.
    """
    if df.empty or len(df) < 21:
        return "NONE", 0.0
    last = df.iloc[-1]
    bull = bear = 0
    strengths: list[float] = []

    e9, e21 = last.get("ema_fast"), last.get("ema_slow")
    if pd.notna(e9) and pd.notna(e21):
        if e9 > e21:
            bull += 1
        else:
            bear += 1
        spread = abs(e9 - e21) / max(abs(e21), 1e-9)
        # On FX 0.0005 (~5 pips) is a meaningful spread => strength 1.0
        strengths.append(min(spread / 0.0005, 1.0))

    rsi = last.get("rsi")
    if pd.notna(rsi):
        if rsi > 55:
            bull += 1
        elif rsi < 45:
            bear += 1
        # Distance from neutral; >=20 pts -> 1.0
        strengths.append(min(abs(rsi - 50) / 20.0, 1.0))

    macd_v, sig_v = last.get("macd"), last.get("macd_signal")
    if pd.notna(macd_v) and pd.notna(sig_v):
        if macd_v > sig_v:
            bull += 1
        else:
            bear += 1
        denom = max(abs(macd_v) + abs(sig_v), 1e-9)
        strengths.append(min(abs(macd_v - sig_v) / denom, 1.0))

    total = bull + bear
    if total == 0:
        return "NONE", 0.0
    # Require at least a 2-vote margin (e.g. 3-0 or 2-0, not 2-1)
    if abs(bull - bear) < 2:
        return "NONE", 0.5
    agreement = max(bull, bear) / total
    avg_strength = sum(strengths) / len(strengths) if strengths else 0.0
    conf = 0.5 * agreement + 0.5 * avg_strength
    direction = "BUY" if bull > bear else "SELL"
    return direction, round(min(conf, 1.0), 2)
    
def choose_expiry(conf, atr, ema_distance):

    atr = float(atr or 0)
    ema_distance = float(ema_distance or 0)

    # strongest trends
    if (
        conf >= 0.85
        and atr >= 0.00020
        and ema_distance >= 0.00012
    ):
        return "M1", 60

    # strong continuation
    if (
        conf >= 0.72
        and ema_distance >= 0.00004
    ):
        return "S30", 30

    return None, None

# ---------- Bot ----------
class PocketOptionBot:
    def __init__(self) -> None:
        self.cfg = read_config()
        self.collector = MarketDataCollector(self.cfg["candle_interval_sec"])
        self.symbol = BOT_STATE["symbol"]
        self.balance = float(BOT_STATE["balance"])
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_started_at = datetime.now(timezone.utc).isoformat()

        self.trades = 0
        self.wins = 0
        self.draws = 0
        self.pnl = 0.0
        self.consecutive_losses = 0
        self.daily_loss = 0.0
        self.last_signal: Optional[str] = None
        self.last_result: Optional[str] = None

        self._closed_deals: list[dict] = []
        self._trade_in_progress = False
        self._page: Optional[Page] = None
        self._ctx: Optional[BrowserContext] = None
        self._ws_frames_seen = 0
        self._ws_labels_seen: set[str] = set()
        self._last_label_log = 0.0

        self.last_trade_ts = 0
        self.last_trade_candle_ts = None
        self.last_saved_ts = None

        self.start_balance = 0.0

    # ----- cooldown active -----

    def _cooldown_active(self) -> bool:

        cooldown = self.cfg.get(
            "trade_cooldown_sec",
            10
        )

        return (
            time.time() - self.last_trade_ts
        ) < cooldown

    # ----- state sync -----
    def _push_state(self, status: str) -> None:

        losses = self.trades - self.wins - self.draws

        effective_trades = self.wins + losses

        win_rate = (
            (self.wins / effective_trades) * 100
            if effective_trades > 0 else 0.0
        )

        state = {
            "balance": round(float(self.balance), 2),

            "pnl": round(float(self.pnl), 2),

            "win_rate": round(win_rate, 1),

            "trades": int(self.trades),

            "wins": int(self.wins),

            "draws": int(self.draws),

            "losses": int(losses),

            "bot_status": status,

            "last_signal": self.last_signal,

            "last_result": self.last_result,

            "last_confidence": getattr(
                self,
                "last_confidence",
                0.0
            ),

            "consecutive_losses": int(
                self.consecutive_losses
            ),

            "daily_loss": round(
                float(self.daily_loss),
                2
            ),

            "mode": self.cfg["mode"],

            "symbol": self.symbol,

            "session_started_at": self.session_started_at,

            "session_id": self.session_id,

            "dataset_rows": getattr(
                self,
                "dataset_rows",
                0
            ),

            "model_loaded": getattr(
                self,
                "model_loaded",
                False
            ),

            "trade_duration_label": (
                f"S{self.cfg['trade_duration_sec']}"
                if self.cfg["trade_duration_sec"] < 60
                else (
                    f"M{int(self.cfg['trade_duration_sec'] / 60)}"
                    if self.cfg["trade_duration_sec"] < 3600
                    else f"H{int(self.cfg['trade_duration_sec'] / 3600)}"
                )
            ),

            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        write_state(state)
    # ----- WS handling -----
    def _attach_ws(self, page: Page) -> None:
        def on_websocket(ws):
            log(f"WS opened: {ws.url[:80]}")
            pending: list[str] = []   # PER-CONNECTION queue

            def on_frame(payload):
                try:
                    self._ws_frames_seen += 1
                    if isinstance(payload, (bytes, bytearray)):
                        try:
                            text = payload.decode("utf-8")
                        except UnicodeDecodeError:
                            return
                    else:
                        text = payload
                    text = text.strip()
                    if not text:
                        return
                    # Socket.IO event header: 42["label",...]
                    if text.startswith("42"):
                        try:
                            arr = json.loads(text[2:])
                        except json.JSONDecodeError:
                            return
                        if isinstance(arr, list) and arr and isinstance(arr[0], str):
                            label = arr[0]
                            if label not in self._ws_labels_seen:
                                self._ws_labels_seen.add(label)
                                log(f"WS new label: {label!r} sample={str(arr)[:160]}")
                            payload_data = arr[1] if len(arr) > 1 else None
                            self._dispatch(label, payload_data)
                            return
                    # Plain JSON frames — try labelled queue first, then
                    # treat as raw tick array as a fallback.
                    if text.startswith("[") or text.startswith("{"):
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError:
                            return
                        if pending:
                            label = pending.pop(0)
                            self._dispatch(label, data)
                            return
                        # Fallback: PO sometimes streams ticks as a raw
                        # array [[symbol, ts, price], ...] or a single
                        # [symbol, ts, price] without an explicit label.
                        self._try_raw_tick(data)
                        return
                    # bare label
                    if text.isascii() and text.replace("_", "").isalnum():
                        pending.append(text)
                except Exception as e:
                    log(f"WS frame parse error: {e}")

            ws.on("framereceived", on_frame)
        page.on("websocket", on_websocket)

    def _try_raw_tick(self, data: Any) -> None:
        """Accept ticks shaped as [sym, ts, price] or [[sym, ts, price], ...]."""
        if not isinstance(data, list) or not data:
            return
        # Single tick
        if (len(data) == 3 and isinstance(data[0], str)
                and isinstance(data[1], (int, float))
                and isinstance(data[2], (int, float))):
            sym, ts, price = data[0], float(data[1]), float(data[2])
            self.symbol = sym
            self.collector.add_tick(sym, ts, price)
            return
        # List of ticks
        if isinstance(data[0], list):
            for tick in data:
                if (isinstance(tick, list) and len(tick) >= 3
                        and isinstance(tick[0], str)
                        and isinstance(tick[1], (int, float))
                        and isinstance(tick[2], (int, float))):
                    sym, ts, price = tick[0], float(tick[1]), float(tick[2])
                    self.symbol = sym
                    self.collector.add_tick(sym, ts, price)

    def _dispatch(self, label: str, data: Any) -> None:
        # Tick streams — PO uses several label names over time
        if label in ("updateStream", "updateAssets", "loadHistoryPeriod",
                     "instruments/update", "chart/tick", "stream"):
            self._try_raw_tick(data if isinstance(data, list) else [])
        elif label == "updateClosedDeals" and isinstance(data, list):
            for deal in data:
                if isinstance(deal, dict):
                    self._closed_deals.append(deal)
        elif label in ("successupdateBalance", "updateBalance"):
            try:
                if isinstance(data, dict) and "balance" in data:
                    self.balance = float(data["balance"])
                elif isinstance(data, (int, float)):
                    self.balance = float(data)
                log(f"BALANCE UPDATE => {self.balance:.2f}")
            except Exception:
                pass
        elif label == "updateCharts":
            return
        else:
            # Heuristic: if the payload looks like ticks, ingest it anyway
            if isinstance(data, list):
                self._try_raw_tick(data)

    # ----- DOM helpers -----
    # Pocket Option A/B-tests the trading panel. Keep selectors scoped to the
    # exact Amount and Expiration containers; never use `.value__val` globally.
    _AMOUNT_SELECTORS = [
        ".block--bet-amount .value input",
        ".block--bet-amount input.value__val",
        ".block--bet-amount input",
        ".section-deal__investment input",
        ".section-deal__investments input",
        'input[data-test="trading-panel-amount"]',
        'xpath=(//*[contains(normalize-space(), "Amount")]/following::input)[1]',
    ]

    _EXPIRATION_CONTROL_SELECTORS = [
        ".block--expiration-inputs .control",
        ".block--expire-inputs .control",
        ".block--expire-time .control",
        ".section-deal__expiration .control",
        'xpath=(//*[contains(normalize-space(), "Time") or contains(normalize-space(), "Expiration")]/following::*[contains(@class,"control")])[1]',
    ]

    _TIME_INPUT_SELECTORS = [
        ".block--expiration-inputs input.value__val",
        ".block--expiration-inputs input",
        ".block--expire-inputs input.value__val",
        ".block--expire-inputs input",
        ".block--expire-time input",
        ".section-deal__expiration input",
        'input[data-test="trading-panel-expiration"]',
    ]

    _DURATION_LABELS = {
        3: ["S3", "53", "3 sec", "3 second", "00:00:03"],
        15: ["S15", "15S", "15 sec", "15 second", "00:00:15"],
        30: ["S30", "30S", "30 sec", "30 second", "00:00:30"],
        60: ["M1", "1M", "1 min", "1 minute", "00:01:00"],
        180: ["M3", "3M", "3 min", "3 minute", "00:03:00"],
        300: ["M5", "5M", "5 min", "5 minute", "00:05:00"],
        1800: ["M30", "30M", "30 min", "30 minute", "00:30:00"],
        3600: ["H1", "60M", "60 min", "60 minute", "01:00:00"],
    }

    @staticmethod
    def _norm_text(value: Any) -> str:
        return "".join(ch for ch in str(value or "").upper() if ch.isalnum())

    @staticmethod
    def normalize_expiry(label: str) -> int:

        import re

        l = label.lower().strip()

        if "sec" in l:
            return int(re.findall(r"\d+", l)[0])

        if "min" in l:
            return int(re.findall(r"\d+", l)[0]) * 60

        if l.startswith("s"):
            return int(re.findall(r"\d+", l)[0])

        if l.startswith("m"):
            return int(re.findall(r"\d+", l)[0]) * 60

        return 0

    @staticmethod
    def _amount_number(value: Any) -> Optional[float]:
        raw = str(value or "")
        cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in ".,-")
        cleaned = cleaned.replace(",", "")
        if cleaned in ("", ".", "-", "-"):
            return None
        try:
            return float(cleaned)
        except Exception:
            return None

    def _duration_labels(self, duration: int) -> list[str]:
        return self._DURATION_LABELS.get(int(duration), ["M1", "1 min", "00:01:00"])

    async def _first_visible_locator(self, selectors: list[str]):
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return sel, loc
            except Exception:
                continue
        return None, None

    async def _clear_and_type(self, loc, value: str) -> None:
        await loc.scroll_into_view_if_needed(timeout=3000)
        await loc.click(timeout=3000)
        try:
            await loc.fill("", timeout=1500)
        except Exception:
            pass
        try:
            if (await loc.input_value(timeout=1000)).strip():
                await self._page.keyboard.press("Meta+A")
                await self._page.keyboard.press("Delete")
        except Exception:
            pass
        try:
            if (await loc.input_value(timeout=1000)).strip():
                await self._page.keyboard.press("Control+A")
                await self._page.keyboard.press("Backspace")
        except Exception:
            pass
        await loc.type(str(value), delay=110)
        await self._page.keyboard.press("Tab")
        await asyncio.sleep(0.35)

    async def _set_amount(self, amount: float) -> tuple[bool, float]:
        sel, loc = await self._first_visible_locator(self._AMOUNT_SELECTORS)
        if not loc:
            log("Amount input not found")
            return False, float(amount)

        wanted_text = f"{int(amount)}" if float(amount).is_integer() else str(amount)
        for attempt in range(1, 4):
            try:
                await self._clear_and_type(loc, wanted_text)
                got_raw = (await loc.input_value(timeout=2000) or "").strip()
                got_num = self._amount_number(got_raw)
                if got_num is not None and abs(got_num - float(amount)) <= 0.01:
                    log(f"Amount verified: {got_raw} via {sel}")
                    return True, got_num
                log(f"Amount mismatch attempt {attempt}: wanted={wanted_text} got={got_raw}")
            except Exception as e:
                log(f"Amount set attempt {attempt} error: {e}")
            await asyncio.sleep(0.25)
        return False, float(amount)

    async def _select_duration_from_popup(self, labels: list[str]) -> bool:
        acceptable = {self._norm_text(x) for x in labels}
        _, control = await self._first_visible_locator(self._EXPIRATION_CONTROL_SELECTORS)
        if control:
            try:
                await control.click(timeout=3000)
                await asyncio.sleep(0.4)
            except Exception:
                pass

        item_selectors = [
            ".dops__timeframes-item",
            ".timeframes-item",
            "button",
            '[role="option"]',
        ]
        for item_sel in item_selectors:
            try:
                items = self._page.locator(item_sel)
                count = min(await items.count(), 80)
                for i in range(count):
                    item = items.nth(i)
                    if not await item.is_visible():
                        continue
                    txt = (
                        await item.inner_text(timeout=1000)
                        or ""
                    ).strip()

                    txt_seconds = self.normalize_expiry(txt)

                    target_seconds = [
                        self.normalize_expiry(x)
                        for x in labels
                    ]

                    if txt_seconds in target_seconds:

                        await item.click(timeout=3000)

                        await asyncio.sleep(0.35)

                        log(
                            f"Duration selected from popup: "
                            f"{txt}"
                        )

                        return True
            except Exception:
                continue
        return False

    async def _set_duration(self, duration: int) -> tuple[bool, str]:
        labels = self._duration_labels(duration)
        target = labels[0]

        if await self._select_duration_from_popup(labels):
            return True, target

        sel, loc = await self._first_visible_locator(self._TIME_INPUT_SELECTORS)
        if not loc:
            log(f"Duration control not found for {target}")
            return False, target

        acceptable = {self._norm_text(x) for x in labels}
        for attempt in range(1, 3):
            try:
                await self._clear_and_type(loc, target)
                got = (await loc.input_value(timeout=2000) or "").strip()
                n_got = self._norm_text(got)
                if n_got in acceptable or any(a and a in n_got for a in acceptable):
                    log(f"Duration verified: {got} via {sel}")
                    return True, target
                log(f"Duration mismatch attempt {attempt}: wanted={target} got={got}")
            except Exception as e:
                log(f"Duration set attempt {attempt} error: {e}")
        return False, target

    async def _read_balance(self) -> Optional[float]:
        selectors = [
            ".js-balance-demo",
            ".js-balance-real",
            ".balance-info-block__data",
            '[data-test="balance"]',
            'xpath=(//*[contains(@class,"balance") and contains(text(),"$")])[1]',
        ]
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                if not await loc.count() or not await loc.is_visible():
                    continue
                text = (await loc.inner_text(timeout=1500) or "").strip()
                num = self._amount_number(text)
                if num is not None:
                    self.balance = float(num)
                    log(f"Balance read: {self.balance:.2f}")
                    return self.balance
            except Exception:
                continue
        return None

    # ----- Trading -----
    async def _ensure_mode_url(self) -> None:
        wanted = DEMO_URL if self.cfg["mode"] == "DEMO" else LIVE_URL
        if self._page.url.rstrip("/") != wanted.rstrip("/"):
            log(f"URL mismatch, navigating to {self.cfg['mode']}")
            try:
                await self._page.goto(wanted, wait_until="networkidle", timeout=60_000)
            except Exception as e:
                log(f"nav error: {e}")

    async def _set_trade_params(self, amount: float, duration: int) -> tuple[bool, float, str]:
        try:
            amount_ok, actual_amount = await self._set_amount(float(amount))
            if not amount_ok:
                return False, actual_amount, ""
            duration_label = ""
            duration_ok = True

            if self.cfg.get("duration_mode", "MANUAL") == "AUTO":
                duration_ok, duration_label = await self._set_duration(int(duration))

                if not duration_ok:
                    return False, actual_amount, duration_label
            else:
                duration_label = "MANUAL"
                log("Manual duration mode enabled")

            log(f"Trade params set Amount={actual_amount:g} Duration={duration_label}")
            return True, actual_amount, duration_label
        except Exception as e:
            log(f"_set_trade_params error: {e}")
            return False, float(amount), ""

    async def _click_direction(self, direction: str) -> None:
            sel = ".btn-call" if direction == "BUY" else ".btn-put"
            log(f"CLICKED {direction}")
            await self._page.click(sel, timeout=5000)

    async def _execute_trade(
            self,
            direction: str,
            confidence: float,
            df: pd.DataFrame,
        ) -> bool:

        if self._trade_in_progress:
            log("Trade already in progress, skipping")
            return False

        cfg = self.cfg

        if not self._can_trade():
            log("BLOCKED by risk limits")
            return False

        self._trade_in_progress = True

        try:
            await self._read_balance()

            balance_before = float(self.balance)

            ok, actual_amount, duration_label = await self._set_trade_params(
                cfg["trade_amount"],
                cfg["trade_duration_sec"]
            )

            if not ok:
                log("Skip trade: could not verify params")
                return False

            if abs(float(actual_amount) - float(cfg["trade_amount"])) > 0.01:
                log(
                    f"WARNING: configured amount={cfg['trade_amount']} "
                    f"but broker field shows {actual_amount}"
                )

            shot = SCREENSHOT_DIR / f"{int(time.time())}_{direction}.png"

            try:
                await self._page.screenshot(path=str(shot))
            except Exception:
                pass

            self._closed_deals.clear()

            await self._click_direction(direction)

            self.last_trade_ts = time.time()

            wait_buffer = 6 if cfg["trade_duration_sec"] <= 30 else 10

            wait_time = cfg["trade_duration_sec"] + wait_buffer

            await asyncio.sleep(wait_time)

            result, pnl = await self._wait_for_trade_result(
                direction=direction,
                amount=actual_amount,
                timeout=cfg["trade_duration_sec"] + 10,
                balance_before=balance_before,
            )

            await asyncio.sleep(0.8)

            await self._read_balance()

            # ---------------------------------------------------
            # Use broker balance as source of truth
            # ---------------------------------------------------

            balance_after = float(self.balance)

            balance_delta = round(balance_after - balance_before, 2)

            if abs(balance_delta) > 0.01:

                if abs(balance_delta - pnl) > 0.01:
                    log(
                        f"PnL corrected from balance delta: "
                        f"deal_pnl={pnl:+.2f} "
                        f"balance_delta={balance_delta:+.2f}"
                    )

                pnl = balance_delta

                if pnl > 0:
                    result = "WIN"

                elif pnl < 0:
                    result = "LOSS"

                else:
                    result = "DRAW"

            # ---------------------------------------------------
            # Stats
            # ---------------------------------------------------

            self.trades += 1

            # IMPORTANT FIX
            self.pnl = round(
                float(self.balance) - float(self.start_balance),
                2
            )

            if result == "WIN":

                self.wins += 1
                self.consecutive_losses = 0

            elif result == "LOSS":

                self.consecutive_losses += 1
                self.daily_loss = abs(
                    min(0, float(self.pnl))
                )

            elif result == "DRAW":
                self.draws += 1
                log("Draw trade recorded")
            self.last_signal = direction
            self.last_result = result

            if result == "WIN":
                color = "WIN"
            elif result == "DRAW":
                color = "DRAW"
            else:
                color = "LOSS"

            log(f"Trade Result: {color} pnl={pnl:+.2f}")

            log(
                f"SESSION => "
                f"wins={self.wins} "
                f"losses={self.trades - self.wins - self.draws} "
                f"session_pnl={self.pnl:+.2f}"
            )

            db_insert_trade({
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": self.session_id,
                "symbol": self.symbol,
                "direction": direction,
                "amount": actual_amount,
                "duration": cfg["trade_duration_sec"],
                "confidence": confidence,
                "result": result,
                "pnl": pnl,
                "balance_after": self.balance,
                "mode": cfg["mode"],
            })

            latest = df.iloc[-1]

            append_training_row({

                "timestamp": latest["t"],

                "open": latest["open"],
                "high": latest["high"],
                "low": latest["low"],
                "close": latest["close"],
                "volume": latest.get("volume", 0),

                "rsi": latest.get("rsi"),
                "ema_fast": latest.get("ema_fast"),
                "ema_slow": latest.get("ema_slow"),

                "macd": latest.get("macd"),
                "macd_signal": latest.get("macd_signal"),

                "signal": direction,
                "confidence": confidence,

                "executed": True,
                "rejection_reason": "",

                "result": result,
                "pnl": pnl,

                "future_close": latest["close"],
                "future_move": 0,

                "candle_size": (
                    latest["high"] - latest["low"]
                ),

                "body_size": abs(
                    latest["close"] - latest["open"]
                ),

                "atr": latest.get("atr"),

                "ema_distance": latest.get(
                    "ema_distance"
                ),

                "upper_wick_ratio": latest.get(
                    "upper_wick_ratio"
                ),

                "lower_wick_ratio": latest.get(
                    "lower_wick_ratio"
                ),

                "momentum_accel": latest.get(
                    "momentum_accel"
                ),
            })

            self._push_state("RUNNING")

            return True

        except Exception as e:

            log(f"_execute_trade error: {e}")

            return False

        finally:

            self._trade_in_progress = False    

    async def _wait_for_trade_result(
            self,
            direction: str,
            amount: float,
            timeout: int = 30,
            balance_before: Optional[float] = None,
        ) -> tuple[str, float]:

        start = time.time()

        seen = set()

        while time.time() - start < timeout:

            try:

                while self._closed_deals:

                    deal = self._closed_deals.pop(0)

                    deal_id = str(
                        deal.get("id")
                        or deal.get("deal_id")
                        or ""
                    )

                    if deal_id in seen:
                        continue

                    seen.add(deal_id)

                    profit = float(deal.get("profit", 0) or 0)
                    invested = float(deal.get("amount", amount) or amount)

                    if abs(invested - amount) > 0.01:
                        log(
                            f"Closed deal amount differs from verified field: "
                            f"verified={amount:g} deal={invested:g}; using deal amount for PnL"
                        )

                    # Best source of PnL is the real balance delta because PO
                    # may report either net profit or gross payout depending on
                    # the event shape. Fall back to deal fields if balance has
                    # not updated yet.
                    if balance_before is not None:
                        await asyncio.sleep(0.8)
                        if await self._read_balance() is not None:
                            delta = round(float(self.balance) - float(balance_before), 2)
                            if abs(delta) > 0.01:
                                result = "WIN" if delta > 0 else "LOSS"
                                log(f"Resolved Trade from balance delta {result} pnl={delta:+.2f}")
                                return result, delta

                    win_field = str(deal.get("win", deal.get("result", ""))).lower()
                    is_draw = (
                        abs(profit) < 0.01
                        or abs(profit - invested) < 0.01
                    )

                    is_win = (
                        not is_draw
                        and (
                            profit > 0
                            or win_field in (
                                "win",
                                "won",
                                "true",
                                "1",
                                "success",
                            )
                        )
                    )
                    if is_draw:

                        result = "DRAW"
                        pnl = 0.0

                    elif is_win:

                        result = "WIN"

                        pnl = (
                            profit - invested
                            if profit > invested
                            else profit
                        )

                        if pnl <= 0:
                            pnl = invested * 0.8

                        pnl = round(pnl, 2)

                    else:

                        result = "LOSS"
                        pnl = -round(invested, 2)

                    log(f"Resolved Trade {result} pnl={pnl:+.2f}")

                    return result, pnl

            except Exception as e:

                log(
                    f"_wait_for_trade_result error: {e}"
                )

            await asyncio.sleep(1)

        if balance_before is not None and await self._read_balance() is not None:
            delta = round(
                float(self.balance) - float(balance_before),
                2
            )
            if abs(delta) > 0.01:
                result = "WIN" if delta > 0 else "LOSS"
                log(
                    f"Trade result inferred from balance delta "
                    f"{result} pnl={delta:+.2f}"
                )
                return result, delta

            # no balance movement = unresolved/draw
            log("Trade result unresolved, marking as DRAW")
            return "DRAW", 0.0
        log("Trade result timeout")
        return "DRAW", 0.0

    def _can_trade(self) -> bool:
        cfg = self.cfg
        if cfg["mode"] == "DEMO" and cfg.get("bypass_risk_in_demo"):
            return True
        if self.consecutive_losses >= cfg["max_consecutive_losses"]:
            log(f"Too many consecutive losses ({self.consecutive_losses})")
            return False
        if self.daily_loss >= cfg["max_daily_loss"]:
            log(f"Daily loss limit hit (${self.daily_loss:.2f})")
            return False
        return True

    async def strategy_cycle(self) -> None:
        self.cfg = read_config()
        new_interval = int(self.cfg.get("candle_interval_sec", self.collector.interval))
        if new_interval != self.collector.interval:
            log(f"Candle interval updated: {self.collector.interval}s -> {new_interval}s")
            self.collector.interval = new_interval

        await self._ensure_mode_url()

        df = self.collector.dataframe()

        min_c = self.cfg["min_candles_required"]

        if len(df) < min_c:
            log(
                f"Warming up: {len(df)}/{min_c} candles "
                f"(ws_frames={self._ws_frames_seen}, "
                f"labels={sorted(self._ws_labels_seen)[:8]})"
            )
            self._push_state("WARMUP")
            await self._read_balance()
            return

        # IMPORTANT FIX
        if len(df) < 35:
            log(f"Waiting indicator warmup {len(df)}/35")
            return

        df = compute_indicators(df)

        direction, conf = vote_signal(df)

        latest = df.iloc[-1]

        if direction == "NONE":

            if self.last_saved_ts == latest["t"]:
                return

            self.last_saved_ts = latest["t"]

            append_training_row({

                "timestamp": latest["t"],

                "open": latest["open"],
                "high": latest["high"],
                "low": latest["low"],
                "close": latest["close"],
                "volume": latest.get("volume", 0),

                "rsi": latest.get("rsi"),
                "ema_fast": latest.get("ema_fast"),
                "ema_slow": latest.get("ema_slow"),

                "macd": latest.get("macd"),
                "macd_signal": latest.get("macd_signal"),

                "signal": "NONE",
                "confidence": conf,

                "executed": False,
                "rejection_reason": "NO_SIGNAL",

                "result": "",
                "pnl": 0,

                "future_close": latest["close"],
                "future_move": 0,
                "candle_size": latest["high"] - latest["low"],
                "body_size": abs(latest["close"] - latest["open"]),

                "atr": latest.get("atr"),
                "ema_distance": latest.get("ema_distance"),

                "upper_wick_ratio": latest.get("upper_wick_ratio"),
                "lower_wick_ratio": latest.get("lower_wick_ratio"),

                "momentum_accel": latest.get("momentum_accel"),
            })

            self._push_state("RUNNING")

            return True

        self.last_confidence = conf

        # avoid flat RSI chop market
        rsi = latest.get("rsi", 50)

        if 48 <= rsi <= 52:
            log("Skipping flat RSI market")
            return

        # sideways market filter
        recent_range = (
            df["high"].tail(8).max() -
            df["low"].tail(8).min()
        )
        if recent_range < 0.00020:
            log(
                f"Skipping sideways market "
                f"(range={recent_range:.6f})"
            )
            return

        # candle exhaustion filter
        candle_size = max(
            latest.get("candle_size", 0),
            0.000001
        )

        body_size = latest.get("body_size", 0)

        body_ratio = body_size / candle_size

        if body_ratio > 0.92:

            log(
                f"Skipping exhaustion candle "
                f"(body_ratio={body_ratio:.2f})"
            )

            return

        prev_close = df.iloc[-2]["close"]
        close = latest["close"]
        move_size = abs(close - prev_close)
        if move_size > 0.00024:
            log(
                f"Skipping overextended move "
                f"(move={move_size:.6f})"
            )
            return
        accel = latest.get(
            "momentum_accel",
            0
        )

        # allow small pullbacks, block only strong reversals

        if direction == "BUY" and accel < -0.00015:

            log(
                "BUY blocked by strong bearish acceleration "
                f"(accel={accel:.6f})"
            )

            return

        if direction == "SELL" and accel > 0.00015:

            log(
                "SELL blocked by strong bullish acceleration "
                f"(accel={accel:.6f})"
            )

            return

        log(f"Signal: {direction} conf={conf:.2f}")
        
        self.last_signal = direction

        self._push_state("RUNNING")

       
        if conf < self.cfg["confidence_threshold"]:
            append_training_row({

                "timestamp": latest["t"],

                "open": latest["open"],
                "high": latest["high"],
                "low": latest["low"],
                "close": latest["close"],
                "volume": latest.get("volume", 0),

                "rsi": latest.get("rsi"),
                "ema_fast": latest.get("ema_fast"),
                "ema_slow": latest.get("ema_slow"),

                "macd": latest.get("macd"),
                "macd_signal": latest.get("macd_signal"),

                "signal": direction,
                "confidence": conf,

                "executed": False,
                "rejection_reason": "LOW_CONFIDENCE",

                "result": "",
                "pnl": 0,

                "future_close": latest["close"],
                "future_move": 0,
                "candle_size": latest["high"] - latest["low"],
                "body_size": abs(latest["close"] - latest["open"]),

                "atr": latest.get("atr"),
                "ema_distance": latest.get("ema_distance"),

                "upper_wick_ratio": latest.get("upper_wick_ratio"),
                "lower_wick_ratio": latest.get("lower_wick_ratio"),

                "momentum_accel": latest.get("momentum_accel"),
            })

            log(
                f"Conf {conf:.2f} below "
                f"threshold {self.cfg['confidence_threshold']}"
            )
            return

        latest_candle_ts = df.iloc[-1]["t"]

        if self._cooldown_active():
            append_training_row({

                "timestamp": latest["t"],

                "open": latest["open"],
                "high": latest["high"],
                "low": latest["low"],
                "close": latest["close"],
                "volume": latest.get("volume", 0),

                "rsi": latest.get("rsi"),
                "ema_fast": latest.get("ema_fast"),
                "ema_slow": latest.get("ema_slow"),

                "macd": latest.get("macd"),
                "macd_signal": latest.get("macd_signal"),

                "signal": direction,
                "confidence": conf,

                "executed": False,
                "rejection_reason": "COOLDOWN",

                "result": "",
                "pnl": 0,

                "future_close": latest["close"],
                "future_move": 0,
                "candle_size": latest["high"] - latest["low"],
                "body_size": abs(latest["close"] - latest["open"]),

                "atr": latest.get("atr"),
                "ema_distance": latest.get("ema_distance"),

                "upper_wick_ratio": latest.get("upper_wick_ratio"),
                "lower_wick_ratio": latest.get("lower_wick_ratio"),

                "momentum_accel": latest.get("momentum_accel"),
            })
            log("Cooldown active")
            return

        if self.last_trade_candle_ts == latest_candle_ts:
            log("Already traded this candle")
            return

        ema_distance = latest.get(
            "ema_distance",
            0
        )

        if abs(ema_distance) < 0.000015:

            log(
                "Skipping weak trend "
                f"(ema_distance={ema_distance:.6f})"
            )

            return

        label, seconds = choose_expiry(
            conf,
            latest.get("atr", 0),
            latest.get("ema_distance", 0),
        )

        if not seconds:
            log("No valid expiry selected")
            return

        self.cfg["trade_duration_sec"] = seconds

        log(
            f"Dynamic expiry selected "
            f"{label} ({seconds}s)"
        )
        await self._page.keyboard.press("Escape")
        await asyncio.sleep(0.2)

        ok = await self._execute_trade(direction, conf, df)

        if ok:
            self.last_trade_candle_ts = latest_candle_ts
            
    # ----- Entry -----
    async def run(self) -> None:
        db_init()
        self._push_state("STARTING")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._ctx = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
            )
            await self._ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            self._page = await self._ctx.new_page()
            self._attach_ws(self._page)

            start_url = DEMO_URL if self.cfg["mode"] == "DEMO" else LIVE_URL
            log(f"Opening {start_url}")
            await self._page.goto(start_url, wait_until="domcontentloaded")
            log("Waiting for login (up to 180s)...")
            try:
                await self._page.wait_for_url(
                    lambda url: "cabinet" in url, timeout=180_000
                )
            except Exception:
                log("Login wait timed out; continuing anyway")
            try:
                await self._page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
            await self._ensure_mode_url()
            await self._read_balance()
            if self.start_balance == 0:
                self.start_balance = float(self.balance)
                log(f"Session start balance: {self.start_balance}")

            try:
                dataset_path = ROOT / "storage" / "datasets" / "training_data.csv"

                if dataset_path.exists():
                    import pandas as pd
                    self.dataset_rows = len(pd.read_csv(dataset_path))

            except Exception as e:
                log(f"Dataset load error: {e}")

            try:
                model_path = ROOT / "ml" / "models" / "xgb_model.pkl"

                self.model_loaded = model_path.exists()

            except Exception:
                self.model_loaded = False
            BOT_STATE["mode"] = "DEMO" if "demo" in self._page.url else "LIVE"
            self._push_state("RUNNING")

            log("Bot running. Ctrl+C to stop.")
            while True:
                try:
                    await self.strategy_cycle()
                except Exception as e:
                    log(f"strategy_cycle error: {e}")
                    traceback.print_exc()
                await asyncio.sleep(self.cfg["candle_interval_sec"])


async def main() -> None:
    bot = PocketOptionBot()
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Stopped by user")
        sys.exit(0)
