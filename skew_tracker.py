#!/usr/bin/env python3
"""
Skew vs Spot Divergence Tracker

Tracks 25-delta put skew changes versus sigma-normalized spot moves
to detect divergences that signal reversal or continuation risk.

Logic (from the post):
  - Spot drops + skew spikes → institutions buying more protection → continuation
  - Spot drops + skew flat  → institutions not worried → likely reversal
  - Spot rips  + skew spikes → hedging the rally → skepticism / reversal
  - Spot rips  + skew flat   → no fear → continuation

Usage:
  python skew_tracker.py                     # scan default watchlist
  python skew_tracker.py AAPL TSLA NVDA      # scan specific tickers
  python skew_tracker.py --days 10           # change lookback window
"""

import sys
import os
import time
import json
import sqlite3
import argparse
import datetime as dt
from typing import Dict, List, Optional, Tuple

import urllib.request

import numpy as np
import pandas as pd
import yfinance as yf

# Import weeklies universe if available
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from weeklies_universe import get_all_tickers
    WEEKLIES = get_all_tickers()
except ImportError:
    WEEKLIES = []


# ---------------------------------------------------------------------------
# Yang-Zhang Historical Volatility (OHLC — superior to close-to-close)
# ---------------------------------------------------------------------------
def _yang_zhang_vol(hist: pd.DataFrame, window: int = 20) -> float:
    """Yang-Zhang volatility estimator using Open, High, Low, Close.

    Combines overnight (close-to-open), Rogers-Satchell (intraday range),
    and close-to-close components. Handles gaps, is drift-independent,
    and ~8x more efficient than close-to-close.

    Returns annualized volatility in percentage (e.g. 25.4 means 25.4%).
    """
    df = hist.tail(window + 1).copy()
    if len(df) < window + 1:
        return 0.0

    o = np.log(df["Open"].values)
    h = np.log(df["High"].values)
    l = np.log(df["Low"].values)
    c = np.log(df["Close"].values)

    # Overnight return: open-to-previous-close
    overnight = o[1:] - c[:-1]
    # Open-to-close
    oc = c[1:] - o[1:]

    # Rogers-Satchell intraday component (uses high, low, open, close)
    rs = (h[1:] - c[1:]) * (h[1:] - o[1:]) + (l[1:] - c[1:]) * (l[1:] - o[1:])

    n = len(overnight)
    if n < 2:
        return 0.0

    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var_overnight = np.sum((overnight - overnight.mean()) ** 2) / (n - 1)
    var_oc = np.sum((oc - oc.mean()) ** 2) / (n - 1)
    var_rs = np.sum(rs) / n

    yz_var = var_overnight + k * var_oc + (1 - k) * var_rs
    yz_var = max(yz_var, 0.0)

    annualized = float(np.sqrt(yz_var * 252) * 100)
    return annualized


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skew_history.db")
DEFAULT_TICKERS = [
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN",
    "META", "GOOGL", "AMD", "NFLX", "CRM", "COIN", "SHOP",
]
SKEW_LOOKBACK_DAYS = 5       # 5-day change in skew
SIGMA_LOOKBACK_DAYS = 5      # 1-week sigma move (5 trading days)
HV_LOOKBACK_DAYS = 20        # 20-day realized vol for sigma calc
DTE_MIN = 20                 # options chain DTE window
DTE_MAX = 50
DIVERGENCE_THRESHOLD = 0.5   # minimum sigma move to flag divergence
BATCH_DELAY = 0.8            # seconds between tickers (yfinance rate limit — raised from 0.3 to avoid 429s)


# ---------------------------------------------------------------------------
# FINRA Dark Pool (RegSHO Short Volume) — cached per scan session
# ---------------------------------------------------------------------------
_FINRA_CACHE: Dict[str, Dict[str, Tuple[int, int]]] = {}  # date_str -> {ticker: (short_vol, total_vol)}


def fetch_finra_short_volume(ticker: str, date_str: str = None) -> Tuple[Optional[int], Optional[int]]:
    """Fetch FINRA RegSHO short volume for a ticker.

    Downloads https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
    and caches it per scan session so we only download once per date (not per ticker).

    Returns (short_volume, total_volume) or (None, None) if unavailable.
    """
    if date_str is None:
        date_str = dt.date.today().isoformat()

    # Convert date to YYYYMMDD format for FINRA URL
    try:
        d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None, None
    yyyymmdd = d.strftime("%Y%m%d")

    # Check cache first
    if yyyymmdd in _FINRA_CACHE:
        entry = _FINRA_CACHE[yyyymmdd].get(ticker.upper())
        if entry:
            return entry
        return None, None

    # Download and parse
    url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{yyyymmdd}.txt"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            lines = resp.read().decode("utf-8", errors="replace").splitlines()

        parsed: Dict[str, Tuple[int, int]] = {}
        for line in lines:
            parts = line.strip().split("|")
            if len(parts) < 5:
                continue
            # Format: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
            sym = parts[1].strip().upper()
            try:
                short_vol = int(float(parts[2]))
                total_vol = int(float(parts[4]))
                # Aggregate across markets (CNMS file may have multiple rows per ticker)
                if sym in parsed:
                    old_s, old_t = parsed[sym]
                    parsed[sym] = (old_s + short_vol, old_t + total_vol)
                else:
                    parsed[sym] = (short_vol, total_vol)
            except (ValueError, IndexError):
                continue

        _FINRA_CACHE[yyyymmdd] = parsed
        entry = parsed.get(ticker.upper())
        if entry:
            return entry
        return None, None

    except Exception:
        # File not available (weekend, holiday, or too early in the day)
        _FINRA_CACHE[yyyymmdd] = {}  # cache the miss so we don't retry
        return None, None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def init_db(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skew_daily (
            ticker      TEXT NOT NULL,
            date        TEXT NOT NULL,
            spot_close  REAL,
            atm_iv      REAL,
            put_25d_iv  REAL,
            call_25d_iv REAL,
            skew        REAL,
            skew_ratio  REAL,
            hv_10d      REAL,
            pc_ratio    REAL,
            put_vol     INTEGER,
            call_vol    INTEGER,
            PRIMARY KEY (ticker, date)
        )
    """)
    # Add columns if upgrading from older schema
    for col, coltype in [("pc_ratio", "REAL"), ("put_vol", "INTEGER"), ("call_vol", "INTEGER"),
                          ("put_wall_strike", "REAL"), ("put_wall_iv", "REAL"), ("put_wall_oi", "INTEGER"),
                          ("call_wall_strike", "REAL"), ("call_wall_iv", "REAL"), ("call_wall_oi", "INTEGER"),
                          ("sector", "TEXT"), ("industry", "TEXT"),
                          ("ticker_vix", "REAL"), ("hv_10d", "REAL"), ("iv_hv_ratio", "REAL"),
                          ("dte_earnings", "INTEGER"),
                          ("vwks", "REAL"), ("vwks_volume", "INTEGER"),
                          ("near_skew", "REAL"), ("near_pc_ratio", "REAL"),
                          ("near_atm_iv", "REAL"), ("near_dte", "INTEGER"),
                          ("skew_term_structure", "TEXT"),
                          ("stock_volume", "INTEGER"),
                          ("hv_method", "TEXT"),  # 'CC' = close-to-close, 'YZ' = Yang-Zhang
                          ("gex", "REAL"),  # per-ticker Gamma Exposure ($ notional)
                          ("gex_to_volume", "REAL"),  # GEX / stock_volume ratio
                          ("put_wall_oi_change", "INTEGER"),  # change in put wall OI from prior scan
                          ("call_wall_oi_change", "INTEGER"),  # change in call wall OI from prior scan
                          ("total_options_oi", "INTEGER"),  # total open interest across all strikes
                          ("short_interest", "INTEGER"),  # shares short from yfinance
                          ("short_ratio", "REAL"),  # days to cover
                          ("short_pct_float", "REAL"),  # short % of float
                          ("dark_pool_short_vol", "INTEGER"),  # FINRA daily short volume
                          ("dark_pool_total_vol", "INTEGER"),  # FINRA daily total volume
                          ("dark_pool_short_pct", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE skew_daily ADD COLUMN {col} {coltype}")
        except Exception:
            pass  # column already exists

    # Methodology changelog — track when we change how metrics are computed
    conn.execute("""
        CREATE TABLE IF NOT EXISTS methodology_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            change_date TEXT NOT NULL,
            field       TEXT NOT NULL,
            old_method  TEXT,
            new_method  TEXT,
            note        TEXT
        )
    """)
    # Log the Yang-Zhang switch (idempotent — only inserts once)
    conn.execute(
        "INSERT OR IGNORE INTO methodology_log (change_date, field, old_method, new_method, note) "
        "VALUES (?, ?, ?, ?, ?)",
        ('2026-03-21', 'hv_20d / hv_30d', 'Close-to-Close (std of log returns)',
         'Yang-Zhang (OHLC + overnight gaps)',
         'Switched to Yang-Zhang estimator which uses Open/High/Low/Close. '
         'All data BEFORE 2026-03-21 uses close-to-close. All data ON or AFTER uses Yang-Zhang. '
         'Yang-Zhang is ~8x more statistically efficient and captures intraday range + overnight gaps.')
    )
    conn.execute(
        "INSERT OR IGNORE INTO methodology_log (change_date, field, old_method, new_method, note) "
        "VALUES (?, ?, ?, ?, ?)",
        ('2026-03-22', 'hv_20d/hv_30d -> hv_10d', 'HV20 + HV30 (two separate windows)',
         'HV10 (single 10-day Yang-Zhang)',
         'Switched to 10-day rolling HV window. More responsive to recent realized vol. '
         'Single window replaces the old hv_20d and hv_30d columns.')
    )

    # Fixed-strike vol history — tracks IV at specific strikes over time
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fixed_strike_vol (
            ticker      TEXT NOT NULL,
            date        TEXT NOT NULL,
            strike      REAL NOT NULL,
            side        TEXT NOT NULL,
            iv          REAL,
            oi          INTEGER,
            volume      INTEGER,
            spot_at_snap REAL,
            PRIMARY KEY (ticker, date, strike, side)
        )
    """)

    # Signal log — every buy/sell signal with full metrics at signal time
    # This is the "day one" history that never gets windowed
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            signal_date     TEXT NOT NULL,
            signal_type     TEXT NOT NULL,
            direction       TEXT NOT NULL,
            spot_at_signal  REAL,
            sigma_iv        REAL,
            sigma_hv        REAL,
            iv_surprise     INTEGER,
            skew_at_signal  REAL,
            skew_change_5d  REAL,
            atm_iv          REAL,
            hv_10d          REAL,
            iv_hv_ratio     REAL,
            pc_ratio        REAL,
            put_wall_strike REAL,
            put_wall_iv     REAL,
            call_wall_strike REAL,
            call_wall_iv    REAL,
            put_wall_direction TEXT,
            call_wall_direction TEXT,
            wall_signal     TEXT,
            sector          TEXT,
            industry        TEXT,
            sector_skew_rank REAL,
            sector_iv_rank  REAL,
            vwks            REAL,
            trade_action    TEXT,
            conviction      TEXT,
            -- forward returns filled in later
            fwd_1d_return   REAL,
            fwd_3d_return   REAL,
            fwd_5d_return   REAL,
            fwd_10d_return  REAL,
            fwd_20d_return  REAL,
            fwd_1d_date     TEXT,
            fwd_3d_date     TEXT,
            fwd_5d_date     TEXT,
            fwd_10d_date    TEXT,
            fwd_20d_date    TEXT,
            outcome         TEXT,
            UNIQUE(ticker, signal_date, signal_type)
        )
    """)

    # Candidate log — every scanned ticker, whether or not a signal fired
    # This is the research backbone for combo mining and hypothesis testing
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_log (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                  TEXT NOT NULL,
            scan_date               TEXT NOT NULL,
            sector                  TEXT,
            industry                TEXT,
            spot_close              REAL,
            signal_family           TEXT,
            direction_hint          TEXT,
            current_signal          TEXT,
            is_tradeable            INTEGER DEFAULT 0,
            spot_return_pct         REAL,
            sigma_iv                REAL,
            sigma_hv                REAL,
            iv_surprise             INTEGER DEFAULT 0,
            skew                    REAL,
            skew_change_5d          REAL,
            atm_iv                  REAL,
            hv_10d                  REAL,
            iv_hv_ratio             REAL,
            pc_ratio                REAL,
            put_vol                 INTEGER,
            call_vol                INTEGER,
            put_wall_strike         REAL,
            put_wall_iv             REAL,
            put_wall_oi             INTEGER,
            call_wall_strike        REAL,
            call_wall_iv            REAL,
            call_wall_oi            INTEGER,
            put_wall_oi_change      INTEGER,
            call_wall_oi_change     INTEGER,
            wall_signal             TEXT,
            ticker_vix              REAL,
            dte_earnings            INTEGER,
            vwks                    REAL,
            vwks_volume             INTEGER,
            near_skew               REAL,
            near_pc_ratio           REAL,
            near_atm_iv             REAL,
            near_dte                INTEGER,
            skew_term_structure     TEXT,
            stock_volume            INTEGER,
            gex                     REAL,
            gex_to_volume           REAL,
            total_options_oi        INTEGER,
            short_interest          INTEGER,
            short_ratio             REAL,
            short_pct_float         REAL,
            dark_pool_short_vol     INTEGER,
            dark_pool_total_vol     INTEGER,
            dark_pool_short_pct     REAL,
            sector_skew_rank        REAL,
            sector_iv_rank          REAL,
            industry_skew_rank      REAL,
            industry_iv_rank        REAL,
            sector_median_skew      REAL,
            sector_median_atm_iv    REAL,
            sector_median_iv_hv_ratio REAL,
            industry_median_skew    REAL,
            industry_median_atm_iv  REAL,
            industry_median_iv_hv_ratio REAL,
            sector_breadth_up       INTEGER,
            sector_breadth_down     INTEGER,
            sector_breadth_skew_up  INTEGER,
            sector_breadth_skew_down INTEGER,
            industry_breadth_up     INTEGER,
            industry_breadth_down   INTEGER,
            skew_pctile_6m          REAL,
            atm_iv_pctile_6m        REAL,
            iv_hv_ratio_pctile_6m   REAL,
            realized_move_pctile_6m REAL,
            notes_json              TEXT,
            fwd_1d_return           REAL,
            fwd_3d_return           REAL,
            fwd_5d_return           REAL,
            fwd_10d_return          REAL,
            fwd_20d_return          REAL,
            fwd_1d_date             TEXT,
            fwd_3d_date             TEXT,
            fwd_5d_date             TEXT,
            fwd_10d_date            TEXT,
            fwd_20d_date            TEXT,
            fwd_5d_return_short     REAL,
            sector_residual_5d      REAL,
            industry_residual_5d    REAL,
            UNIQUE(ticker, scan_date)
        )
    """)
    # Safe migration: add columns that might not exist yet
    _candidate_log_cols = [
        ("sector_skew_rank", "REAL"), ("sector_iv_rank", "REAL"),
        ("industry_skew_rank", "REAL"), ("industry_iv_rank", "REAL"),
        ("sector_median_skew", "REAL"), ("sector_median_atm_iv", "REAL"),
        ("sector_median_iv_hv_ratio", "REAL"),
        ("industry_median_skew", "REAL"), ("industry_median_atm_iv", "REAL"),
        ("industry_median_iv_hv_ratio", "REAL"),
        ("sector_breadth_up", "INTEGER"), ("sector_breadth_down", "INTEGER"),
        ("sector_breadth_skew_up", "INTEGER"), ("sector_breadth_skew_down", "INTEGER"),
        ("industry_breadth_up", "INTEGER"), ("industry_breadth_down", "INTEGER"),
        ("industry_breadth_skew_up", "REAL"), ("industry_breadth_skew_down", "REAL"),
        ("skew_pctile_6m", "REAL"), ("atm_iv_pctile_6m", "REAL"),
        ("iv_hv_ratio_pctile_6m", "REAL"), ("realized_move_pctile_6m", "REAL"),
        ("notes_json", "TEXT"),
        ("fwd_1d_return", "REAL"), ("fwd_3d_return", "REAL"),
        ("fwd_5d_return", "REAL"), ("fwd_10d_return", "REAL"), ("fwd_20d_return", "REAL"),
        ("fwd_1d_date", "TEXT"), ("fwd_3d_date", "TEXT"),
        ("fwd_5d_date", "TEXT"), ("fwd_10d_date", "TEXT"), ("fwd_20d_date", "TEXT"),
        ("fwd_5d_return_short", "REAL"),
        ("sector_residual_5d", "REAL"), ("industry_residual_5d", "REAL"),
    ]
    for col, coltype in _candidate_log_cols:
        try:
            conn.execute(f"ALTER TABLE candidate_log ADD COLUMN {col} {coltype}")
        except Exception:
            pass

    conn.commit()
    return conn


