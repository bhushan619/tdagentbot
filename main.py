"""Pocket Option AI Trading Bot.

Playwright-driven, headed Chromium. Listens to PO's binary Socket.IO
frames at the Python level (per-connection pending queue), builds 15s
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
from typing import Any, Optional

import pandas as pd

# pandas_ta 0.3.14 still calls Series.append/DataFrame.append, which were
# removed in pandas 2.x.
if not hasattr(pd.Series, "append"):

    def _series_append(self, to_append, ignore_index=False, verify_integrity=False):
        return pd.concat(
            [self, to_append],
            ignore_index=ignore_index,
            verify_integrity=verify_integrity,
        )

    pd.Series.append = _series_append

if not hasattr(pd.DataFrame, "append"):

    def _dataframe_append(
        self, other, ignore_index=False, verify_integrity=False, sort=False
    ):
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
    BOT_STATE,
    read_config,
    write_state,
    ROOT,
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
    conn.execute(
        """
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
            profit_percent REAL,
            balance_after REAL,
            mode TEXT
        )
    """
    )
    conn.commit()
    conn.close()


def db_insert_trade(row: dict) -> None:
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """INSERT INTO trades
           (ts, session_id, symbol, direction, amount, duration,
            confidence, result, pnl, profit_percent, balance_after, mode)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["ts"],
            row["session_id"],
            row["symbol"],
            row["direction"],
            row["amount"],
            row["duration"],
            row["confidence"],
            row["result"],
            row["pnl"],
            row["profit_percent"],
            row["balance_after"],
            row["mode"],
        ),
    )
    conn.commit()
    conn.close()


