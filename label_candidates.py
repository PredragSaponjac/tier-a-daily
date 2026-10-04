#!/usr/bin/env python3
"""
Label Candidates — Versioned prospective calendar-day close benchmarks.

Signals on/after 2026-10-05 are written into candidate_forward_v2. Legacy
candidate_log labels are preserved. Entry and forward prices use the same
price-only split-rebased series; missing expected provider inputs fail loudly.

HORIZONS ARE CALENDAR DAYS (documented 2026-10-04, audit F20): fwd_Nd is the first
close ON OR AFTER scan_date + N calendar days, not N trading sessions. A Friday fwd_3d
lands on Monday (one session later); fwd_20d is roughly 14 sessions. fwd_Nd_date
records the session actually used. Read every "Nd" result in that light; the
stop-aware path labels in path_labels.py count sessions.

Usage:
    python label_candidates.py
    python label_candidates.py --db path/to/skew_history.db
"""

import os
import sys
import time
import sqlite3
import argparse
import datetime as dt
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skew_history.db")


FORWARD_VERSION = 2
PROSPECTIVE_START = '2026-10-05'
HORIZONS = (('1d', 1), ('3d', 3), ('5d', 5), ('10d', 10), ('20d', 20))
FORWARD_TABLE = 'candidate_forward_v2'