def save_candidate(conn: sqlite3.Connection, ticker: str, scan_date: str,
                   snap: Dict, div: Optional[Dict] = None,
                   walls: Optional[Dict] = None,
                   sector_ranks: Optional[Dict] = None):
    """Log every scanned ticker to candidate_log — whether or not a signal fired.

    This builds the research dataset for combo mining and hypothesis testing.
    """
    # Determine signal family and direction from divergence (if any)
    signal_family = ""
    direction_hint = ""
    current_signal = ""
    is_tradeable = 0
    spot_return_pct = None
    sigma_iv = None
    sigma_hv = None
    iv_surprise = 0
    skew_change_5d = None
    wall_signal_str = ""

    if div:
        current_signal = div.get("signal", "NEUTRAL")
        is_tradeable = 1 if div.get("tradeable", False) else 0
        spot_return_pct = div.get("spot_return_pct")
        sigma_iv = div.get("sigma_iv")
        sigma_hv = div.get("sigma_hv")
        iv_surprise = 1 if div.get("iv_surprise", False) else 0
        skew_change_5d = div.get("skew_change")
        # Signal family: group by reversal vs continuation
        if "REVERSAL" in current_signal:
            signal_family = "REVERSAL"
        elif "CONTINUATION" in current_signal:
            signal_family = "CONTINUATION"
        # Direction hint
        if "BULLISH" in current_signal:
            direction_hint = "LONG"
        elif "BEARISH" in current_signal:
            direction_hint = "SHORT"

    if walls:
        wall_signal_str = walls.get("wall_signal", "")

    atm_iv = snap.get("atm_iv", 0) or 0
    hv_10d = snap.get("hv_10d", 0) or 0
    iv_hv_ratio = round(atm_iv / max(hv_10d, 0.01), 2) if hv_10d > 0 else None

    # Read OI changes from the already-saved skew_daily row
    put_wall_oi_change = None
    call_wall_oi_change = None
    try:
        row = conn.execute(
            "SELECT put_wall_oi_change, call_wall_oi_change FROM skew_daily "
            "WHERE ticker = ? AND date = ?", (ticker, scan_date)
        ).fetchone()
        if row:
            put_wall_oi_change, call_wall_oi_change = row
    except Exception:
        pass

    conn.execute("""
        INSERT OR REPLACE INTO candidate_log
        (ticker, scan_date, sector, industry, spot_close,
         signal_family, direction_hint, current_signal, is_tradeable,
         spot_return_pct, sigma_iv, sigma_hv, iv_surprise,
         skew, skew_change_5d, atm_iv, hv_10d, iv_hv_ratio,
         pc_ratio, put_vol, call_vol,
         put_wall_strike, put_wall_iv, put_wall_oi,
         call_wall_strike, call_wall_iv, call_wall_oi,
         put_wall_oi_change, call_wall_oi_change, wall_signal,
         ticker_vix, dte_earnings,
         vwks, vwks_volume,
         near_skew, near_pc_ratio, near_atm_iv, near_dte, skew_term_structure,
         stock_volume, gex, gex_to_volume,
         total_options_oi, short_interest, short_ratio, short_pct_float,
         dark_pool_short_vol, dark_pool_total_vol, dark_pool_short_pct,
         sector_skew_rank, sector_iv_rank)
        VALUES (?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?)
    """, (
        ticker, scan_date,
        snap.get("sector", ""), snap.get("industry", ""),
        snap.get("spot_close", 0),
        signal_family, direction_hint, current_signal, is_tradeable,
        spot_return_pct, sigma_iv, sigma_hv, iv_surprise,
        snap.get("skew", 0), skew_change_5d,
        atm_iv, hv_10d, iv_hv_ratio,
        snap.get("pc_ratio", 0), snap.get("put_vol", 0), snap.get("call_vol", 0),
        snap.get("put_wall_strike", 0), snap.get("put_wall_iv", 0), snap.get("put_wall_oi", 0),
        snap.get("call_wall_strike", 0), snap.get("call_wall_iv", 0), snap.get("call_wall_oi", 0),
        put_wall_oi_change, call_wall_oi_change, wall_signal_str,
        snap.get("ticker_vix", 0), snap.get("dte_earnings"),
        snap.get("vwks", 0), snap.get("vwks_volume", 0),
        snap.get("near_skew", 0), snap.get("near_pc_ratio", 0),
        snap.get("near_atm_iv", 0), snap.get("near_dte"),
        snap.get("skew_term_structure", ""),
        snap.get("stock_volume", 0), snap.get("gex", 0), snap.get("gex_to_volume"),
        snap.get("total_options_oi"), snap.get("short_interest"),
        snap.get("short_ratio"), snap.get("short_pct_float"),
        snap.get("dark_pool_short_vol"), snap.get("dark_pool_total_vol"),
        snap.get("dark_pool_short_pct"),
        sector_ranks.get("sector_skew_rank", 0) if sector_ranks else 0,
        sector_ranks.get("sector_iv_rank", 0) if sector_ranks else 0,
    ))
    conn.commit()


def save_reading(conn: sqlite3.Connection, ticker: str, date: str, data: Dict):
    # --- Compute put/call wall OI change from prior reading ---
    put_wall_oi_change = None
    call_wall_oi_change = None
    try:
        # GAP GUARD (2026-07-20): bound the "prior reading" to the last ~10 calendar
        # days. Unbounded, after the 6/27-7/17 outage this picked up the 6/26 row and
        # reported a THREE-WEEK OI delta as a day-over-day change — and
        # put_wall_oi_change <= 0 is a Tier A filter. No recent prior => leave it None,
        # which excludes the candidate (the Tier A query requires it NOT NULL).
        _floor = (dt.datetime.strptime(date, "%Y-%m-%d").date() - dt.timedelta(days=10)).isoformat()
        prior = conn.execute(
            "SELECT put_wall_oi, call_wall_oi FROM skew_daily "
            "WHERE ticker = ? AND date < ? AND date >= ? ORDER BY date DESC LIMIT 1",
            (ticker, date, _floor)
        ).fetchone()
        if prior:
            prior_put_oi, prior_call_oi = prior
            cur_put_oi = data.get("put_wall_oi", 0) or 0
            cur_call_oi = data.get("call_wall_oi", 0) or 0
            if prior_put_oi and prior_put_oi > 0:
                put_wall_oi_change = cur_put_oi - prior_put_oi
            if prior_call_oi and prior_call_oi > 0:
                call_wall_oi_change = cur_call_oi - prior_call_oi
    except Exception:
        pass

    # --- Fetch FINRA dark pool data ---
    # Try today first, then fall back to prior trading days (FINRA publishes with delay)
    dp_short_vol, dp_total_vol = fetch_finra_short_volume(ticker, date)
    if dp_short_vol is None:
        try:
            d = dt.datetime.strptime(date, "%Y-%m-%d").date()
            for offset in range(1, 4):  # try up to 3 days back
                prev = d - dt.timedelta(days=offset)
                dp_short_vol, dp_total_vol = fetch_finra_short_volume(ticker, prev.isoformat())
                if dp_short_vol is not None:
                    break
        except Exception:
            pass
    dp_short_pct = None
    if dp_short_vol is not None and dp_total_vol is not None and dp_total_vol > 0:
        dp_short_pct = round(dp_short_vol / dp_total_vol, 4)

    conn.execute("""
        INSERT OR REPLACE INTO skew_daily
        (ticker, date, spot_close, atm_iv, put_25d_iv, call_25d_iv, skew, skew_ratio, hv_10d,
         pc_ratio, put_vol, call_vol,
         put_wall_strike, put_wall_iv, put_wall_oi,
         call_wall_strike, call_wall_iv, call_wall_oi,
         sector, industry,
         ticker_vix, iv_hv_ratio, dte_earnings,
         vwks, vwks_volume,
         near_skew, near_pc_ratio, near_atm_iv, near_dte, skew_term_structure,
         stock_volume, hv_method, gex, gex_to_volume,
         put_wall_oi_change, call_wall_oi_change, total_options_oi,
         short_interest, short_ratio, short_pct_float,
         dark_pool_short_vol, dark_pool_total_vol, dark_pool_short_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticker, date,
        data.get("spot_close", 0),
        data.get("atm_iv", 0),
        data.get("put_25d_iv", 0),
        data.get("call_25d_iv", 0),
        data.get("skew", 0),
        data.get("skew_ratio", 1.0),
        data.get("hv_10d", 0),
        data.get("pc_ratio", 0),
        data.get("put_vol", 0),
        data.get("call_vol", 0),
        data.get("put_wall_strike", 0),
        data.get("put_wall_iv", 0),
        data.get("put_wall_oi", 0),
        data.get("call_wall_strike", 0),
        data.get("call_wall_iv", 0),
        data.get("call_wall_oi", 0),
        data.get("sector", ""),
        data.get("industry", ""),
        data.get("ticker_vix", 0),
        data.get("iv_hv_ratio", 0),
        data.get("dte_earnings"),
        data.get("vwks", 0),
        data.get("vwks_volume", 0),
        data.get("near_skew", 0),
        data.get("near_pc_ratio", 0),
        data.get("near_atm_iv", 0),
        data.get("near_dte"),
        data.get("skew_term_structure", ""),
        data.get("stock_volume", 0),
        "YZ",  # Yang-Zhang (OHLC) — all rows before 2026-03-21 were "CC" (close-to-close)
        data.get("gex", 0),
        data.get("gex_to_volume"),
        put_wall_oi_change,
        call_wall_oi_change,
        data.get("total_options_oi"),
        data.get("short_interest"),
        data.get("short_ratio"),
        data.get("short_pct_float"),
        dp_short_vol,
        dp_total_vol,
        dp_short_pct,
    ))
    # Save fixed-strike vol for the top OI strikes
    for strike_data in data.get("fixed_strikes", []):
        conn.execute("""
            INSERT OR REPLACE INTO fixed_strike_vol
            (ticker, date, strike, side, iv, oi, volume, spot_at_snap)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticker, date,
            strike_data["strike"], strike_data["side"],
            strike_data["iv"], strike_data["oi"],
            strike_data["volume"], data.get("spot_close", 0),
        ))
    conn.commit()