# ---------- Market data ----------
class MarketDataCollector:
    """Builds N-second OHLCV candles from raw ticks."""

    def __init__(self, interval_sec: int = 15, maxlen: int = 500) -> None:
        self.interval = interval_sec
        self.candles: deque[dict] = deque(maxlen=maxlen)
        self._current: Optional[dict] = None

    def add_tick(self, symbol: str, ts: float, price: float) -> None:
        bucket = int(ts // self.interval) * self.interval
        if self._current is None or self._current["t"] != bucket:
            if self._current is not None:
                self.candles.append(self._current)
                log(
                    f"New Candle {symbol} O={self._current['o']:.5f} "
                    f"H={self._current['h']:.5f} L={self._current['l']:.5f} "
                    f"C={self._current['c']:.5f}"
                )
            self._current = {
                "t": bucket,
                "symbol": symbol,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
                "v": 1,
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
        df = df.rename(
            columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
        )
        return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()

    # Core trend
    df["ema_fast"] = ta.ema(df["close"], length=9)
    df["ema_slow"] = ta.ema(df["close"], length=21)
    df["ema_mid"] = ta.ema(df["close"], length=13)  # extra confirmation layer

    # Momentum
    df["rsi"] = ta.rsi(df["close"], length=14)

    # Stochastic (new) — catches momentum shifts the RSI misses
    stoch = ta.stoch(df["high"], df["low"], df["close"], k=5, d=3, smooth_k=3)
    if stoch is not None and not stoch.empty:
        df["stoch_k"] = stoch.iloc[:, 0]
        df["stoch_d"] = stoch.iloc[:, 1]

    # MACD
    macd = ta.macd(df["close"])
    if macd is not None and not macd.empty:
        df["macd"] = macd.iloc[:, 0]
        df["macd_signal"] = macd.iloc[:, 2]
        df["macd_hist"] = macd.iloc[:, 1]  # histogram — direction of momentum change

    # Bollinger Bands
    bb = ta.bbands(df["close"], length=20)
    if bb is not None and not bb.empty:
        df["bb_lower"] = bb.iloc[:, 0]
        df["bb_upper"] = bb.iloc[:, 2]
        bb_width = (df["bb_upper"] - df["bb_lower"]).replace(0, 1e-9)
        df["bb_pct"] = (df["close"] - df["bb_lower"]) / bb_width  # 0=lower, 1=upper
        df["bb_squeeze"] = bb_width / df["close"]  # low = tight bands = breakout pending

    # Candle structure
    df["momentum"] = df["close"] - df["close"].shift(5)
    df["momentum_accel"] = df["momentum"] - df["momentum"].shift(1)
    df["candle_size"] = df["high"] - df["low"]
    df["body_size"] = (df["close"] - df["open"]).abs()

    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["atr"] = atr
    df["ema_distance"] = (df["ema_fast"] - df["ema_slow"]).abs()

    candle_range = (df["high"] - df["low"]).replace(0, 1e-9)
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    df["upper_wick_ratio"] = upper_wick / candle_range
    df["lower_wick_ratio"] = lower_wick / candle_range

    # Price-action consistency: count bullish/bearish candles in last 5
    df["bull_candles_5"] = (df["close"] > df["open"]).rolling(5).sum()
    df["bear_candles_5"] = (df["close"] < df["open"]).rolling(5).sum()

    return df


def vote_signal(df: pd.DataFrame) -> tuple[str, float]:
    """Adaptive confidence engine.

    Instead of a simple 3-indicator majority vote (which produced conf
    values stuck at 0.58-0.61 and missed clear trends), this engine:

    1. Scores each of 6 signal sources on a continuous 0-1 scale.
    2. Applies a regime-aware *weight* to each source — trend indicators
       get higher weight in trending conditions, momentum indicators
       dominate in volatile conditions.
    3. Requires directional AGREEMENT across sources before scoring high
       (disagreement caps confidence at 0.60 regardless of strength).
    4. Adds a price-action consistency bonus when candle structure aligns.

    This means a strong, clear trend (like the sustained SELL session seen
    in the logs) will produce conf 0.72-0.85 instead of 0.59-0.61, while
    choppy/ambiguous markets will still produce low scores and be skipped.
    """
    if df.empty or len(df) < 21:
        return "NONE", 0.0

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last

    # ── helper ──────────────────────────────────────────────────────────
    def _get(key, default=None):
        v = last.get(key)
        return default if (v is None or (isinstance(v, float) and pd.isna(v))) else v

    # ── 1. EMA TREND (weight 0.28) ───────────────────────────────────────
    # Measures trend direction AND separation strength.
    e9  = _get("ema_fast")
    e13 = _get("ema_mid")
    e21 = _get("ema_slow")
    ema_score = 0.0
    ema_dir = 0  # +1 bull / -1 bear
    if e9 is not None and e21 is not None:
        spread = (e9 - e21) / max(abs(e21), 1e-9)
        ema_dir = 1 if e9 > e21 else -1
        # Strength: spread of 0.0005 (5 pips) = 1.0; proportional below
        raw_strength = min(abs(spread) / 0.0005, 1.0)
        # Bonus: all three EMAs aligned (9>13>21 or 9<13<21)
        alignment_bonus = 0.0
        if e13 is not None:
            if ema_dir == 1 and e9 > e13 > e21:
                alignment_bonus = 0.15
            elif ema_dir == -1 and e9 < e13 < e21:
                alignment_bonus = 0.15
        ema_score = min(raw_strength + alignment_bonus, 1.0)
    ema_weight = 0.28

    # ── 2. RSI (weight 0.18) ─────────────────────────────────────────────
    # Not just direction but SLOPE — rising RSI in bull trend is key.
    rsi = _get("rsi", 50.0)
    prev_rsi = prev.get("rsi") or rsi
    rsi_dir = 0
    rsi_score = 0.0
    if rsi > 52:
        rsi_dir = 1
        # Distance from neutral + slope bonus
        rsi_score = min((rsi - 50) / 20.0, 1.0)
        if rsi > prev_rsi:
            rsi_score = min(rsi_score + 0.10, 1.0)
    elif rsi < 48:
        rsi_dir = -1
        rsi_score = min((50 - rsi) / 20.0, 1.0)
        if rsi < prev_rsi:
            rsi_score = min(rsi_score + 0.10, 1.0)
    rsi_weight = 0.18

    # ── 3. MACD (weight 0.20) ────────────────────────────────────────────
    # Uses histogram direction (momentum change) not just line crossover.
    macd_v   = _get("macd", 0.0)
    macd_sig = _get("macd_signal", 0.0)
    macd_hist = _get("macd_hist", 0.0)
    prev_hist = prev.get("macd_hist") or 0.0
    macd_dir = 0
    macd_score = 0.0
    if macd_v is not None and macd_sig is not None:
        macd_dir = 1 if macd_v > macd_sig else -1
        denom = max(abs(macd_v) + abs(macd_sig), 1e-9)
        separation = min(abs(macd_v - macd_sig) / denom, 1.0)
        # Histogram expanding in direction = strong momentum
        hist_expanding = (
            (macd_dir == 1 and macd_hist > 0 and macd_hist > prev_hist) or
            (macd_dir == -1 and macd_hist < 0 and macd_hist < prev_hist)
        )
        hist_bonus = 0.15 if hist_expanding else 0.0
        macd_score = min(separation + hist_bonus, 1.0)
    macd_weight = 0.20

    # ── 4. STOCHASTIC (weight 0.14) ──────────────────────────────────────
    # Detects momentum direction independent of trend magnitude.
    stoch_k = _get("stoch_k", 50.0)
    stoch_d = _get("stoch_d", 50.0)
    stoch_dir = 0
    stoch_score = 0.0
    if stoch_k is not None and stoch_d is not None:
        if stoch_k > 55 and stoch_k > stoch_d:
            stoch_dir = 1
            stoch_score = min((stoch_k - 50) / 50.0, 1.0)
        elif stoch_k < 45 and stoch_k < stoch_d:
            stoch_dir = -1
            stoch_score = min((50 - stoch_k) / 50.0, 1.0)
    stoch_weight = 0.14

    # ── 5. BOLLINGER BAND POSITION + SQUEEZE (weight 0.12) ───────────────
    # bb_pct: where price sits in the band (0=lower, 1=upper) → direction.
    # bb_squeeze: band width relative to price → how meaningful that position is.
    #   Tight squeeze (low value) = bands contracting = breakout imminent = amplify score.
    #   Wide bands (high value)   = price drifting in noise = dampen score.
    bb_pct     = _get("bb_pct", 0.5)
    bb_squeeze = _get("bb_squeeze")   # None if BB not computed yet
    bb_dir = 0
    bb_score = 0.0
    if bb_pct is not None:
        if bb_pct > 0.6:
            bb_dir = 1
            bb_score = min((bb_pct - 0.5) / 0.5, 1.0)
        elif bb_pct < 0.4:
            bb_dir = -1
            bb_score = min((0.5 - bb_pct) / 0.5, 1.0)
        # Squeeze multiplier: typical bb_squeeze on FX ≈ 0.0008–0.0025
        # Below 0.0010 = tight (breakout building) → multiply up to 1.3×
        # Above 0.0020 = wide (choppy/noisy)       → multiply down to 0.7×
        if bb_squeeze is not None and bb_score > 0:
            squeeze_mult = 1.0
            if bb_squeeze < 0.0010:
                squeeze_mult = 1.0 + 0.3 * (1.0 - bb_squeeze / 0.0010)
            elif bb_squeeze > 0.0020:
                squeeze_mult = max(0.7, 1.0 - (bb_squeeze - 0.0020) / 0.0020)
            bb_score = min(bb_score * squeeze_mult, 1.0)
    bb_weight = 0.12

    # ── 6. PRICE-ACTION CONSISTENCY (weight 0.08) ────────────────────────
    # Candle body count in recent bars — pure price structure.
    bull_c = _get("bull_candles_5", 2.5)
    bear_c = _get("bear_candles_5", 2.5)
    pa_dir = 0
    pa_score = 0.0
    if bull_c is not None and bear_c is not None:
        if bull_c >= 4:
            pa_dir = 1
            pa_score = (bull_c - 2.5) / 2.5
        elif bear_c >= 4:
            pa_dir = -1
            pa_score = (bear_c - 2.5) / 2.5
    pa_weight = 0.08

    # ── DIRECTION VOTE ───────────────────────────────────────────────────
    # Each source casts a weighted directional vote.
    sources = [
        (ema_dir,   ema_score,   ema_weight,   "EMA"),
        (rsi_dir,   rsi_score,   rsi_weight,   "RSI"),
        (macd_dir,  macd_score,  macd_weight,  "MACD"),
        (stoch_dir, stoch_score, stoch_weight, "STOCH"),
        (bb_dir,    bb_score,    bb_weight,    "BB"),
        (pa_dir,    pa_score,    pa_weight,    "PA"),
    ]

    bull_weight_total = sum(w * s for d, s, w, _ in sources if d > 0)
    bear_weight_total = sum(w * s for d, s, w, _ in sources if d < 0)
    total_weight = sum(w for _, _, w, _ in sources)

    if bull_weight_total == 0 and bear_weight_total == 0:
        return "NONE", 0.0

    # Direction is the winning side by weighted score
    if bull_weight_total > bear_weight_total:
        direction = "BUY"
        win_score = bull_weight_total
        lose_score = bear_weight_total
    else:
        direction = "SELL"
        win_score = bear_weight_total
        lose_score = bull_weight_total

    # ── AGREEMENT CHECK ──────────────────────────────────────────────────
    # Count sources that actively oppose the direction (score > 0)
    opposing = sum(
        1 for d, s, w, _ in sources
        if d != 0 and s > 0.1 and
        ((direction == "BUY" and d < 0) or (direction == "SELL" and d > 0))
    )
    # Strong opposition (2+ sources disagree) caps confidence
    if opposing >= 2:
        return direction, 0.58  # below threshold — will be filtered

    # ── CONFIDENCE CALCULATION ───────────────────────────────────────────
    # Base: weighted score of winning side, normalised to total weight
    base_conf = win_score / total_weight

    # Dominance bonus: how much stronger is the winning side vs losing?
    # A 2:1 win/lose ratio adds +0.05; clean sweep adds +0.12
    if lose_score > 0:
        dominance = min(win_score / lose_score, 4.0) / 4.0
    else:
        dominance = 1.0
    dominance_bonus = dominance * 0.12

    conf = min(base_conf + dominance_bonus, 1.0)

    # Require minimum direction vote: the three primary indicators
    # (EMA, RSI, MACD) must have at least 2 of 3 pointing the right way.
    primary_agree = sum(
        1 for d, s, w, name in sources
        if name in ("EMA", "RSI", "MACD") and
        ((direction == "BUY" and d > 0) or (direction == "SELL" and d < 0))
    )
    if primary_agree < 2:
        # Secondary indicators carry the signal alone — unreliable; cap low
        conf = min(conf, 0.60)

    return direction, round(min(conf, 1.0), 2)


# ============================================================
# MARKET REGIME DETECTION
# ============================================================


def detect_market_regime(atr, ema_distance, trend_bars, candle_size, cfg):

    rd = cfg.get("regime_detection", {})

    trending_ema_distance = rd.get("trending_ema_distance", 0.00005)
    trending_atr = rd.get("trending_atr", 0.00016)
    volatile_atr = rd.get("volatile_atr", 0.00024)
    volatile_candle_atr_multiplier = rd.get("volatile_candle_atr_multiplier", 2.2)
    ranging_ema_distance = rd.get("ranging_ema_distance", 0.000018)
    ranging_atr = rd.get("ranging_atr", 0.00011)
    exhausted_trend_bars = rd.get("exhausted_trend_bars", 7)

    if ema_distance >= trending_ema_distance and atr >= trending_atr:
        return "TRENDING"

    # High ATR alone always means volatile regardless of direction.
    # Spike candle (candle_size >> ATR) only signals VOLATILE when EMA distance
    # is weak — a big candle inside a directional market is noise, not a regime.
    if atr >= volatile_atr:
        return "VOLATILE"
    if candle_size >= (atr * volatile_candle_atr_multiplier) and ema_distance < trending_ema_distance:
        return "VOLATILE"

    if ema_distance <= ranging_ema_distance and atr <= ranging_atr:
        return "RANGING"

    if trend_bars >= exhausted_trend_bars:
        return "EXHAUSTED"

    return "NORMAL"


# ============================================================
# APPLY DYNAMIC CONFIG
# ============================================================


def apply_dynamic_config(regime, cfg):
    dynamic = dict(cfg)
    regimes = cfg.get("market_regimes", {})
    normal_cfg = regimes.get("NORMAL", {})
    regime_cfg = regimes.get(regime, normal_cfg)
    dynamic.update(regime_cfg)
    return dynamic


# ============================================================
# CHOOSE EXPIRY
# ============================================================


def choose_expiry(conf, atr, ema_distance, cfg, regime="NORMAL"):
    """Select trade expiry duration based on confidence and regime.

    S15 for high-confidence signals (>= s15_conf, default 0.68).
    S30 for standard signals (>= s30_conf, default 0.63).
    Falls back to S30 for any signal above the confidence threshold
    so valid signals are never silently discarded.
    """
    regimes = cfg.get("market_regimes", {})
    normal_cfg = regimes.get("NORMAL", {})
    regime_cfg = regimes.get(regime, normal_cfg)

    # Allow per-regime override, but use sensible new defaults
    s15_conf = regime_cfg.get("s15_conf", 0.68)
    s30_conf = regime_cfg.get("s30_conf", 0.63)
    min_conf = cfg.get("confidence_threshold", 0.62)
    preferred_expiry = regime_cfg.get("preferred_expiry", "S30")

    if preferred_expiry == "S15":
        if conf >= s15_conf:
            return "S15", 15
        if conf >= s30_conf:
            return "S30", 30
    else:
        if conf >= s15_conf:
            return "S15", 15
        if conf >= s30_conf:
            return "S30", 30

    # Fallback: any signal that passed the confidence threshold still trades
    # with the safer 30s expiry rather than being silently discarded.
    if conf >= min_conf:
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
        self.losses = 0
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

        self.last_trade_ts = 0
        self.last_trade_candle_ts = None
        self.last_saved_ts = None
        self.current_trend_trade_count = 0

        self.session_wins = 0
        self.session_losses = 0
        self.recent_trade_results = []

        self.last_trade_direction = None
        self.same_direction_count = 0

        self.hard_cooldown_candles = 0
        self._last_cooldown_candle = None

        self.pending_regime = None
        self.pending_regime_count = 0

        self._no_expiry_count = 0

        self.start_balance = 0.0

    # ----- cooldown active -----

    def _cooldown_active(self, cooldown: int = 10) -> bool:
        return (time.time() - self.last_trade_ts) < cooldown

    # ----- state sync -----
    def _push_state(self, status: str) -> None:
        losses = self.trades - self.wins - self.draws
        effective_trades = self.wins + losses
        win_rate = (self.wins / effective_trades) * 100 if effective_trades > 0 else 0.0

        state = {
            "balance": round(self.balance, 2),
            "pnl": round(float(self.pnl), 2),
            "win_rate": round(win_rate, 1),
            "trades": int(self.trades),
            "wins": int(self.wins),
            "draws": int(self.draws),
            "losses": int(losses),
            "bot_status": status,
            "last_signal": self.last_signal,
            "last_result": self.last_result,
            "last_confidence": getattr(self, "last_confidence", 0.0),
            "consecutive_losses": int(self.consecutive_losses),
            "daily_loss": round(float(self.daily_loss), 2),
            "mode": self.cfg["mode"],
            "symbol": self.symbol,
            "session_started_at": self.session_started_at,
            "session_id": self.session_id,
            "dataset_rows": getattr(self, "dataset_rows", 0),
            "model_loaded": getattr(self, "model_loaded", False),
            "trade_duration_label": (
                f"S{self.cfg['trade_duration_sec']}"
                if self.cfg["trade_duration_sec"] < 60
                else (
                    f"M{int(self.cfg['trade_duration_sec'] / 60)}"
                    if self.cfg["trade_duration_sec"] < 3600
                    else f"H{int(self.cfg['trade_duration_sec'] / 3600)}"
                )
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        write_state(state)

    # ----- WS handling -----
    def _attach_ws(self, page: Page) -> None:
        def on_websocket(ws):
            log(f"WS opened: {ws.url[:80]}")
            pending: list[str] = []

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
                    if text.startswith("[") or text.startswith("{"):
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError:
                            return
                        if pending:
                            label = pending.pop(0)
                            self._dispatch(label, data)
                            return
                        self._try_raw_tick(data)
                        return
                    if text.isascii() and text.replace("_", "").isalnum():
                        pending.append(text)
                except Exception as e:
                    log(f"WS frame parse error: {e}")

            ws.on("framereceived", on_frame)

        page.on("websocket", on_websocket)

    def _try_raw_tick(self, data: Any) -> None:
        if not isinstance(data, list) or not data:
            return
        if (
            len(data) == 3
            and isinstance(data[0], str)
            and isinstance(data[1], (int, float))
            and isinstance(data[2], (int, float))
        ):
            sym, ts, price = data[0], float(data[1]), float(data[2])
            self.symbol = sym
            self.collector.add_tick(sym, ts, price)
            return
        if isinstance(data[0], list):
            for tick in data:
                if (
                    isinstance(tick, list)
                    and len(tick) >= 3
                    and isinstance(tick[0], str)
                    and isinstance(tick[1], (int, float))
                    and isinstance(tick[2], (int, float))
                ):
                    sym, ts, price = tick[0], float(tick[1]), float(tick[2])
                    self.symbol = sym
                    self.collector.add_tick(sym, ts, price)

    def _dispatch(self, label: str, data: Any) -> None:
        if label in (
            "updateStream",
            "updateAssets",
            "loadHistoryPeriod",
            "instruments/update",
            "chart/tick",
            "stream",
        ):
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
            if isinstance(data, list):
                self._try_raw_tick(data)

    # ----- DOM helpers -----
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
                log(
                    f"Amount mismatch attempt {attempt}: wanted={wanted_text} got={got_raw}"
                )
            except Exception as e:
                log(f"Amount set attempt {attempt} error: {e}")
            await asyncio.sleep(0.25)
        return False, float(amount)

    async def _select_duration_from_popup(self, labels: list[str]) -> bool:
        _, control = await self._first_visible_locator(
            self._EXPIRATION_CONTROL_SELECTORS
        )
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
                    txt = (await item.inner_text(timeout=1000) or "").strip()
                    txt_seconds = self.normalize_expiry(txt)
                    target_seconds = [self.normalize_expiry(x) for x in labels]
                    if txt_seconds in target_seconds:
                        await item.click(timeout=3000)
                        await asyncio.sleep(0.35)
                        log(f"Duration selected from popup: {txt}")
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

    async def _set_trade_params(
        self, amount: float, duration: int
    ) -> tuple[bool, float, str]:
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

            balance_before = self.balance

            ok, actual_amount, _ = await self._set_trade_params(
                cfg["trade_amount"], cfg["trade_duration_sec"]
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

            balance_after = self.balance
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

            # Stats
            self.trades += 1

            self.pnl = round(
                self.balance - float(self.start_balance),
                2
            )

            if result == "WIN":

                self.wins += 1

                self.session_wins += 1

                self.recent_trade_results.append(
                    "WIN"
                )

                self.consecutive_losses = 0

            elif result == "LOSS":

                self.losses += 1

                self.session_losses += 1

                self.recent_trade_results.append(
                    "LOSS"
                )

                self.consecutive_losses += 1

                self.daily_loss = abs(
                    min(0, float(self.pnl))
                )

                self.hard_cooldown_candles = 2

                log(
                    "Cooldown activated after loss "
                    "(skip next 2 candles)"
                )

            elif result == "DRAW":

                self.draws += 1

                self.recent_trade_results.append(
                    "DRAW"
                )

                log("Draw trade recorded")

            # keep recent history limited
            self.recent_trade_results = (
                self.recent_trade_results[-20:]
            )

            self.last_signal = direction
            self.last_result = result

            log(
                f"Trade Result: {result} "
                f"pnl={pnl:+.2f}"
            )

            log(
                f"SESSION => "
                f"wins={self.wins} "
                f"losses={self.losses} "
                f"draws={self.draws} "
                f"session_pnl={self.pnl:+.2f}"
            )

            db_insert_trade(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "session_id": self.session_id,
                    "symbol": self.symbol,
                    "direction": direction,
                    "amount": actual_amount,
                    "duration": cfg["trade_duration_sec"],
                    "confidence": confidence,
                    "result": result,
                    "pnl": pnl,
                    "profit_percent": round((pnl / actual_amount) * 100, 1),
                    "balance_after": self.balance,
                    "mode": cfg["mode"],
                }
            )

            latest = df.iloc[-1]

            append_training_row(
                {
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
                    "candle_size": (latest["high"] - latest["low"]),
                    "body_size": abs(latest["close"] - latest["open"]),
                    "atr": latest.get("atr"),
                    "ema_distance": latest.get("ema_distance"),
                    "upper_wick_ratio": latest.get("upper_wick_ratio"),
                    "lower_wick_ratio": latest.get("lower_wick_ratio"),
                    "momentum_accel": latest.get("momentum_accel"),
                }
            )

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
                    deal_id = str(deal.get("id") or deal.get("deal_id") or "")
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

                    if balance_before is not None:
                        await asyncio.sleep(0.8)
                        if await self._read_balance() is not None:
                            delta = round(self.balance - float(balance_before), 2)
                            if abs(delta) > 0.01:
                                result = "WIN" if delta > 0 else "LOSS"
                                log(
                                    f"Resolved Trade from balance delta {result} pnl={delta:+.2f}"
                                )
                                return result, delta

                    win_field = str(deal.get("win", deal.get("result", ""))).lower()
                    is_draw = abs(profit) < 0.01 or abs(profit - invested) < 0.01

                    is_win = not is_draw and (
                        profit > 0
                        or win_field in ("win", "won", "true", "1", "success")
                    )
                    if is_draw:
                        result = "DRAW"
                        pnl = 0.0
                    elif is_win:
                        result = "WIN"
                        pnl = profit - invested if profit > invested else profit
                        if pnl <= 0:
                            pnl = invested * 0.8
                        pnl = round(pnl, 2)
                    else:
                        result = "LOSS"
                        pnl = -round(invested, 2)

                    log(f"Resolved Trade {result} pnl={pnl:+.2f}")
                    return result, pnl

            except Exception as e:
                log(f"_wait_for_trade_result error: {e}")

            await asyncio.sleep(1)

        if balance_before is not None and await self._read_balance() is not None:
            delta = round(self.balance - float(balance_before), 2)
            if abs(delta) > 0.01:
                result = "WIN" if delta > 0 else "LOSS"
                log(
                    f"Trade result inferred from balance delta "
                    f"{result} pnl={delta:+.2f}"
                )
                return result, delta

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
        cfg = self.cfg

        new_interval = int(self.cfg.get("candle_interval_sec", self.collector.interval))

        if new_interval != self.collector.interval:
            log(
                f"Candle interval updated: "
                f"{self.collector.interval}s -> {new_interval}s"
            )
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

        if len(df) < 35:
            log(f"Waiting indicator warmup {len(df)}/35")
            return

        df = compute_indicators(df)

        direction, conf = vote_signal(df)

        latest = df.iloc[-1]

        # corrupted candle protection
        if abs(latest["close"] - latest["open"]) > 0.01:
            log("Skipping corrupted candle")
            return

        # ----- hard cooldown management -----
        current_candle_ts = str(latest["t"])

        if (
            self.hard_cooldown_candles > 0
            and self._last_cooldown_candle != current_candle_ts
        ):
            self._last_cooldown_candle = current_candle_ts
            remaining = self.hard_cooldown_candles
            self.hard_cooldown_candles -= 1
            log(f"Hard cooldown active ({remaining} candles remaining)")
            return

        # centralized indicators
        ema_distance = float(latest.get("ema_distance", 0))
        atr = float(latest.get("atr", 0.0001))
        rsi = float(latest.get("rsi", 50))

        if direction == "NONE":
            if self.last_saved_ts == latest["t"]:
                return
            self.last_saved_ts = latest["t"]

            append_training_row(
                {
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
                    "candle_size": (latest["high"] - latest["low"]),
                    "body_size": abs(latest["close"] - latest["open"]),
                    "atr": latest.get("atr"),
                    "ema_distance": latest.get("ema_distance"),
                    "upper_wick_ratio": latest.get("upper_wick_ratio"),
                    "lower_wick_ratio": latest.get("lower_wick_ratio"),
                    "momentum_accel": latest.get("momentum_accel"),
                }
            )

            self._push_state("RUNNING")
            return

        self.last_confidence = conf

        # ---------------- SESSION FATIGUE ----------------
        total_trades = self.session_wins + self.session_losses

        adaptive_threshold = 0.0

        if total_trades >= 15:
            adaptive_threshold += 0.02

        if total_trades >= 25:
            adaptive_threshold += 0.03

        recent_results = getattr(self, "recent_trade_results", [])

        if len(recent_results) >= 6:

            recent_winrate = (
                sum(1 for x in recent_results[-6:] if x == "WIN")
                / 6
            ) * 100

            if recent_winrate < 55:

                adaptive_threshold += 0.03

                log(
                    f"Safe mode activated "
                    f"(recent_winrate={recent_winrate:.1f}%)"
                )

        if 48 <= rsi <= 52:
            log("Skipping flat RSI market")
            return

        # sideways filter
        recent_range = df["high"].tail(8).max() - df["low"].tail(8).min()
        if recent_range < atr * 1.1:
            log(f"Skipping sideways market (range={recent_range:.6f})")
            return

        # weak trend filter
        if abs(ema_distance) < self.cfg.get("min_ema_distance", 0.00002):
            log("Skipping weak trend " f"(ema_distance={ema_distance:.6f})")
            return

        # ---------------- EMA VELOCITY DECAY ----------------
        prev = df.iloc[-2]

        prev_ema_distance = abs(
            float(prev.get("ema_distance", 0))
        )

        ema_velocity = (
            abs(ema_distance)
            - prev_ema_distance
        )

        if ema_velocity < -0.00001:

            log(
                f"EMA momentum decay detected "
                f"(velocity={ema_velocity:.6f})"
            )

            conf *= 0.82

        # candle analysis
        candle_size = max(latest.get("candle_size", 0), 0.000001)
        body_size = latest.get("body_size", 0)
        body_ratio = body_size / candle_size

        # ---------------- BODY STRENGTH ----------------
        recent_body_avg = max(
            df["body_size"].tail(6).mean(),
            0.000001
        )

        body_strength = (
            body_size / recent_body_avg
        )

        if body_strength < 0.75:

            conf *= 0.84

            log(
                f"Weak candle expansion "
                f"(strength={body_strength:.2f})"
            )

        upper_wick_ratio = latest.get("upper_wick_ratio", 0)
        lower_wick_ratio = latest.get("lower_wick_ratio", 0)

        # rejection wick filters
        if candle_size > 0:
            if direction == "BUY" and upper_wick_ratio > self.cfg.get(
                "max_upper_wick_ratio", 0.55
            ):
                log("BUY rejected: large upper wick")
                return

            if direction == "SELL" and lower_wick_ratio > self.cfg.get(
                "max_lower_wick_ratio", 0.55
            ):
                log("SELL rejected: large lower wick")
                return

        # exhaustion candle
        if body_ratio >= 0.88 and candle_size >= atr * 1.8:
            log(f"Skipping exhaustion candle (body_ratio={body_ratio:.2f})")
            return

        prev_close = df.iloc[-2]["close"]
        close = latest["close"]
        move_size = abs(close - prev_close)

        distance_from_high = latest["high"] - latest["close"]
        distance_from_low = latest["close"] - latest["low"]

        # trend aging preparation
        trend_bars = 0
        closes = df["close"].tail(10).tolist()
        for i in range(len(closes) - 1, 0, -1):
            if direction == "BUY":
                if closes[i] > closes[i - 1]:
                    trend_bars += 1
                else:
                    break
            else:
                if closes[i] < closes[i - 1]:
                    trend_bars += 1
                else:
                    break

        # dynamic market regime
        regime = detect_market_regime(
            atr=atr,
            ema_distance=ema_distance,
            trend_bars=trend_bars,
            candle_size=candle_size,
            cfg=self.cfg,
        )

        if regime == self.pending_regime:
            self.pending_regime_count += 1
        else:
            self.pending_regime = regime
            self.pending_regime_count = 1

        if self.pending_regime_count < 2:
            log(f"Waiting regime confirmation ({regime})")
            return

        regime = self.pending_regime

        dynamic_cfg = apply_dynamic_config(regime, self.cfg)
        runtime_cooldown = dynamic_cfg.get("trade_cooldown_sec", 10)

        self.last_regime = regime

        pullback_threshold = dynamic_cfg.get("pullback_threshold", 0.10)

        if direction == "BUY":
            if distance_from_low < (atr * pullback_threshold):
                log("BUY skipped: weak pullback entry")
                return

        if direction == "SELL":
            if distance_from_high < (atr * pullback_threshold):
                log("SELL skipped: weak pullback entry")
                return

        # overextended candle
        if move_size > self.cfg.get("max_single_candle_move", 0.00022):
            log(f"Skipping overextended move (move={move_size:.6f})")
            return

        # momentum spike protection
        if len(df) >= 3:
            recent_move = abs(latest["close"] - df.iloc[-3]["close"])
            if recent_move > atr * 2.8:
                log("Skipping momentum spike")
                return

        # momentum acceleration
        accel = latest.get("momentum_accel", 0)

        if direction == "BUY" and accel < dynamic_cfg.get("max_accel_buy", -0.00028):
            log("BUY blocked by strong bearish acceleration " f"(accel={accel:.6f})")
            return

        if direction == "SELL" and accel > dynamic_cfg.get("max_accel_sell", 0.00028):
            log("SELL blocked by strong bullish acceleration " f"(accel={accel:.6f})")
            return

        # advanced trend exhaustion
        ema_slope = abs(latest.get("ema_fast", 0) - latest.get("ema_slow", 0))
        recent_body_avg = df["body_size"].tail(4).mean()

        if trend_bars >= 5 and recent_body_avg < atr * 0.55 and ema_slope < 0.000025:
            log("Trend exhaustion detected")
            return

        if trend_bars >= self.cfg.get("trend_age_limit", 6) and conf < 0.72:
            log(f"Skipping aged trend (trend_bars={trend_bars})")
            return

        if getattr(self, "_last_logged_regime", None) != regime:
            self._last_logged_regime = regime
            log(
                f"Market regime: {regime} | "
                f"conf_threshold={dynamic_cfg['confidence_threshold']} "
                f"cooldown={runtime_cooldown}"
            )

        # reset trend trade count on weak trend
        if trend_bars < 2:
            self.current_trend_trade_count = 0

        # max trades per trend protection
        if self.current_trend_trade_count >= dynamic_cfg.get("max_trades_per_trend", 3):
            log(f"Max trades reached for trend ({self.current_trend_trade_count})")
            return

        # Confidence dampening — proportional multipliers, not flat deductions.
        # Flat deductions (e.g. -0.07 -0.08 = -0.15) destroyed the adaptive
        # engine's output: a well-earned conf=0.69 could drop to 0.54 and die.
        # Multipliers preserve the engine's relative judgment: a strong signal
        # stays strong (just slightly dampened), a weak signal stays weak.
        dampen = 1.0

        # ---------------- AGGRESSIVE AGED TREND DAMPEN ----------------

        if trend_bars >= 4:

            dampen *= 0.82

            log(
                "Confidence dampen: "
                "mature trend (×0.82)"
            )

        if trend_bars >= 6:

            dampen *= 0.72

            log(
                "Confidence dampen: "
                "exhausted trend (×0.72)"
            )

        # Low volatility: reduce by 7%
        if atr < self.cfg.get("min_atr", 0.00012):
            dampen *= self.cfg.get("confidence_dampen_low_volatility", 0.93)
            log(f"Confidence dampen: low volatility (×{self.cfg.get('confidence_dampen_low_volatility', 0.93):.2f})")

        # Wick in 0.35–0.55 range: reduce by 9%
        # (hard reject already blocks > 0.55; no dampen below 0.35)
        if direction == "BUY" and upper_wick_ratio > 0.35:
            dampen *= self.cfg.get("confidence_dampen_wick", 0.97)
            log(f"Confidence dampen: bearish wick (×{self.cfg.get('confidence_dampen_wick', 0.97):.2f})")

        if direction == "SELL" and lower_wick_ratio > 0.35:
            dampen *= self.cfg.get("confidence_dampen_wick", 0.97)
            log(f"Confidence dampen: bullish wick (×{self.cfg.get('confidence_dampen_wick', 0.97):.2f})")

        conf = round(max(0.0, min(conf * dampen, 1.0)), 2)
        if dampen < 1.0:
            log(f"Signal: {direction} conf={conf:.2f} (after dampen)")

        if (
            getattr(self, "_last_logged_signal", None)
            != (direction, round(conf, 2))
        ):
            self._last_logged_signal = (direction, round(conf, 2))
            log(f"Signal: {direction} conf={conf:.2f}")

        # ---------------- DYNAMIC THRESHOLD ----------------

        dynamic_threshold = (
            dynamic_cfg["confidence_threshold"]
            + adaptive_threshold
        )

        log(
            f"Signal: {direction} "
            f"conf={conf:.2f} "
            f"threshold={dynamic_threshold:.2f}"
        )

        if conf < dynamic_threshold:

            log(
                f"Conf {conf:.2f} below "
                f"threshold "
                f"{dynamic_threshold:.2f}"
            )

            return

        self.last_signal = direction
        self._push_state("RUNNING")
        latest_candle_ts = df.iloc[-1]["t"]

        # cooldown
        if self._cooldown_active(runtime_cooldown):
            log("Cooldown active")
            return

        if self.last_trade_candle_ts == latest_candle_ts:
            log("Already traded this candle")
            return

        overall_trend = latest["ema_fast"] - latest["ema_slow"]
        strong_bias_threshold = atr * 0.3

        if direction == "BUY" and overall_trend < -strong_bias_threshold:
            log("BUY blocked by bearish market bias")
            return

        if direction == "SELL" and overall_trend > strong_bias_threshold:
            log("SELL blocked by bullish market bias")
            return

        # trend exhaustion protection
        if direction == "BUY" and trend_bars >= 5 and conf < 0.78:
            log("BUY skipped: late trend continuation")
            return

        # expiry selection — now has fallback so valid signals always get an expiry
        label, seconds = choose_expiry(conf, atr, ema_distance, self.cfg, regime)

        # ---------------- STRICTER S30 RULES ----------------

        if seconds == 30:

            if conf < 0.78:

                log(
                    "S30 blocked: "
                    "confidence not strong enough"
                )

                return

            if trend_bars >= 4:

                log(
                    "S30 blocked: "
                    "trend too mature"
                )

                return

            if body_strength < 1.2:

                log(
                    "S30 blocked: "
                    "weak expansion"
                )

                return

            if ema_velocity < 0:

                log(
                    "S30 blocked: "
                    "momentum decay"
                )

                return

        if not seconds:
            self._no_expiry_count += 1
            if self._no_expiry_count % 3 == 1:
                log(
                    f"No valid expiry selected (conf={conf:.2f}, "
                    f"total_skipped={self._no_expiry_count})"
                )
            return

        # Reset no-expiry counter on successful selection
        self._no_expiry_count = 0

        selected_trade_duration = seconds
        log(f"Dynamic expiry selected {label} ({seconds}s)")

        await self._page.keyboard.press("Escape")
        await asyncio.sleep(0.2)

        # reset repeated direction tracking after long inactivity
        if time.time() - self.last_trade_ts > 120:
            self.same_direction_count = 0
            self.last_trade_direction = None

        # adaptive repeated direction protection
        max_same_direction = {
            "TRENDING": 5,
            "NORMAL": 3,
            "VOLATILE": 2,
            "RANGING": 1,
            "EXHAUSTED": 1,
        }.get(regime, 3)

        projected_same_direction_count = (
            self.same_direction_count + 1
            if direction == self.last_trade_direction
            else 1
        )

        if projected_same_direction_count > max_same_direction:
            log(
                f"Too many repeated {direction} trades "
                f"({projected_same_direction_count}/{max_same_direction})"
            )
            return

        original_duration = cfg["trade_duration_sec"]
        cfg["trade_duration_sec"] = selected_trade_duration

        ok = await self._execute_trade(direction, conf, df)

        cfg["trade_duration_sec"] = original_duration

        if ok:
            self.last_trade_candle_ts = latest_candle_ts
            self.current_trend_trade_count += 1

            if direction == self.last_trade_direction:
                self.same_direction_count += 1
            else:
                self.same_direction_count = 1

            self.last_trade_direction = direction

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
                self.start_balance = self.balance
                log(f"Session start balance: {self.start_balance}")

            try:
                dataset_path = ROOT / "storage" / "datasets" / "training_data.csv"
                if dataset_path.exists():
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