def ensure_forward_schema(conn):
    horizons = ', '.join(f'fwd_{label}_return REAL, fwd_{label}_date TEXT' for label, _ in HORIZONS)
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {FORWARD_TABLE} (
      id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, scan_date TEXT NOT NULL,
      sector TEXT, industry TEXT, entry REAL, label_version INTEGER NOT NULL,
      price_basis TEXT, price_hash TEXT, price_inputs_json TEXT, observed_through TEXT,
      fwd_5d_return_short REAL, sector_residual_5d REAL, industry_residual_5d REAL,
      {horizons})""")
    if 'price_inputs_json' not in {r[1] for r in conn.execute(f'PRAGMA table_info({FORWARD_TABLE})')}:
        conn.execute(f'ALTER TABLE {FORWARD_TABLE} ADD COLUMN price_inputs_json TEXT')


def forward_labels(hist, signal_date):
    """Calendar-day CLOSE benchmark using one price-only split-rebased series.

    This is not an executable next-open path. Never divide adjusted future prices
    by candidate_log.spot_close; entry and every future price share this same series.
    """
    import path_labels as PL
    signal = dt.date.fromisoformat(signal_date)
    anchor = hist[hist.date == signal_date]
    if len(anchor) != 1:
        raise PL.LabelDataError(f'missing/duplicate signal-session close {signal_date}')
    entry = float(anchor.iloc[0].Close)
    rec = {'entry': entry, 'label_version': FORWARD_VERSION,
           'price_basis': 'signal_close_benchmark_yahoo_price_only_split_rebased',
           'price_hash': PL.price_hash(hist), 'price_inputs_json': PL.price_inputs(hist),
           'observed_through': hist.iloc[-1].date}
    for label, n_days in HORIZONS:
        target = (signal + dt.timedelta(days=n_days)).isoformat()
        future = hist[hist.date >= target]
        if not future.empty:
            rec[f'fwd_{label}_return'] = round((float(future.iloc[0].Close) / entry - 1) * 100, 6)
            rec[f'fwd_{label}_date'] = future.iloc[0].date
    if 'fwd_5d_return' in rec:
        rec['fwd_5d_return_short'] = -rec['fwd_5d_return']
    return rec


def update_forward_returns(db_path: str) -> int:
    """Write ONLY version-2 prospective benchmark rows; preserve legacy labels.

    Partial provider failures roll back this transaction and fail the workflow.
    Forward horizons are calendar days and include only completed regular sessions.
    """
    import path_labels as PL
    from market_time import last_completed_session
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_forward_schema(conn)
        asof = last_completed_session()
        rows = conn.execute(f"""SELECT c.id,c.ticker,c.scan_date,c.sector,c.industry
            FROM candidate_log c LEFT JOIN {FORWARD_TABLE} f ON c.id=f.id
            WHERE c.scan_date BETWEEN ? AND ? AND f.fwd_20d_return IS NULL
            ORDER BY c.scan_date""", (PROSPECTIVE_START, asof.isoformat())).fetchall()
        groups = {}
        for row in rows:
            groups.setdefault(row['ticker'], []).append(dict(row))
        updated, errors = 0, []
        for ticker, candidates in groups.items():
            try:
                hist = yf.Ticker(ticker).history(start=min(c['scan_date'] for c in candidates),
                    end=(asof + dt.timedelta(days=1)).isoformat(), interval='1d',
                    auto_adjust=False, actions=True)
                hist = PL.prepare_prices(hist, asof=asof)
                if hist.empty:
                    raise PL.LabelDataError('empty completed-session history')
                if hist.iloc[-1].date != asof.isoformat():
                    raise PL.LabelDataError(f'provider history stale: expected {asof}, got {hist.iloc[-1].date}')
                for cand in candidates:
                    rec = {**cand, **forward_labels(hist, cand['scan_date'])}
                    cols = list(rec)
                    conn.execute(f'INSERT INTO {FORWARD_TABLE} (' + ','.join(cols) + ') VALUES ('
                        + ','.join('?' for _ in cols) + ') ON CONFLICT(id) DO UPDATE SET '
                        + ','.join(f'{c}=excluded.{c}' for c in cols if c != 'id'), list(rec.values()))
                    updated += 1
            except Exception as e:
                errors.append(f'{ticker}: {type(e).__name__}: {e}')
        if errors:
            conn.rollback()
            raise PL.LabelDataError('forward labeling incomplete; transaction rolled back: ' + '; '.join(errors[:20]))
        _compute_residuals(conn, table=FORWARD_TABLE)
        conn.commit()
        print(f'[forward] v{FORWARD_VERSION}: wrote {updated} prospective calendar-day benchmark rows; legacy labels preserved')
        return updated
    finally:
        conn.close()


def _residuals(df: pd.DataFrame, key: str, excluded: tuple) -> pd.Series:
    """fwd_5d_return minus the median of its COMPLETE (scan_date, key) peer group; NaN for
    excluded/unknown groups and groups with fewer than 3 members."""
    ok = df[key].notna() & ~df[key].isin(excluded)
    g = df[ok].groupby(['scan_date', key])['fwd_5d_return']
    med, n = g.transform('median'), g.transform('size')
    out = pd.Series(np.nan, index=df.index)
    idx = med.index[n >= 3]
    out.loc[idx] = (df.loc[idx, 'fwd_5d_return'] - med.loc[idx]).round(4)
    return out


def _compute_residuals(conn: sqlite3.Connection, table="candidate_log"):
    """Compute sector and industry residual returns for 5d horizon.

    sector_residual_5d = fwd_5d_return - median(fwd_5d_return for same sector on same date)
    industry_residual_5d = fwd_5d_return - median(fwd_5d_return for same industry on same date)

    AUDIT F13 (2026-10-04): the median was taken over only the rows whose residual was still
    NULL, so peers labelled in an earlier run were left out and every arrival batch was
    centred on itself (fixture: returns 0,1,2 arriving after 100,101,102 were each centred
    within their batch; the full peer median is 51). In the release DB 54,454 of 62,691
    sector residuals disagreed with full-peer medians. Every run now recomputes from the
    COMPLETE population of each date and rewrites only the values that changed, so late
    arrivals and re-labelled returns repair their peers too. Research-only columns: nothing
    in the live bot or the self-audit reads them.
    """
    if table not in ("candidate_log", FORWARD_TABLE):
        raise ValueError("Unexpected residual table")
    df = pd.read_sql_query(f"""
            SELECT id, scan_date, sector, industry, fwd_5d_return,
                   sector_residual_5d AS old_s, industry_residual_5d AS old_i
            FROM {table}
            WHERE fwd_5d_return IS NOT NULL
        """, conn)

    if df.empty:
        return

    new_s = _residuals(df, 'sector', ('Unknown', '', 'ETF'))
    new_i = _residuals(df, 'industry', ('Unknown', ''))

    def same(a, b):
        return (a.isna() & b.isna()) | ((a - b).abs() < 1e-9)

    changed = ~(same(new_s, df['old_s']) & same(new_i, df['old_i']))
    rows = [(None if pd.isna(s) else float(s), None if pd.isna(i) else float(i), int(k))
            for s, i, k in zip(new_s[changed], new_i[changed], df['id'][changed])]
    if rows:
        conn.executemany(f"UPDATE {table} SET sector_residual_5d = ?, "
                         "industry_residual_5d = ? WHERE id = ?", rows)
        print(f"  [RESIDUALS] recomputed from full peer groups: {len(rows)} rows changed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Label candidates with forward returns")
    parser.add_argument("--db", default=DB_PATH, help="Path to skew_history.db")
    args = parser.parse_args()

    print(f"\n  LABEL CANDIDATES — Forward Return Filler")
    print(f"  {'='*50}")
    print(f"  DB: {args.db}")
    print(f"  Date: {dt.date.today().isoformat()}")
    print(f"  {'='*50}\n")

    n = update_forward_returns(args.db)
    print(f"\n  Done. Updated {n} rows.")