# ---------------------------------------------------------------------------
# Sector / Industry lookup (cached per session)
# ---------------------------------------------------------------------------
_SECTOR_CACHE: Dict[str, Tuple[str, str]] = {}

# Well-known ETFs — yfinance doesn't return sector for these
_ETF_SECTORS = {
    "SPY": ("ETF", "S&P 500"), "QQQ": ("ETF", "Nasdaq 100"), "IWM": ("ETF", "Russell 2000"),
    "DIA": ("ETF", "Dow 30"), "XLF": ("ETF", "Financials"), "XLK": ("ETF", "Technology"),
    "XLE": ("ETF", "Energy"), "XLV": ("ETF", "Healthcare"), "XLI": ("ETF", "Industrials"),
    "XLP": ("ETF", "Consumer Staples"), "XLY": ("ETF", "Consumer Discretionary"),
    "XLU": ("ETF", "Utilities"), "XLB": ("ETF", "Materials"), "XLRE": ("ETF", "Real Estate"),
    "XLC": ("ETF", "Communication Services"), "SMH": ("ETF", "Semiconductors"),
    "GLD": ("ETF", "Gold"), "SLV": ("ETF", "Silver"), "TLT": ("ETF", "Treasuries"),
    "HYG": ("ETF", "High Yield Bonds"), "EEM": ("ETF", "Emerging Markets"),
    "ARKK": ("ETF", "Innovation"), "SOXX": ("ETF", "Semiconductors"),
}


def get_sector(ticker: str) -> Tuple[str, str]:
    """Get (sector, industry) for a ticker. Cached per session."""
    if ticker in _SECTOR_CACHE:
        return _SECTOR_CACHE[ticker]
    if ticker in _ETF_SECTORS:
        _SECTOR_CACHE[ticker] = _ETF_SECTORS[ticker]
        return _SECTOR_CACHE[ticker]
    try:
        info = yf.Ticker(ticker).info
        sector = info.get("sector", "Unknown")
        industry = info.get("industry", "Unknown")
        _SECTOR_CACHE[ticker] = (sector, industry)
    except Exception:
        _SECTOR_CACHE[ticker] = ("Unknown", "Unknown")
    return _SECTOR_CACHE[ticker]


# ---------------------------------------------------------------------------
# Signal logging — timestamps every signal with full metrics
# ---------------------------------------------------------------------------
def log_signal(conn: sqlite3.Connection, ticker: str, div: Dict, snap: Dict,
               walls: Optional[Dict], sector: str, industry: str,
               sector_skew_rank: float = 0, sector_iv_rank: float = 0):
    """Log a trade signal with all metrics for forward return tracking."""
    today_str = dt.date.today().isoformat()

    # Determine conviction from signal strength
    conviction = "LOW"
    if div.get("iv_surprise") and abs(div.get("sigma_iv", 0)) >= 1.0:
        conviction = "HIGH"
    elif abs(div.get("sigma_iv", 0)) >= 0.75:
        conviction = "MEDIUM"

    # Determine direction for forward return interpretation
    direction = "LONG" if "BULLISH" in div.get("signal", "") else "SHORT"

    conn.execute("""
        INSERT OR IGNORE INTO signal_log
        (ticker, signal_date, signal_type, direction, spot_at_signal,
         sigma_iv, sigma_hv, iv_surprise, skew_at_signal, skew_change_5d,
         atm_iv, hv_10d, iv_hv_ratio, pc_ratio,
         put_wall_strike, put_wall_iv, call_wall_strike, call_wall_iv,
         put_wall_direction, call_wall_direction, wall_signal,
         sector, industry, sector_skew_rank, sector_iv_rank,
         vwks, trade_action, conviction)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticker, today_str, div["signal"], direction, snap.get("spot_close", 0),
        div.get("sigma_iv", 0), div.get("sigma_hv", 0), int(div.get("iv_surprise", False)),
        div.get("skew_now", 0), div.get("skew_change", 0),
        snap.get("atm_iv", 0), snap.get("hv_10d", 0),
        round(snap.get("atm_iv", 0) / max(snap.get("hv_10d", 1), 1), 2),
        snap.get("pc_ratio", 0),
        snap.get("put_wall_strike", 0), snap.get("put_wall_iv", 0),
        snap.get("call_wall_strike", 0), snap.get("call_wall_iv", 0),
        walls.get("put_wall_direction", "") if walls else "",
        walls.get("call_wall_direction", "") if walls else "",
        walls.get("wall_signal", "") if walls else "",
        sector, industry, sector_skew_rank, sector_iv_rank,
        snap.get("vwks", 0),
        div.get("trade_action", ""), conviction,
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# Forward return updater — fills in actual returns after signals
# ---------------------------------------------------------------------------
def update_forward_returns(conn: sqlite3.Connection):
    """Go back and fill in forward returns for past signals.

    Runs each scan session. Checks which signals are missing forward returns
    and fills them in using yfinance price data.
    """
    today = dt.date.today()
    # Find signals that need updating (have NULL forward returns)
    rows = conn.execute("""
        SELECT id, ticker, signal_date, spot_at_signal, direction
        FROM signal_log
        WHERE fwd_20d_return IS NULL
        ORDER BY signal_date
    """).fetchall()

    if not rows:
        return 0

    updated = 0
    # Group by ticker to minimize yfinance calls
    ticker_signals: Dict[str, list] = {}
    for row in rows:
        sig_id, ticker, sig_date, spot_at_signal, direction = row
        ticker_signals.setdefault(ticker, []).append({
            "id": sig_id, "date": sig_date, "spot": spot_at_signal, "direction": direction
        })

    for ticker, signals in ticker_signals.items():
        try:
            # Get enough price history to cover all forward windows
            earliest_date = min(s["date"] for s in signals)
            start_dt = dt.datetime.strptime(earliest_date, "%Y-%m-%d").date()
            hist = yf.Ticker(ticker).history(
                start=(start_dt - dt.timedelta(days=5)).isoformat(),
                end=(today + dt.timedelta(days=1)).isoformat()
            )
            if hist.empty:
                continue

            hist.index = pd.to_datetime(hist.index).tz_localize(None)

            for sig in signals:
                sig_date = dt.datetime.strptime(sig["date"], "%Y-%m-%d")
                spot_at = sig["spot"]
                if spot_at <= 0:
                    continue

                updates = {}
                all_filled = True
                for label, n_days in [("1d", 1), ("3d", 3), ("5d", 5), ("10d", 10), ("20d", 20)]:
                    target_date = sig_date + dt.timedelta(days=n_days)
                    # Find closest trading day on or after target
                    future = hist[hist.index >= target_date]
                    if not future.empty:
                        actual_date = future.index[0]
                        actual_price = float(future["Close"].iloc[0])
                        ret = round((actual_price - spot_at) / spot_at * 100, 2)
                        # Flip sign for SHORT signals (we profit from down moves)
                        if sig["direction"] == "SHORT":
                            ret = -ret
                        updates[f"fwd_{label}_return"] = ret
                        updates[f"fwd_{label}_date"] = actual_date.strftime("%Y-%m-%d")
                    else:
                        all_filled = False

                if updates:
                    # Determine outcome if we have at least 5d return
                    outcome = None
                    if "fwd_5d_return" in updates:
                        r5 = updates["fwd_5d_return"]
                        if r5 >= 3:
                            outcome = "STRONG_WIN"
                        elif r5 >= 1:
                            outcome = "WIN"
                        elif r5 >= -1:
                            outcome = "FLAT"
                        elif r5 >= -3:
                            outcome = "LOSS"
                        else:
                            outcome = "STRONG_LOSS"

                    set_clauses = []
                    params = []
                    for k, v in updates.items():
                        set_clauses.append(f"{k} = ?")
                        params.append(v)
                    if outcome:
                        set_clauses.append("outcome = ?")
                        params.append(outcome)
                    params.append(sig["id"])

                    conn.execute(
                        f"UPDATE signal_log SET {', '.join(set_clauses)} WHERE id = ?",
                        params
                    )
                    updated += 1

            time.sleep(0.2)  # rate limit
        except Exception as e:
            print(f"  [FWD] Error updating {ticker}: {e}")

    conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Sector comparison — rank ticker metrics against sector peers
# ---------------------------------------------------------------------------
def compute_sector_ranks(conn: sqlite3.Connection, ticker: str, sector: str,
                          today_str: str) -> Dict:
    """Compare a ticker's current metrics against its sector peers from today's data."""
    if sector in ("Unknown", "ETF"):
        return {"sector_skew_rank": 0, "sector_iv_rank": 0, "peer_count": 0, "peers": []}

    # Get all tickers in the same sector from today's readings
    peers_df = pd.read_sql_query("""
        SELECT ticker, skew, atm_iv, pc_ratio, spot_close
        FROM skew_daily
        WHERE date = ? AND sector = ? AND ticker != ?
    """, conn, params=(today_str, sector, ticker))

    if peers_df.empty or len(peers_df) < 2:
        return {"sector_skew_rank": 0, "sector_iv_rank": 0, "peer_count": 0, "peers": []}

    # Get this ticker's values
    this = pd.read_sql_query(
        "SELECT skew, atm_iv, pc_ratio FROM skew_daily WHERE date = ? AND ticker = ?",
        conn, params=(today_str, ticker)
    )
    if this.empty:
        return {"sector_skew_rank": 0, "sector_iv_rank": 0, "peer_count": 0, "peers": []}

    my_skew = float(this.iloc[0]["skew"])
    my_iv = float(this.iloc[0]["atm_iv"])

    # Percentile rank within sector (0 = lowest, 100 = highest)
    all_skew = list(peers_df["skew"]) + [my_skew]
    all_iv = list(peers_df["atm_iv"]) + [my_iv]
    skew_rank = round(sum(1 for x in all_skew if x <= my_skew) / len(all_skew) * 100, 1)
    iv_rank = round(sum(1 for x in all_iv if x <= my_iv) / len(all_iv) * 100, 1)

    # Find notable peers (most extreme skew in sector)
    peers_df_sorted = peers_df.sort_values("skew", ascending=False)
    notable = []
    for _, row in peers_df_sorted.head(3).iterrows():
        notable.append(f"{row['ticker']}(skew={row['skew']:+.1f})")

    return {
        "sector_skew_rank": skew_rank,
        "sector_iv_rank": iv_rank,
        "peer_count": len(peers_df),
        "peers": notable,
    }


# ---------------------------------------------------------------------------
# Pattern study — analyze historical signal effectiveness
# ---------------------------------------------------------------------------
def generate_pattern_report(conn: sqlite3.Connection) -> Optional[str]:
    """Analyze which metric combos historically predicted price moves.

    Groups signals by their characteristics and computes average forward returns.
    This is the 'what combos actually worked' analysis.
    """
    df = pd.read_sql_query("""
        SELECT * FROM signal_log
        WHERE fwd_5d_return IS NOT NULL
        ORDER BY signal_date
    """, conn)

    if len(df) < 5:
        return None  # need minimum data

    lines = []
    lines.append(f"\n{'='*90}")
    lines.append(f"  PATTERN STUDY — Signal Performance ({len(df)} signals with forward returns)")
    lines.append(f"{'='*90}\n")

    # 1. By signal type
    lines.append("  BY SIGNAL TYPE:")
    for sig_type in df["signal_type"].unique():
        subset = df[df["signal_type"] == sig_type]
        avg_1d = subset["fwd_1d_return"].mean() if "fwd_1d_return" in subset else 0
        avg_5d = subset["fwd_5d_return"].mean()
        avg_10d = subset["fwd_10d_return"].mean() if subset["fwd_10d_return"].notna().any() else 0
        win_rate = (subset["fwd_5d_return"] > 0).mean() * 100
        lines.append(f"    {sig_type:25s}  n={len(subset):3d}  "
                     f"1d={avg_1d:+5.2f}%  5d={avg_5d:+5.2f}%  10d={avg_10d:+5.2f}%  "
                     f"win_rate={win_rate:.0f}%")

    # 2. By conviction level
    lines.append("\n  BY CONVICTION:")
    for conv in ["HIGH", "MEDIUM", "LOW"]:
        subset = df[df["conviction"] == conv]
        if len(subset) == 0:
            continue
        avg_5d = subset["fwd_5d_return"].mean()
        win_rate = (subset["fwd_5d_return"] > 0).mean() * 100
        lines.append(f"    {conv:25s}  n={len(subset):3d}  "
                     f"5d={avg_5d:+5.2f}%  win_rate={win_rate:.0f}%")

    # 3. By IV surprise (did move exceed straddle pricing?)
    lines.append("\n  IV SURPRISE IMPACT:")
    for surprise in [1, 0]:
        label = "IV surprise (σ>1)" if surprise else "Normal move (σ<1)"
        subset = df[df["iv_surprise"] == surprise]
        if len(subset) == 0:
            continue
        avg_5d = subset["fwd_5d_return"].mean()
        win_rate = (subset["fwd_5d_return"] > 0).mean() * 100
        lines.append(f"    {label:25s}  n={len(subset):3d}  "
                     f"5d={avg_5d:+5.2f}%  win_rate={win_rate:.0f}%")

    # 4. Skew change magnitude buckets
    lines.append("\n  BY SKEW CHANGE MAGNITUDE:")
    df["skew_bucket"] = pd.cut(df["skew_change_5d"].abs(),
                                bins=[0, 0.5, 1, 2, 5, 100],
                                labels=["<0.5", "0.5-1", "1-2", "2-5", "5+"])
    for bucket in ["<0.5", "0.5-1", "1-2", "2-5", "5+"]:
        subset = df[df["skew_bucket"] == bucket]
        if len(subset) == 0:
            continue
        avg_5d = subset["fwd_5d_return"].mean()
        win_rate = (subset["fwd_5d_return"] > 0).mean() * 100
        lines.append(f"    skew_chg {bucket:6s}           n={len(subset):3d}  "
                     f"5d={avg_5d:+5.2f}%  win_rate={win_rate:.0f}%")

    # 5. Wall confirmation impact
    lines.append("\n  WALL CONFIRMATION IMPACT:")
    has_wall = df[df["wall_signal"].str.len() > 0] if "wall_signal" in df.columns else pd.DataFrame()
    no_wall = df[~df.index.isin(has_wall.index)] if not has_wall.empty else df
    if len(has_wall) > 0:
        lines.append(f"    With wall signal        n={len(has_wall):3d}  "
                     f"5d={has_wall['fwd_5d_return'].mean():+5.2f}%  "
                     f"win_rate={(has_wall['fwd_5d_return'] > 0).mean() * 100:.0f}%")
    if len(no_wall) > 0:
        lines.append(f"    Without wall signal     n={len(no_wall):3d}  "
                     f"5d={no_wall['fwd_5d_return'].mean():+5.2f}%  "
                     f"win_rate={(no_wall['fwd_5d_return'] > 0).mean() * 100:.0f}%")

    # 6. VWKS confirmation (did volume center of mass agree with signal?)
    if "vwks" in df.columns and df["vwks"].notna().any():
        lines.append("\n  VWKS CONFIRMATION (volume center of mass):")
        # For BULLISH signals, VWKS should be positive (confirming)
        # For BEARISH signals, VWKS should be negative (confirming)
        bullish = df[df["direction"] == "LONG"]
        bearish = df[df["direction"] == "SHORT"]
        if len(bullish) > 0:
            confirmed = bullish[bullish["vwks"] > 0]
            denied = bullish[bullish["vwks"] <= 0]
            if len(confirmed) > 0:
                lines.append(f"    BULL + VWKS confirms    n={len(confirmed):3d}  "
                             f"5d={confirmed['fwd_5d_return'].mean():+5.2f}%  "
                             f"win_rate={(confirmed['fwd_5d_return'] > 0).mean() * 100:.0f}%")
            if len(denied) > 0:
                lines.append(f"    BULL + VWKS denies      n={len(denied):3d}  "
                             f"5d={denied['fwd_5d_return'].mean():+5.2f}%  "
                             f"win_rate={(denied['fwd_5d_return'] > 0).mean() * 100:.0f}%")
        if len(bearish) > 0:
            confirmed = bearish[bearish["vwks"] < 0]
            denied = bearish[bearish["vwks"] >= 0]
            if len(confirmed) > 0:
                lines.append(f"    BEAR + VWKS confirms    n={len(confirmed):3d}  "
                             f"5d={confirmed['fwd_5d_return'].mean():+5.2f}%  "
                             f"win_rate={(confirmed['fwd_5d_return'] > 0).mean() * 100:.0f}%")
            if len(denied) > 0:
                lines.append(f"    BEAR + VWKS denies      n={len(denied):3d}  "
                             f"5d={denied['fwd_5d_return'].mean():+5.2f}%  "
                             f"win_rate={(denied['fwd_5d_return'] > 0).mean() * 100:.0f}%")

    # 7. By sector
    if "sector" in df.columns and df["sector"].notna().any():
        lines.append("\n  BY SECTOR:")
        for sector in df["sector"].dropna().unique():
            if sector in ("Unknown", ""):
                continue
            subset = df[df["sector"] == sector]
            if len(subset) < 2:
                continue
            avg_5d = subset["fwd_5d_return"].mean()
            win_rate = (subset["fwd_5d_return"] > 0).mean() * 100
            lines.append(f"    {sector:25s}  n={len(subset):3d}  "
                         f"5d={avg_5d:+5.2f}%  win_rate={win_rate:.0f}%")

    # 7. Best and worst individual signals
    lines.append("\n  TOP 5 BEST SIGNALS:")
    best = df.nlargest(5, "fwd_5d_return")
    for _, row in best.iterrows():
        lines.append(f"    {row['signal_date']}  {row['ticker']:6s}  {row['signal_type']:22s}  "
                     f"σ={row['sigma_iv']:+.2f}  skewΔ={row['skew_change_5d']:+.1f}  "
                     f"→ 5d={row['fwd_5d_return']:+.2f}%")

    lines.append("\n  TOP 5 WORST SIGNALS:")
    worst = df.nsmallest(5, "fwd_5d_return")
    for _, row in worst.iterrows():
        lines.append(f"    {row['signal_date']}  {row['ticker']:6s}  {row['signal_type']:22s}  "
                     f"σ={row['sigma_iv']:+.2f}  skewΔ={row['skew_change_5d']:+.1f}  "
                     f"→ 5d={row['fwd_5d_return']:+.2f}%")

    lines.append(f"\n{'='*90}\n")
    return "\n".join(lines)


def get_history(conn: sqlite3.Connection, ticker: str, days: int = 10) -> pd.DataFrame:
    """Last `days` readings for a ticker — bounded by DATE, not just row count.

    GAP GUARD (added 2026-07-20). This used to be a pure `LIMIT ?` on rows, so a
    collection outage (the scanner was dead 6/27-7/17) silently returned rows weeks
    apart: [7/20, 6/26, 6/25, ...]. compute_divergence() takes "skew N days ago" as a
    ROW offset, so skew_change_5d would have compared today's skew against a JUNE
    reading and reported it as a 5-day collapse — manufacturing false Tier A signals
    out of three weeks of drift. Now rows far older than the requested window are
    dropped, so a gap yields too-little history and the signal is simply not produced.
    """
    df = pd.read_sql_query(
        "SELECT * FROM skew_daily WHERE ticker = ? ORDER BY date DESC LIMIT ?",
        conn, params=(ticker, days)
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        # `days` is TRADING days; allow ~1.5x calendar slack + holidays, measured from
        # the newest row so this works for both live scans and historical backfills.
        max_span = pd.Timedelta(days=int(days * 1.5) + 4)
        df = df[df["date"] >= (df["date"].max() - max_span)]
        df = df.sort_values("date")
    return df


# ---------------------------------------------------------------------------
# Skew calculation (mirrors orca.py get_skew_25d logic)
# ---------------------------------------------------------------------------
def compute_skew_snapshot(ticker: str) -> Optional[Dict]:
    """Fetch live options chain and compute 25-delta skew for a ticker."""
    try:
        tk = yf.Ticker(ticker)
        # Get spot price
        price = 0.0
        try:
            price = float(tk.fast_info.last_price)
        except Exception:
            pass
        if price <= 0:
            hist_p = tk.history(period="5d")
            if hist_p.empty:
                return None
            price = float(hist_p["Close"].iloc[-1])

        # Pick expiration in DTE window
        expirations = tk.options
        if not expirations:
            return None

        today = dt.date.today()
        best_exp = None
        best_dte = None
        for exp_str in expirations:
            exp_date = dt.datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if DTE_MIN <= dte <= DTE_MAX:
                if best_dte is None or dte < best_dte:
                    best_exp = exp_str
                    best_dte = dte

        # Fallback: nearest expiration >= 7 DTE
        if best_exp is None:
            for exp_str in expirations:
                exp_date = dt.datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if dte >= 7:
                    best_exp = exp_str
                    best_dte = dte
                    break

        if best_exp is None or best_dte is None:
            return None

        chain = tk.option_chain(best_exp)
        calls = chain.calls
        puts = chain.puts

        # ATM IV (nearest strike to spot) — reset_index to avoid duplicate idx
        all_strikes = pd.concat([calls[["strike", "impliedVolatility"]],
                                  puts[["strike", "impliedVolatility"]]],
                                 ignore_index=True)
        all_strikes = all_strikes[all_strikes["impliedVolatility"] > 0]
        if all_strikes.empty:
            return None

        atm_idx = (all_strikes["strike"] - price).abs().idxmin()
        atm_iv = float(all_strikes.loc[atm_idx, "impliedVolatility"]) * 100

        # 25-delta strikes
        iv_dec = atm_iv / 100
        t = max(best_dte, 1) / 365.0
        offset = 0.675 * iv_dec * np.sqrt(t)  # norm.ppf(0.75) ≈ 0.675

        put_25d_strike = price * np.exp(-offset)
        call_25d_strike = price * np.exp(offset)

        # Interpolate IV at 25D put — filter out stale/bad IV (< 50% of ATM)
        min_iv = iv_dec * 0.5  # in decimal
        put_25d_iv = 0.0
        puts_valid = puts[(puts["impliedVolatility"] > min_iv) &
                          (puts["impliedVolatility"] > 0)]
        if not puts_valid.empty:
            idx = (puts_valid["strike"] - put_25d_strike).abs().idxmin()
            put_25d_iv = float(puts_valid.loc[idx, "impliedVolatility"]) * 100

        # Interpolate IV at 25D call
        call_25d_iv = 0.0
        calls_valid = calls[(calls["impliedVolatility"] > min_iv) &
                            (calls["impliedVolatility"] > 0)]
        if not calls_valid.empty:
            idx = (calls_valid["strike"] - call_25d_strike).abs().idxmin()
            call_25d_iv = float(calls_valid.loc[idx, "impliedVolatility"]) * 100

        skew = round(put_25d_iv - call_25d_iv, 2) if put_25d_iv > 0 and call_25d_iv > 0 else 0
        skew_ratio = round(put_25d_iv / call_25d_iv, 3) if call_25d_iv > 0 else 1.0

        # Put/Call volume ratio from the chain
        put_vol = puts["volume"].sum() if "volume" in puts.columns else 0
        call_vol = calls["volume"].sum() if "volume" in calls.columns else 0
        pc_ratio = round(put_vol / max(call_vol, 1), 3)

        # --- Near-Term Chain (3-7 DTE) — tactical/gamma view ---
        near_exp = None
        near_exp_dte = None
        for exp_str in expirations:
            exp_date = dt.datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if 3 <= dte <= 10:
                near_exp = exp_str
                near_exp_dte = dte
                break

        near_skew = 0.0
        near_pc_ratio = 0.0
        near_atm_iv = 0.0
        skew_term_structure = ""

        if near_exp and near_exp != best_exp:
            try:
                near_chain = tk.option_chain(near_exp)
                n_calls = near_chain.calls
                n_puts = near_chain.puts

                # Near-term ATM IV
                n_all = pd.concat([n_calls[["strike", "impliedVolatility"]],
                                   n_puts[["strike", "impliedVolatility"]]],
                                  ignore_index=True)
                n_all = n_all[n_all["impliedVolatility"] > 0]
                if not n_all.empty:
                    n_atm_idx = (n_all["strike"] - price).abs().idxmin()
                    near_atm_iv = round(float(n_all.loc[n_atm_idx, "impliedVolatility"]) * 100, 2)

                # Near-term 25-delta skew
                if near_atm_iv > 0:
                    n_iv_dec = near_atm_iv / 100
                    n_t = max(near_exp_dte, 1) / 365.0
                    n_offset = 0.675 * n_iv_dec * np.sqrt(n_t)
                    n_put_25d_strike = price * np.exp(-n_offset)
                    n_call_25d_strike = price * np.exp(n_offset)

                    n_min_iv = n_iv_dec * 0.5
                    n_puts_valid = n_puts[(n_puts["impliedVolatility"] > n_min_iv) &
                                          (n_puts["impliedVolatility"] > 0)]
                    n_calls_valid = n_calls[(n_calls["impliedVolatility"] > n_min_iv) &
                                            (n_calls["impliedVolatility"] > 0)]
                    n_put_iv = 0.0
                    n_call_iv = 0.0
                    if not n_puts_valid.empty:
                        idx = (n_puts_valid["strike"] - n_put_25d_strike).abs().idxmin()
                        n_put_iv = float(n_puts_valid.loc[idx, "impliedVolatility"]) * 100
                    if not n_calls_valid.empty:
                        idx = (n_calls_valid["strike"] - n_call_25d_strike).abs().idxmin()
                        n_call_iv = float(n_calls_valid.loc[idx, "impliedVolatility"]) * 100
                    if n_put_iv > 0 and n_call_iv > 0:
                        near_skew = round(n_put_iv - n_call_iv, 2)

                # Near-term P/C ratio
                n_put_vol = n_puts["volume"].fillna(0).sum()
                n_call_vol = n_calls["volume"].fillna(0).sum()
                near_pc_ratio = round(n_put_vol / max(n_call_vol, 1), 3)

                # Term structure signal
                # Compare near-term skew (gamma/tactical) vs far-term skew (structural)
                skew_diff = near_skew - skew
                if abs(skew_diff) > 1.0:
                    if near_skew > skew + 1.0:
                        skew_term_structure = "NEAR_STEEP"  # short-term fear > structural
                    elif near_skew < skew - 1.0:
                        skew_term_structure = "NEAR_FLAT"   # short-term calm vs structural fear
                else:
                    skew_term_structure = "ALIGNED"
            except Exception:
                pass

        # VWKS — Volume-Weighted Strike-Spot Ratio (Bernile, Gao & Hu 2019)
        # VWKS = Σ(Vi × (Ki/S - 1)) / Σ(Vi) across ALL contracts, ALL expirations
        # Paper uses unsigned volume across all strikes and all expirations
        # High VWKS = volume tilted toward high strikes (OTM calls) = bullish
        # Low VWKS = volume tilted toward low strikes (OTM puts) = bearish
        vwks = 0.0
        total_vwks_vol = 0
        try:
            for exp_str in expirations:
                exp_d = dt.datetime.strptime(exp_str, "%Y-%m-%d").date()
                if (exp_d - today).days < 1 or (exp_d - today).days > 90:
                    continue  # skip expired and very far out
                try:
                    ch = tk.option_chain(exp_str)
                except Exception:
                    continue
                for side_df in [ch.calls, ch.puts]:
                    if "volume" not in side_df.columns:
                        continue
                    for _, row in side_df.iterrows():
                        raw_v = row.get("volume", 0)
                        if pd.isna(raw_v) or raw_v is None:
                            continue
                        v = int(raw_v)
                        if v > 0 and price > 0:
                            vwks += v * (row["strike"] / price - 1)
                            total_vwks_vol += v
        except Exception:
            pass
        if total_vwks_vol > 0:
            vwks = vwks / total_vwks_vol

        # --- Fixed-Strike Vol: Put Wall & Call Wall ---
        # Put wall = highest OI put strike (where dealers are short puts = support)
        # Call wall = highest OI call strike (where dealers are short calls = resistance)
        put_wall_strike, put_wall_iv, put_wall_oi = 0.0, 0.0, 0
        call_wall_strike, call_wall_iv, call_wall_oi = 0.0, 0.0, 0
        fixed_strikes = []

        if "openInterest" in puts.columns and not puts.empty:
            puts_oi = puts[puts["openInterest"] > 0].copy()
            if not puts_oi.empty:
                pw_idx = puts_oi["openInterest"].idxmax()
                put_wall_strike = float(puts_oi.loc[pw_idx, "strike"])
                put_wall_iv = float(puts_oi.loc[pw_idx, "impliedVolatility"]) * 100
                put_wall_oi = int(puts_oi.loc[pw_idx, "openInterest"]) if pd.notna(puts_oi.loc[pw_idx, "openInterest"]) else 0
                # Save top 3 put strikes by OI for fixed-strike tracking
                top_puts = puts_oi.nlargest(3, "openInterest")
                for _, row in top_puts.iterrows():
                    fixed_strikes.append({
                        "strike": float(row["strike"]),
                        "side": "PUT",
                        "iv": round(float(row["impliedVolatility"]) * 100, 2) if pd.notna(row.get("impliedVolatility")) else 0.0,
                        "oi": int(row["openInterest"]) if pd.notna(row.get("openInterest")) else 0,
                        "volume": int(row["volume"]) if pd.notna(row.get("volume")) else 0,
                    })

        if "openInterest" in calls.columns and not calls.empty:
            calls_oi = calls[calls["openInterest"] > 0].copy()
            if not calls_oi.empty:
                cw_idx = calls_oi["openInterest"].idxmax()
                call_wall_strike = float(calls_oi.loc[cw_idx, "strike"])
                call_wall_iv = float(calls_oi.loc[cw_idx, "impliedVolatility"]) * 100
                call_wall_oi = int(calls_oi.loc[cw_idx, "openInterest"]) if pd.notna(calls_oi.loc[cw_idx, "openInterest"]) else 0
                # Save top 3 call strikes by OI
                top_calls = calls_oi.nlargest(3, "openInterest")
                for _, row in top_calls.iterrows():
                    fixed_strikes.append({
                        "strike": float(row["strike"]),
                        "side": "CALL",
                        "iv": round(float(row["impliedVolatility"]) * 100, 2) if pd.notna(row.get("impliedVolatility")) else 0.0,
                        "oi": int(row["openInterest"]) if pd.notna(row.get("openInterest")) else 0,
                        "volume": int(row["volume"]) if pd.notna(row.get("volume")) else 0,
                    })

        # 10-day HV from price history (Yang-Zhang estimator — uses OHLC + overnight gaps)
        hist = tk.history(period="60d")
        hv_10d = 0.0
        stock_volume = 0
        if len(hist) >= 2:
            # Latest bar volume
            vol_val = hist["Volume"].iloc[-1] if "Volume" in hist.columns else 0
            stock_volume = int(vol_val) if pd.notna(vol_val) else 0
        if len(hist) >= 12:
            hv_10d = round(_yang_zhang_vol(hist, window=10), 2)

        # --- Per-Ticker GEX (Gamma Exposure) ---
        # GEX = sum(gamma × OI × 100 × spot) for calls (positive)
        #      - sum(gamma × OI × 100 × spot) for puts (negative)
        # Gamma computed via Black-Scholes: gamma = N'(d1) / (S × σ × √T)
        from scipy.stats import norm as _norm
        gex_total = 0.0
        try:
            t_years = max(best_dte, 1) / 365.0
            sqrt_t = np.sqrt(t_years)
            r = 0.043  # risk-free rate

            for side_df, sign in [(calls, 1.0), (puts, -1.0)]:
                if side_df.empty or "openInterest" not in side_df.columns:
                    continue
                for _, row in side_df.iterrows():
                    oi = row.get("openInterest", 0)
                    iv_raw = row.get("impliedVolatility", 0)
                    strike = row.get("strike", 0)
                    if not oi or oi <= 0 or iv_raw <= 0 or strike <= 0:
                        continue
                    d1 = (np.log(price / strike) + (r + 0.5 * iv_raw**2) * t_years) / (iv_raw * sqrt_t)
                    gamma = _norm.pdf(d1) / (price * iv_raw * sqrt_t)
                    gex_total += sign * gamma * oi * 100 * price
        except Exception:
            pass
        gex_total = round(gex_total, 2)

        # GEX / Volume ratio
        gex_to_volume = None
        if stock_volume and stock_volume > 0 and gex_total != 0:
            gex_to_volume = round(gex_total / stock_volume, 4)

        # --- Total Options OI across all strikes in this chain ---
        total_options_oi = 0
        for side_df in [puts, calls]:
            if "openInterest" in side_df.columns:
                total_options_oi += int(side_df["openInterest"].fillna(0).sum())

        # --- Short Interest from yfinance ticker.info ---
        # NOTE: tk.info makes a separate API call — skip on rate limit errors
        short_interest = None
        short_ratio = None
        short_pct_float = None
        try:
            time.sleep(0.3)  # extra delay before info call
            info = tk.info
            si = info.get("sharesShort")
            if si is not None and si > 0:
                short_interest = int(si)
            sr = info.get("shortRatio")
            if sr is not None:
                short_ratio = float(sr)
            spf = info.get("shortPercentOfFloat")
            if spf is not None:
                short_pct_float = round(float(spf) * 100, 2)  # convert to percentage
        except Exception:
            pass  # short interest is optional — don't fail the whole ticker

        return {
            "spot_close": round(price, 2),
            "atm_iv": round(atm_iv, 2),
            "put_25d_iv": round(put_25d_iv, 2),
            "call_25d_iv": round(call_25d_iv, 2),
            "skew": skew,
            "skew_ratio": skew_ratio,
            "hv_10d": hv_10d,
            "pc_ratio": pc_ratio,
            "put_vol": int(put_vol) if pd.notna(put_vol) else 0,
            "call_vol": int(call_vol) if pd.notna(call_vol) else 0,
            "put_wall_strike": round(put_wall_strike, 2),
            "put_wall_iv": round(put_wall_iv, 2),
            "put_wall_oi": put_wall_oi,
            "call_wall_strike": round(call_wall_strike, 2),
            "call_wall_iv": round(call_wall_iv, 2),
            "call_wall_oi": call_wall_oi,
            "fixed_strikes": fixed_strikes,
            "dte": best_dte,
            "expiration": best_exp,
            "put_25d_strike": round(put_25d_strike, 2),
            "call_25d_strike": round(call_25d_strike, 2),
            "vwks": round(vwks, 6),
            "vwks_volume": total_vwks_vol,
            "near_skew": near_skew,
            "near_pc_ratio": near_pc_ratio,
            "near_atm_iv": near_atm_iv,
            "near_dte": near_exp_dte,
            "skew_term_structure": skew_term_structure,
            "stock_volume": stock_volume,
            "gex": gex_total,
            "gex_to_volume": gex_to_volume,
            "total_options_oi": total_options_oi,
            "short_interest": short_interest,
            "short_ratio": short_ratio,
            "short_pct_float": short_pct_float,
        }

    except Exception as e:
        print(f"  [{ticker}] Error: {e}")
        return None


# ---------------------------------------------------------------------------
# CBOE-style VIX calculation for individual tickers
# Adapts the variance-swap formula from the CBOE VIX white paper.
# Interpolates between two expirations bracketing 30 calendar days.
# ---------------------------------------------------------------------------
RISK_FREE_RATE = 0.043  # ~4.3% fed funds, updated periodically
MINUTES_IN_YEAR = 525600
MINUTES_IN_DAY = 1440
TARGET_DAYS = 30
N30 = TARGET_DAYS * MINUTES_IN_DAY  # 43200


def _select_vix_options(chain_df: pd.DataFrame, side: str, k0: float) -> pd.DataFrame:
    """Select OTM options per CBOE rules: stop after two consecutive zero bids.
    Falls back to lastPrice when bid is unavailable (after hours)."""
    if side == "put":
        opts = chain_df[chain_df["strike"] < k0].sort_values("strike", ascending=False).copy()
    else:
        opts = chain_df[chain_df["strike"] > k0].sort_values("strike", ascending=True).copy()

    # Check if we have live bid/ask data (market hours) or need lastPrice fallback
    has_bids = (opts["bid"] > 0).any() if "bid" in opts.columns else False

    selected = []
    zero_count = 0
    for _, row in opts.iterrows():
        if has_bids:
            bid = row.get("bid", 0)
            if pd.isna(bid) or bid <= 0:
                zero_count += 1
                if zero_count >= 2:
                    break
            else:
                zero_count = 0
                selected.append(row)
        else:
            # After hours: use lastPrice, skip zeros
            last = row.get("lastPrice", 0)
            if pd.isna(last) or last <= 0:
                zero_count += 1
                if zero_count >= 2:
                    break
            else:
                zero_count = 0
                selected.append(row)

    return pd.DataFrame(selected) if selected else pd.DataFrame()


def compute_ticker_vix(ticker: str) -> Optional[Dict]:
    """Calculate CBOE-style 30-day implied volatility for any ticker.

    Steps (per CBOE white paper):
    1. Find two expirations bracketing 30 days (near-term >23d, next-term <37d)
       For weeklies, we relax to >7d and <60d if needed, then interpolate.
    2. For each expiration: compute forward price, select OTM options, sum variance contributions
    3. Interpolate to get 30-day variance, take sqrt × 100 = ticker VIX
    4. Also compute 30-day HV (realized vol) for IV/HV ratio

    Returns dict with: ticker_vix, near_term_iv, next_term_iv, hv_10d, iv_hv_ratio, etc.
    """
    try:
        tk = yf.Ticker(ticker)

        # Get spot price
        price = 0.0
        try:
            price = float(tk.fast_info.last_price)
        except Exception:
            pass
        if price <= 0:
            hist_p = tk.history(period="5d")
            if hist_p.empty:
                return None
            price = float(hist_p["Close"].iloc[-1])

        expirations = tk.options
        if not expirations:
            return None

        today = dt.date.today()
        now_minutes = dt.datetime.now()

        # Find near-term (>7d) and next-term that bracket 30 days
        exp_list = []
        for exp_str in expirations:
            exp_date = dt.datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if dte >= 7:
                exp_list.append((exp_str, exp_date, dte))

        if len(exp_list) < 2:
            return None

        # Find pair that brackets 30 days
        near_exp = None
        next_exp = None
        for i in range(len(exp_list) - 1):
            if exp_list[i][2] <= TARGET_DAYS and exp_list[i + 1][2] >= TARGET_DAYS:
                near_exp = exp_list[i]
                next_exp = exp_list[i + 1]
                break

        # If no perfect bracket, use closest pair
        if near_exp is None:
            if exp_list[0][2] >= TARGET_DAYS:
                # All expirations are beyond 30 days — use first two
                near_exp = exp_list[0]
                next_exp = exp_list[1] if len(exp_list) > 1 else exp_list[0]
            else:
                # Find the transition point
                for i in range(len(exp_list)):
                    if exp_list[i][2] >= TARGET_DAYS:
                        near_exp = exp_list[max(0, i - 1)]
                        next_exp = exp_list[i]
                        break
                if near_exp is None:
                    near_exp = exp_list[-2] if len(exp_list) >= 2 else exp_list[-1]
                    next_exp = exp_list[-1]

        def calc_sigma_sq(exp_info):
            """Calculate σ² for one expiration per CBOE formula."""
            exp_str, exp_date, dte = exp_info

            # Time to expiration in years (using minutes for precision)
            minutes_remaining = dte * MINUTES_IN_DAY
            T = minutes_remaining / MINUTES_IN_YEAR
            if T <= 0:
                return None, None, None

            R = RISK_FREE_RATE

            chain = tk.option_chain(exp_str)
            calls = chain.calls.copy()
            puts = chain.puts.copy()

            if calls.empty or puts.empty:
                return None, None, None

            # Step 1: Find forward price F
            # F = Strike + e^(RT) × (Call - Put) at the strike where |Call - Put| is smallest
            merged = calls[["strike", "lastPrice", "bid", "ask"]].merge(
                puts[["strike", "lastPrice", "bid", "ask"]],
                on="strike", suffixes=("_call", "_put")
            )
            if merged.empty:
                return None, None, None

            # Use mid-quotes where available, fall back to lastPrice
            def get_mid(bid, ask, last):
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
                elif last > 0:
                    return last
                return 0
            merged["call_mid"] = merged.apply(
                lambda r: get_mid(r["bid_call"], r["ask_call"], r["lastPrice_call"]), axis=1)
            merged["put_mid"] = merged.apply(
                lambda r: get_mid(r["bid_put"], r["ask_put"], r["lastPrice_put"]), axis=1)

            merged["diff"] = (merged["call_mid"] - merged["put_mid"]).abs()
            # Only consider strikes near the money
            near_money = merged[(merged["strike"] > price * 0.8) & (merged["strike"] < price * 1.2)]
            if near_money.empty:
                near_money = merged

            f_row = near_money.loc[near_money["diff"].idxmin()]
            F = f_row["strike"] + np.exp(R * T) * (f_row["call_mid"] - f_row["put_mid"])

            # K0 = strike immediately below F
            all_strikes = sorted(merged["strike"].unique())
            K0 = max([k for k in all_strikes if k <= F], default=all_strikes[0])

            # Step 2: Select OTM options
            otm_puts = _select_vix_options(puts, "put", K0)
            otm_calls = _select_vix_options(calls, "call", K0)

            # At K0: average put and call
            k0_call = calls[calls["strike"] == K0]
            k0_put = puts[puts["strike"] == K0]

            # Build the full strip
            contributions = []

            def _get_option_mid(row):
                """Get mid-quote, falling back to lastPrice after hours."""
                b, a = row.get("bid", 0), row.get("ask", 0)
                if b > 0 and a > 0:
                    return (b + a) / 2
                lp = row.get("lastPrice", 0)
                return lp if lp > 0 else 0

            # Add OTM puts (sorted ascending for ΔK calc)
            if not otm_puts.empty:
                otm_puts = otm_puts.sort_values("strike")
                for idx, (_, row) in enumerate(otm_puts.iterrows()):
                    mid = _get_option_mid(row)
                    if mid <= 0:
                        continue
                    contributions.append({"strike": row["strike"], "mid": mid, "type": "put"})

            # Add K0 (average of put and call)
            k0_mid = 0
            if not k0_call.empty and not k0_put.empty:
                c_mid = _get_option_mid(k0_call.iloc[0])
                p_mid = _get_option_mid(k0_put.iloc[0])
                k0_mid = (c_mid + p_mid) / 2
                if k0_mid > 0:
                    contributions.append({"strike": K0, "mid": k0_mid, "type": "k0"})

            # Add OTM calls
            if not otm_calls.empty:
                otm_calls = otm_calls.sort_values("strike")
                for _, row in otm_calls.iterrows():
                    mid = _get_option_mid(row)
                    if mid <= 0:
                        continue
                    contributions.append({"strike": row["strike"], "mid": mid, "type": "call"})

            if len(contributions) < 3:
                return None, None, None

            # Sort by strike
            contributions.sort(key=lambda x: x["strike"])

            # Calculate sum: Σ (ΔKi / Ki²) × e^(RT) × Q(Ki)
            total = 0.0
            for i, c in enumerate(contributions):
                Ki = c["strike"]
                Qi = c["mid"]

                # ΔK: half the distance between adjacent strikes
                if i == 0:
                    dK = contributions[1]["strike"] - Ki
                elif i == len(contributions) - 1:
                    dK = Ki - contributions[-2]["strike"]
                else:
                    dK = (contributions[i + 1]["strike"] - contributions[i - 1]["strike"]) / 2

                contribution = (dK / (Ki ** 2)) * np.exp(R * T) * Qi
                total += contribution

            # σ² = (2/T) × total - (1/T) × (F/K0 - 1)²
            sigma_sq = (2 / T) * total - (1 / T) * ((F / K0 - 1) ** 2)

            return sigma_sq, T, len(contributions)

        # Calculate σ² for both terms
        sig1, T1, n1 = calc_sigma_sq(near_exp) or (None, None, None)
        sig2, T2, n2 = calc_sigma_sq(next_exp) or (None, None, None)

        if sig1 is None and sig2 is None:
            return None

        # If only one term available, use it directly
        if sig1 is None:
            vix_30d = np.sqrt(max(sig2, 0)) * 100
            ticker_vix = round(vix_30d, 2)
        elif sig2 is None:
            vix_30d = np.sqrt(max(sig1, 0)) * 100
            ticker_vix = round(vix_30d, 2)
        else:
            # Step 3: Interpolate to 30-day variance
            # VIX = 100 × sqrt{ [T1×σ1²×(NT2-N30)/(NT2-NT1) + T2×σ2²×(N30-NT1)/(NT2-NT1)] × N365/N30 }
            NT1 = T1 * MINUTES_IN_YEAR
            NT2 = T2 * MINUTES_IN_YEAR

            if NT2 - NT1 <= 0:
                # Same expiration — just use one
                vix_30d = np.sqrt(max(sig1, 0)) * 100
            else:
                w1 = T1 * sig1 * (NT2 - N30) / (NT2 - NT1)
                w2 = T2 * sig2 * (N30 - NT1) / (NT2 - NT1)
                variance_30d = (w1 + w2) * MINUTES_IN_YEAR / N30
                vix_30d = np.sqrt(max(variance_30d, 0)) * 100

            ticker_vix = round(vix_30d, 2)

        # 10-day HV (realized vol) — Yang-Zhang estimator (OHLC + overnight gaps)
        hist = tk.history(period="60d")
        hv_10d = 0.0
        if len(hist) >= 12:
            hv_10d = round(_yang_zhang_vol(hist, window=10), 2)

        iv_hv_ratio = round(ticker_vix / max(hv_10d, 0.01), 2) if hv_10d > 0 else 0

        # Days to earnings
        dte_earnings = None
        try:
            cal = tk.calendar
            if cal is not None and not cal.empty:
                # calendar may be a DataFrame with 'Earnings Date' row or columns
                if isinstance(cal, pd.DataFrame):
                    if "Earnings Date" in cal.index:
                        earn_dates = cal.loc["Earnings Date"]
                        if len(earn_dates) > 0:
                            next_earn = pd.to_datetime(earn_dates.iloc[0])
                            if next_earn.date() >= today:
                                dte_earnings = (next_earn.date() - today).days
                    elif "Earnings Date" in cal.columns:
                        earn_dates = cal["Earnings Date"]
                        for ed in earn_dates:
                            ed_date = pd.to_datetime(ed).date()
                            if ed_date >= today:
                                dte_earnings = (ed_date - today).days
                                break
        except Exception:
            pass

        # Also try .earnings_dates for more reliable data
        # Suppress yfinance "possibly delisted" warnings (normal for ETFs)
        if dte_earnings is None:
            try:
                import io, contextlib
                with contextlib.redirect_stderr(io.StringIO()):
                    earn_df = tk.earnings_dates
                if earn_df is not None and not earn_df.empty:
                    future_dates = [d for d in earn_df.index if d.date() >= today]
                    if future_dates:
                        dte_earnings = (min(future_dates).date() - today).days
            except Exception:
                pass

        return {
            "ticker_vix": ticker_vix,
            "near_iv": round(np.sqrt(max(sig1, 0)) * 100, 2) if sig1 is not None else 0,
            "next_iv": round(np.sqrt(max(sig2, 0)) * 100, 2) if sig2 is not None else 0,
            "near_dte": near_exp[2] if near_exp else 0,
            "next_dte": next_exp[2] if next_exp else 0,
            "near_n_opts": n1 or 0,
            "next_n_opts": n2 or 0,
            "hv_10d": hv_10d,
            "iv_hv_ratio": iv_hv_ratio,
            "dte_earnings": dte_earnings,
            "spot": round(price, 2),
        }

    except Exception as e:
        print(f"  [{ticker}] VIX calc error: {e}")
        return None


# ---------------------------------------------------------------------------
# Fixed-strike vol & wall rolling analysis
# ---------------------------------------------------------------------------
def analyze_walls(history: pd.DataFrame) -> Optional[Dict]:
    """
    Analyze put/call wall behavior over time.

    Key signals:
      - Put wall NOT rolling lower after selloff → supportive (IBM example)
      - Put wall rolling lower with price → continuation, more downside expected
      - Call wall IV crushing as price approaches → resistance holds (AAPL $280 example)
      - Call wall IV rising as price approaches → breakout demand, resistance may break

    Also tracks fixed-strike vol: is IV at the wall strike rising or falling?
    """
    if len(history) < 2:
        return None

    current = history.iloc[-1]
    oldest = history.iloc[0]

    # Need wall data
    if "put_wall_strike" not in current.index or "call_wall_strike" not in current.index:
        return None

    pw_now = current.get("put_wall_strike", 0)
    pw_then = oldest.get("put_wall_strike", 0)
    pw_iv_now = current.get("put_wall_iv", 0)
    pw_iv_then = oldest.get("put_wall_iv", 0)

    cw_now = current.get("call_wall_strike", 0)
    cw_then = oldest.get("call_wall_strike", 0)
    cw_iv_now = current.get("call_wall_iv", 0)
    cw_iv_then = oldest.get("call_wall_iv", 0)

    spot_now = current.get("spot_close", 0)

    result = {
        "put_wall_now": pw_now,
        "put_wall_then": pw_then,
        "put_wall_rolled": False,
        "put_wall_direction": "HOLDING",
        "put_wall_iv_change": round(pw_iv_now - pw_iv_then, 2) if pw_iv_now > 0 and pw_iv_then > 0 else 0,
        "call_wall_now": cw_now,
        "call_wall_then": cw_then,
        "call_wall_rolled": False,
        "call_wall_direction": "HOLDING",
        "call_wall_iv_change": round(cw_iv_now - cw_iv_then, 2) if cw_iv_now > 0 and cw_iv_then > 0 else 0,
        "wall_signal": "",
    }

    # Did the put wall roll?
    if pw_now > 0 and pw_then > 0:
        pw_pct_change = (pw_now - pw_then) / pw_then * 100
        if pw_pct_change < -2:  # wall moved down > 2%
            result["put_wall_rolled"] = True
            result["put_wall_direction"] = "ROLLING LOWER"
        elif pw_pct_change > 2:
            result["put_wall_rolled"] = True
            result["put_wall_direction"] = "ROLLING HIGHER"

    # Did the call wall roll?
    if cw_now > 0 and cw_then > 0:
        cw_pct_change = (cw_now - cw_then) / cw_then * 100
        if cw_pct_change > 2:
            result["call_wall_rolled"] = True
            result["call_wall_direction"] = "ROLLING HIGHER"
        elif cw_pct_change < -2:
            result["call_wall_rolled"] = True
            result["call_wall_direction"] = "ROLLING LOWER"

    # Generate wall signals
    signals = []

    # Spot falling but put wall not rolling lower = SUPPORTIVE
    spot_change = (spot_now - oldest.get("spot_close", spot_now)) / max(oldest.get("spot_close", 1), 1) * 100
    if spot_change < -2 and result["put_wall_direction"] == "HOLDING":
        signals.append("PUT WALL HOLDING: stock down but put wall hasn't rolled lower — options landscape not shifting with price (supportive)")
    elif spot_change < -2 and result["put_wall_direction"] == "ROLLING LOWER":
        signals.append("PUT WALL ROLLING LOWER: puts being monetized and rolled down — more downside expected")

    # Call wall IV behavior as price approaches
    if cw_now > 0 and spot_now > 0:
        distance_to_call_wall = (cw_now - spot_now) / spot_now * 100
        if distance_to_call_wall < 3 and distance_to_call_wall > 0:  # within 3% of call wall
            if result["call_wall_iv_change"] < -1:
                signals.append(f"CALL WALL VOL CRUSHING: approaching ${cw_now:.0f} resistance but call IV dropping — no breakout demand, resistance likely holds")
            elif result["call_wall_iv_change"] > 1:
                signals.append(f"CALL WALL VOL RISING: approaching ${cw_now:.0f} resistance with call IV rising — breakout demand building, resistance may break")

    # Put wall IV behavior (fixed-strike vol at support)
    if pw_now > 0 and spot_now > 0:
        distance_to_put_wall = (spot_now - pw_now) / spot_now * 100
        if distance_to_put_wall < 3 and distance_to_put_wall > 0:  # within 3% of put wall
            if result["put_wall_iv_change"] < -1:
                signals.append(f"PUT WALL VOL DROPPING: near ${pw_now:.0f} support and put IV dropping — protection being unwound, support likely holds")
            elif result["put_wall_iv_change"] > 1:
                signals.append(f"PUT WALL VOL RISING: near ${pw_now:.0f} support and put IV rising — more protection being bought, support may break")

    result["wall_signal"] = " | ".join(signals) if signals else ""
    return result


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------
def compute_divergence(history: pd.DataFrame) -> Optional[Dict]:
    """
    Compare 5-day skew change vs 1-week sigma move in spot.

    Sigma move = (price change %) / (daily HV * sqrt(days))
    Skew change = current skew - skew N days ago

    Divergence types:
      BEARISH_CONTINUATION: spot down + skew up (smart money buying protection)
      BULLISH_REVERSAL:     spot down + skew flat/down (no fear, likely bounce)
      BULLISH_CONTINUATION: spot up + skew flat/down (no hedging, trend healthy)
      BEARISH_REVERSAL:     spot up + skew up (hedging the rally, skepticism)
    """
    if len(history) < 2:
        return None

    # Need at least 2 DISTINCT dates — can't compute divergence from a single day
    unique_dates = history["date"].nunique()
    if unique_dates < 2:
        return None

    current = history.iloc[-1]
    oldest = history.iloc[0]

    # Spot return over the window
    if oldest["spot_close"] <= 0:
        return None
    spot_return_pct = (current["spot_close"] - oldest["spot_close"]) / oldest["spot_close"] * 100
    n_days = max((current["date"] - oldest["date"]).days, 1)

    # HV-normalized sigma (backward-looking: was this move big vs recent history?)
    hv_daily = current["hv_10d"] / np.sqrt(252) if current["hv_10d"] > 0 else 1.0
    hv_expected_move = hv_daily * np.sqrt(n_days)
    sigma_hv = spot_return_pct / hv_expected_move if hv_expected_move > 0 else 0

    # IV-normalized sigma (forward-looking: did the move exceed what options priced?)
    # ATM IV = what the straddle market expects over the option's life
    iv_daily = current["atm_iv"] / np.sqrt(252) if current["atm_iv"] > 0 else 1.0
    iv_expected_move = iv_daily * np.sqrt(n_days)
    sigma_iv = spot_return_pct / iv_expected_move if iv_expected_move > 0 else 0

    # IV-based sigma is the primary signal — tells you if the move exceeded
    # what the options market was pricing (ATM straddle implied move)
    sigma_move = sigma_iv  # primary: did the move exceed implied expectations?
    iv_surprise = abs(sigma_iv) > 1.0  # move bigger than what straddles priced

    # Skew change
    skew_change = current["skew"] - oldest["skew"]

    # Classify
    spot_dir = "UP" if spot_return_pct > 0 else "DOWN"
    skew_dir = "UP" if skew_change > 0.5 else ("DOWN" if skew_change < -0.5 else "FLAT")

    signal = "NEUTRAL"
    description = ""
    trade_action = ""
    tradeable = False

    if abs(sigma_move) >= DIVERGENCE_THRESHOLD:
        if spot_dir == "DOWN" and skew_dir == "UP":
            signal = "BEARISH_CONTINUATION"
            description = "Spot down + skew bid = smart money buying more protection after the drop. They think there's more coming."
            trade_action = "DO NOT BUY THE DIP. Fade at your own peril. If trading: buy puts or put spreads."
            tradeable = False  # continuation = don't counter-trade

        elif spot_dir == "DOWN" and skew_dir in ("FLAT", "DOWN"):
            signal = "BULLISH_REVERSAL"
            description = "Spot down but skew calm = institutions not reaching for protection. The move is more likely to fade."
            tradeable = True
            if abs(sigma_move) >= 1.0:
                trade_action = "TRADEABLE REVERSAL. Strong dip-buy candidate. Sell cash-secured puts or buy call spreads. The bigger the sigma move with flat skew, the better the setup."
            else:
                trade_action = "MILD REVERSAL SIGNAL. Watchlist candidate — wait for confirmation or a deeper dip before entering."

        elif spot_dir == "UP" and skew_dir in ("FLAT", "DOWN"):
            signal = "BULLISH_CONTINUATION"
            description = "Spot up + skew calm = no one hedging the rally. Healthy trend, institutions are comfortable."
            trade_action = "TREND IS HEALTHY. Buy dips or hold existing longs. Sell puts on pullbacks."
            tradeable = False  # continuation = just hold

        elif spot_dir == "UP" and skew_dir == "UP":
            signal = "BEARISH_REVERSAL"
            description = "Spot up but skew getting bid = smart money hedging INTO the rally. Skepticism building despite price strength."
            tradeable = True
            if abs(sigma_move) >= 1.0:
                trade_action = "TRADEABLE REVERSAL. Institutions don't trust this rally. Consider buying puts or put spreads, or taking profits on longs."
            else:
                trade_action = "MILD REVERSAL SIGNAL. Tighten stops on longs. Watch for follow-through."

    is_divergence = signal in ("BULLISH_REVERSAL", "BEARISH_REVERSAL")

    # IV surprise = move exceeded what straddles priced
    iv_note = ""
    if iv_surprise and is_divergence:
        iv_note = " IV SURPRISE: move exceeded implied expectations — vol expansion likely, increases conviction."
    elif iv_surprise:
        iv_note = " IV SURPRISE: move exceeded implied expectations!"

    return {
        "spot_return_pct": round(spot_return_pct, 2),
        "sigma_hv": round(sigma_hv, 2),
        "sigma_iv": round(sigma_iv, 2),
        "sigma_move": round(sigma_move, 2),
        "iv_surprise": iv_surprise,
        "skew_now": round(current["skew"], 2),
        "skew_then": round(oldest["skew"], 2),
        "skew_change": round(skew_change, 2),
        "n_days": n_days,
        "signal": signal,
        "description": description + iv_note,
        "trade_action": trade_action,
        "tradeable": tradeable,
        "is_divergence": is_divergence,
        "spot_dir": spot_dir,
        "skew_dir": skew_dir,
    }


# ---------------------------------------------------------------------------
# AI Analyst (Sonnet 4.6) — reads divergences and explains trade setups
# ---------------------------------------------------------------------------
HAIKU_SYSTEM_PROMPT = """You are an expert options volatility analyst. You analyze skew vs spot divergences,
fixed-strike volatility behavior, and put/call wall positioning to identify the best trade candidates.

Your framework (the "Delta Framework"):
- Skew divergence: When spot moves but skew doesn't confirm, it's a reversal signal.
  Spot down + skew flat = institutions not worried = dip buy.
  Spot up + skew bid = hedging the rally = take profits or short.
- Fixed-strike vol: Monitor IV at specific strikes (put/call walls) over time.
  Call wall IV crushing as price approaches resistance = no breakout demand = capped.
  Put wall IV dropping near support = protection unwound = support holds.
- Wall rolling: If stock drops but put wall doesn't roll lower, the options landscape
  isn't shifting with price = supportive. If put wall rolls lower = more downside expected.
- IV surprise: When the actual move exceeds what ATM straddles priced (σ(IV) > 1),
  vol expansion is likely and conviction increases.
- P/C ratio: Heavy put demand (>1.2) or heavy call demand (<0.7) adds context.
- Sector context: Compare a ticker's skew and IV rank within its sector peers.
  If one name in a sector has extreme skew while peers are calm, it's stock-specific.
  If the whole sector is moving, it may be macro or sector rotation.

For each trade candidate, provide:
1. A clear 2-3 sentence thesis explaining WHY this is tradeable (reference the specific vol signals)
2. Sector context — is this name an outlier vs peers, or is the whole sector moving?
3. The recommended trade structure (sell puts, buy call spreads, buy put spreads, etc.)
4. Key strikes to watch (the wall levels) and what would invalidate the setup
5. Conviction level (HIGH / MEDIUM / LOW) based on how many signals align

Be concise and direct. No fluff. Write like a prop desk volatility note."""


def run_sonnet_analysis(tradeable_tickers: List, wall_results: Dict,
                        snapshots: Dict, conn: sqlite3.Connection,
                        sector_map: Optional[Dict] = None) -> Optional[str]:
    """Send divergence data to Sonnet 4.6 for trade analysis."""
    if sector_map is None:
        sector_map = {}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  [AI] No ANTHROPIC_API_KEY set — skipping Sonnet analysis")
        return None

    # Build the data payload for Sonnet
    data_lines = []
    for ticker, div in tradeable_tickers:
        snap = snapshots.get(ticker, {})
        walls = wall_results.get(ticker, {})

        # Get recent history for context
        history = get_history(conn, ticker, days=10)
        hist_summary = []
        for _, row in history.iterrows():
            hist_summary.append({
                "date": str(row["date"].date()) if hasattr(row["date"], "date") else str(row["date"]),
                "spot": row.get("spot_close", 0),
                "skew": row.get("skew", 0),
                "atm_iv": row.get("atm_iv", 0),
                "pc_ratio": row.get("pc_ratio", 0),
            })

        ticker_data = {
            "ticker": ticker,
            "signal": div["signal"],
            "spot": snap.get("spot_close", 0),
            "spot_return_5d": div["spot_return_pct"],
            "sigma_iv": div["sigma_iv"],
            "sigma_hv": div["sigma_hv"],
            "iv_surprise": div["iv_surprise"],
            "skew_now": div["skew_now"],
            "skew_then": div["skew_then"],
            "skew_change": div["skew_change"],
            "atm_iv": snap.get("atm_iv", 0),
            "hv_10d": snap.get("hv_10d", 0),
            "iv_hv_ratio": round(snap.get("atm_iv", 0) / max(snap.get("hv_10d", 1), 1), 2),
            "pc_ratio": snap.get("pc_ratio", 0),
            "put_wall_strike": snap.get("put_wall_strike", 0),
            "put_wall_iv": snap.get("put_wall_iv", 0),
            "put_wall_oi": snap.get("put_wall_oi", 0),
            "call_wall_strike": snap.get("call_wall_strike", 0),
            "call_wall_iv": snap.get("call_wall_iv", 0),
            "call_wall_oi": snap.get("call_wall_oi", 0),
            "wall_analysis": walls.get("wall_signal", "") if walls else "",
            "put_wall_direction": walls.get("put_wall_direction", "") if walls else "",
            "call_wall_direction": walls.get("call_wall_direction", "") if walls else "",
            "put_wall_iv_change": walls.get("put_wall_iv_change", 0) if walls else 0,
            "call_wall_iv_change": walls.get("call_wall_iv_change", 0) if walls else 0,
            "sector": sector_map.get(ticker, ("", ""))[0] if sector_map else snap.get("sector", ""),
            "industry": sector_map.get(ticker, ("", ""))[1] if sector_map else snap.get("industry", ""),
            "history": hist_summary,
        }
        data_lines.append(ticker_data)

    # Also include wall-only signals (no skew divergence but notable wall behavior)
    for ticker, walls in wall_results.items():
        if ticker not in [t[0] for t in tradeable_tickers] and walls.get("wall_signal"):
            snap = snapshots.get(ticker, {})
            data_lines.append({
                "ticker": ticker,
                "signal": "WALL_SIGNAL_ONLY",
                "spot": snap.get("spot_close", 0),
                "wall_analysis": walls.get("wall_signal", ""),
                "put_wall_strike": walls.get("put_wall_now", 0),
                "call_wall_strike": walls.get("call_wall_now", 0),
                "put_wall_direction": walls.get("put_wall_direction", ""),
                "call_wall_direction": walls.get("call_wall_direction", ""),
                "put_wall_iv_change": walls.get("put_wall_iv_change", 0),
                "call_wall_iv_change": walls.get("call_wall_iv_change", 0),
                "atm_iv": snap.get("atm_iv", 0),
                "skew_now": snap.get("skew", 0),
                "pc_ratio": snap.get("pc_ratio", 0),
            })

    if not data_lines:
        return None

    user_msg = f"""Analyze these skew/vol divergence signals from today's scan ({dt.date.today().isoformat()}).

Rank the trade candidates from best to worst. For each one, give me:
1. Thesis (2-3 sentences — what the vol surface is telling you)
2. Trade structure (specific: sell X put, buy X/Y call spread, etc.)
3. Key levels to watch (walls, support/resistance from vol data)
4. Conviction (HIGH/MEDIUM/LOW)
5. What invalidates (what would make you exit or skip this trade)

DATA:
{json.dumps(data_lines, indent=2)}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=HAIKU_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        analysis = response.content[0].text
        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        cost = (tokens_in * 3.00 + tokens_out * 15.00) / 1_000_000
        print(f"\n{'='*90}")
        print(f"  AI ANALYST (Sonnet 4.6)  |  {tokens_in} in / {tokens_out} out  |  ${cost:.4f}")
        print(f"{'='*90}")
        print()
        for line in analysis.split("\n"):
            print(f"  {line}")
        print()
        return analysis

    except ImportError:
        print("  [AI] anthropic package not installed — pip install anthropic")
        return None
    except Exception as e:
        print(f"  [AI] Sonnet analysis failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_scan(tickers: List[str], lookback: int = SKEW_LOOKBACK_DAYS, use_ai: bool = True):
    conn = init_db()
    today_str = dt.date.today().isoformat()

    print(f"\n{'='*90}")
    print(f"  SKEW vs SPOT DIVERGENCE TRACKER  |  {today_str}  |  {len(tickers)} tickers")
    print(f"{'='*90}\n")

    # Step 0: Update forward returns for past signals
    fwd_updated = update_forward_returns(conn)
    if fwd_updated:
        print(f"  [FWD] Updated {fwd_updated} past signal(s) with forward returns\n")

    # Step 1: Snapshot today's skew for all tickers
    print(f"[1/3] Fetching live skew snapshots ({len(tickers)} tickers)...\n")
    snapshots = {}
    sector_map: Dict[str, Tuple[str, str]] = {}
    success = 0
    failed = 0
    for i, ticker in enumerate(tickers):
        snap = compute_skew_snapshot(ticker)
        if snap:
            # Get sector/industry
            sector, industry = get_sector(ticker)
            snap["sector"] = sector
            snap["industry"] = industry
            sector_map[ticker] = (sector, industry)

            # CBOE-style VIX calculation + earnings
            vix_data = compute_ticker_vix(ticker)
            if vix_data:
                snap["ticker_vix"] = vix_data["ticker_vix"]
                snap["hv_10d"] = vix_data["hv_10d"]
                snap["iv_hv_ratio"] = vix_data["iv_hv_ratio"]
                snap["dte_earnings"] = vix_data["dte_earnings"]

            save_reading(conn, ticker, today_str, snap)
            snapshots[ticker] = snap
            success += 1
            pw = snap.get('put_wall_strike', 0)
            cw = snap.get('call_wall_strike', 0)
            tvix = snap.get('ticker_vix', 0)
            ivhv = snap.get('iv_hv_ratio', 0)
            dte_e = snap.get('dte_earnings')
            earn_str = f"  EARN={dte_e}d" if dte_e is not None else ""
            vwks_val = snap.get('vwks', 0)
            vwks_dir = "BULL" if vwks_val > 0.005 else ("BEAR" if vwks_val < -0.005 else "NEUT")
            n_skew = snap.get('near_skew', 0)
            n_pc = snap.get('near_pc_ratio', 0)
            n_dte = snap.get('near_dte')
            ts = snap.get('skew_term_structure', '')
            near_str = f"  near({n_dte}d):skew={n_skew:+.1f},P/C={n_pc:.2f}" if n_dte else ""
            ts_str = f"  [{ts}]" if ts else ""
            print(f"  {ticker:6s}  spot={snap['spot_close']:>8.2f}  "
                  f"VIX={tvix:5.1f}%  "
                  f"IV/HV={ivhv:4.2f}  "
                  f"skew={snap['skew']:+6.2f}  "
                  f"P/C={snap['pc_ratio']:.2f}  "
                  f"VWKS={vwks_val:+.4f}({vwks_dir})  "
                  f"DTE={snap['dte']}d{earn_str}{near_str}{ts_str}")
        else:
            failed += 1
            if len(tickers) <= 30:  # only print failures for small lists
                print(f"  {ticker:6s}  -- no data --")
        # Rate limit for large scans
        if len(tickers) > 20 and i < len(tickers) - 1:
            time.sleep(BATCH_DELAY)
        # Progress for large scans
        if len(tickers) > 50 and (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(tickers)} done ({success} ok, {failed} failed)")
    print(f"\n  Snapshot complete: {success} ok, {failed} failed")

    # Step 2: Compute divergences and wall analysis from history
    print(f"\n[2/3] Computing {lookback}-day skew vs spot divergences...\n")

    results = []
    wall_results = {}
    candidate_count = 0
    for ticker in tickers:
        history = get_history(conn, ticker, days=lookback + 5)

        div = None
        walls_for_ticker = None
        if len(history) >= 2:
            div = compute_divergence(history)
            if div:
                results.append((ticker, div))

            walls_for_ticker = analyze_walls(history)
            if walls_for_ticker and walls_for_ticker["wall_signal"]:
                wall_results[ticker] = walls_for_ticker

        # Save candidate_log row for every ticker with a snapshot
        snap = snapshots.get(ticker)
        if snap:
            save_candidate(conn, ticker, today_str, snap, div=div,
                           walls=walls_for_ticker)
            candidate_count += 1

    if candidate_count > 0:
        print(f"  [CANDIDATE LOG] Wrote {candidate_count} rows to candidate_log")

    # Sort: tradeable divergences first, then other divergences, then by abs(sigma_move)
    results.sort(key=lambda x: (
        -int(x[1]["tradeable"]),
        -int(x[1]["is_divergence"]),
        -abs(x[1]["sigma_move"])
    ))

    # Display summary table
    print(f"  {'TICKER':6s}  {'SPOT':>8s}  {'σ(IV)':>7s}  {'SKEW Δ':>7s}  {'SIGNAL':22s}  READING")
    print(f"  {'-'*6}  {'-'*8}  {'-'*7}  {'-'*7}  {'-'*22}  {'-'*55}")

    divergence_count = 0
    tradeable_count = 0
    tradeable_tickers = []
    for ticker, div in results:
        marker = ""
        if div["tradeable"]:
            marker = " <<< TRADE CANDIDATE"
            tradeable_count += 1
            tradeable_tickers.append((ticker, div))
        elif div["is_divergence"]:
            marker = " *** DIVERGENCE ***"

        if div["is_divergence"]:
            divergence_count += 1

        iv_flag = " !!" if div["iv_surprise"] else ""
        sig_display = div["signal"].replace("_", " ")
        print(f"  {ticker:6s}  "
              f"{div['spot_return_pct']:+7.2f}%  "
              f"{div['sigma_iv']:+6.2f}σ{iv_flag:2s}  "
              f"{div['skew_change']:+6.2f}  "
              f"{sig_display:22s}  "
              f"{div['description']}{marker}")

    # Detailed trade recommendations for tradeable divergences
    if tradeable_tickers:
        print(f"\n{'='*90}")
        print(f"  TRADE RECOMMENDATIONS ({tradeable_count} setups)")
        print(f"{'='*90}")
        for ticker, div in tradeable_tickers:
            snap = snapshots.get(ticker, {})
            sig_display = div["signal"].replace("_", " ")
            sector, industry = sector_map.get(ticker, ("Unknown", "Unknown"))

            # Sector comparison
            sec_ranks = compute_sector_ranks(conn, ticker, sector, today_str)
            sector_skew_rank = sec_ranks.get("sector_skew_rank", 0)
            sector_iv_rank = sec_ranks.get("sector_iv_rank", 0)

            # Log signal for forward return tracking
            walls = wall_results.get(ticker)
            log_signal(conn, ticker, div, snap, walls, sector, industry,
                       sector_skew_rank, sector_iv_rank)

            print(f"\n  --- {ticker} | {sig_display} | {sector} / {industry} ---")
            print(f"  Spot: ${snap.get('spot_close', 0):.2f}  |  "
                  f"5d return: {div['spot_return_pct']:+.2f}%  |  "
                  f"σ(IV): {div['sigma_iv']:+.2f}  |  "
                  f"Skew: {div['skew_now']:+.1f} (was {div['skew_then']:+.1f}, Δ{div['skew_change']:+.1f})")
            pc = snap.get('pc_ratio', 0)
            pc_read = "heavy put demand" if pc > 1.2 else ("heavy call demand" if pc < 0.7 else "balanced")
            print(f"  ATM IV: {snap.get('atm_iv', 0):.1f}%  |  HV10: {snap.get('hv_10d', 0):.1f}%  |  "
                  f"IV/HV: {snap.get('atm_iv', 0) / max(snap.get('hv_10d', 1), 1):.2f}  |  "
                  f"P/C: {pc:.2f} ({pc_read})")
            # Sector peer comparison
            if sec_ranks["peer_count"] > 0:
                print(f"  SECTOR: skew rank {sector_skew_rank:.0f}th pctl vs {sec_ranks['peer_count']} {sector} peers  |  "
                      f"IV rank {sector_iv_rank:.0f}th pctl  |  "
                      f"Notable: {', '.join(sec_ranks['peers'][:3])}")
            print(f"  WHY:   {div['description']}")
            print(f"  TRADE: {div['trade_action']}")
            if div["iv_surprise"]:
                print(f"  NOTE:  Move exceeded IV expectations — vol expansion likely, higher conviction.")
            # Add wall confirmation if available
            if walls and walls["wall_signal"]:
                print(f"  WALLS: {walls['wall_signal']}")
                if walls["put_wall_now"] > 0:
                    print(f"         Put wall: ${walls['put_wall_now']:.0f} (was ${walls['put_wall_then']:.0f}) — {walls['put_wall_direction']}  |  IV Δ{walls['put_wall_iv_change']:+.1f}")
                if walls["call_wall_now"] > 0:
                    print(f"         Call wall: ${walls['call_wall_now']:.0f} (was ${walls['call_wall_then']:.0f}) — {walls['call_wall_direction']}  |  IV Δ{walls['call_wall_iv_change']:+.1f}")

    # Wall signals (tickers with notable wall behavior but maybe no skew divergence)
    wall_only = {t: w for t, w in wall_results.items()
                 if w["wall_signal"] and t not in [x[0] for x in tradeable_tickers]}
    if wall_only:
        print(f"\n{'='*90}")
        print(f"  FIXED-STRIKE VOL SIGNALS ({len(wall_only)} tickers)")
        print(f"{'='*90}")
        for ticker, walls in wall_only.items():
            snap = snapshots.get(ticker, {})
            print(f"\n  --- {ticker} | spot ${snap.get('spot_close', 0):.2f} ---")
            print(f"  {walls['wall_signal']}")
            if walls["put_wall_now"] > 0:
                print(f"  Put wall: ${walls['put_wall_now']:.0f} (was ${walls['put_wall_then']:.0f}) — {walls['put_wall_direction']}  |  IV Δ{walls['put_wall_iv_change']:+.1f}")
            if walls["call_wall_now"] > 0:
                print(f"  Call wall: ${walls['call_wall_now']:.0f} (was ${walls['call_wall_then']:.0f}) — {walls['call_wall_direction']}  |  IV Δ{walls['call_wall_iv_change']:+.1f}")

    print(f"\n{'='*90}")
    print(f"  {len(results)} tickers analyzed  |  {divergence_count} divergences  |  "
          f"{tradeable_count} tradeable setups  |  {len(wall_results)} wall signals")
    if divergence_count == 0 and not wall_results:
        # Check if we simply don't have enough history yet
        has_multi_day = any(
            get_history(conn, t, days=10)["date"].nunique() >= 2
            for t in tickers[:5] if not get_history(conn, t, days=10).empty
        )
        if not has_multi_day:
            print("  Day 1 — building baseline. Divergence signals start after 2+ days of data.")
        else:
            print("  No skew/spot divergences — skew is confirming the spot move across the board.")
    print(f"{'='*90}\n")

    # Step 3: AI Analyst — only runs when there are tradeable setups or wall signals
    # and we have enough history (5+ days of data)
    ai_analysis = None
    has_enough_history = any(
        len(get_history(conn, t, days=5)) >= 3
        for t in [x[0] for x in tradeable_tickers[:3]]
    ) if tradeable_tickers else False

    if (tradeable_tickers or wall_results) and has_enough_history and use_ai:
        print("[3/3] Running AI analyst (Sonnet 4.6)...\n")
        ai_analysis = run_sonnet_analysis(tradeable_tickers, wall_results, snapshots, conn, sector_map)

    # Post-scan: compute derived features for candidate_log
    try:
        from feature_lab import compute_derived_features
        derived = compute_derived_features(conn, today_str)
        if derived > 0:
            print(f"  [FEATURE LAB] Computed derived features for {derived} candidates")
    except ImportError:
        pass  # feature_lab not available yet
    except Exception as e:
        print(f"  [FEATURE LAB] Error: {e}")

    # Pattern study — show historical performance if we have enough data
    pattern_report = generate_pattern_report(conn)
    if pattern_report:
        print(pattern_report)

    conn.close()
    return results, ai_analysis


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Skew vs Spot Divergence Tracker")
    parser.add_argument("tickers", nargs="*", default=None, help="Tickers to scan")
    parser.add_argument("--days", type=int, default=SKEW_LOOKBACK_DAYS, help="Lookback days (default 5)")
    parser.add_argument("--weeklies", action="store_true", default=True,
                        help="Scan full CBOE weeklies universe (671 tickers) — ON by default")
    parser.add_argument("--small", action="store_true",
                        help="Scan only the 15 default tickers (override --weeklies)")
    parser.add_argument("--divergences-only", action="store_true", help="Only show divergences in output")
    parser.add_argument("--no-ai", "--no_ai", action="store_true", help="Skip AI analyst even if divergences found")
    parser.add_argument("--report", action="store_true", help="Show pattern study report (no scan)")
    parser.add_argument("--signals", action="store_true", help="Show all logged signals and forward returns")
    args = parser.parse_args()

    # Report-only modes
    if args.report or args.signals:
        conn = init_db()
        if args.report:
            report = generate_pattern_report(conn)
            if report:
                print(report)
            else:
                print("Not enough signal data yet (need 5+ signals with forward returns).")
        if args.signals:
            df = pd.read_sql_query("SELECT * FROM signal_log ORDER BY signal_date DESC", conn)
            if df.empty:
                print("No signals logged yet.")
            else:
                print(f"\n{'='*120}")
                print(f"  ALL LOGGED SIGNALS ({len(df)} total)")
                print(f"{'='*120}")
                print(f"  {'DATE':10s}  {'TICKER':6s}  {'SIGNAL':22s}  {'DIR':5s}  {'SPOT':>8s}  "
                      f"{'σ(IV)':>6s}  {'SKEWΔ':>6s}  {'CONV':6s}  "
                      f"{'1d':>6s}  {'3d':>6s}  {'5d':>6s}  {'10d':>6s}  {'20d':>6s}  {'OUTCOME':12s}  {'SECTOR'}")
                print(f"  {'-'*10}  {'-'*6}  {'-'*22}  {'-'*5}  {'-'*8}  "
                      f"{'-'*6}  {'-'*6}  {'-'*6}  "
                      f"{'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*12}  {'-'*20}")
                for _, row in df.iterrows():
                    def fmt_ret(v):
                        return f"{v:+5.1f}%" if pd.notna(v) and v != 0 else "  --  "
                    print(f"  {row['signal_date']:10s}  {row['ticker']:6s}  {row['signal_type']:22s}  "
                          f"{row['direction']:5s}  ${row['spot_at_signal']:>7.2f}  "
                          f"{row['sigma_iv']:+5.2f}  {row['skew_change_5d']:+5.1f}  {row.get('conviction', ''):6s}  "
                          f"{fmt_ret(row.get('fwd_1d_return'))}  {fmt_ret(row.get('fwd_3d_return'))}  "
                          f"{fmt_ret(row.get('fwd_5d_return'))}  {fmt_ret(row.get('fwd_10d_return'))}  "
                          f"{fmt_ret(row.get('fwd_20d_return'))}  {str(row.get('outcome', '')):12s}  "
                          f"{row.get('sector', '')}")
                print(f"{'='*120}\n")
        conn.close()
        sys.exit(0)

    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    elif args.small:
        tickers = DEFAULT_TICKERS
    elif WEEKLIES:
        tickers = WEEKLIES
    else:
        tickers = DEFAULT_TICKERS

    run_scan(tickers, lookback=args.days, use_ai=not args.no_ai)
