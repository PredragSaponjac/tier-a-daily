#!/usr/bin/env python3
"""
Label Candidates — Fill in forward returns for candidate_log rows.

For each candidate_log row with NULL forward returns, fetches price data
via yfinance and fills 1d/3d/5d/10d/20d returns.

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


def update_forward_returns(db_path: str) -> int:
    """Fill in forward returns for candidate_log rows that are missing them.

    Groups by ticker to minimize yfinance API calls.
    Returns the number of rows updated.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today = dt.date.today()

    # Find rows needing updates — only those old enough to have 1d+ returns
    min_date = (today - dt.timedelta(days=1)).isoformat()
    rows = conn.execute("""
        SELECT id, ticker, scan_date, spot_close, sector, industry
        FROM candidate_log
        WHERE fwd_20d_return IS NULL
        AND scan_date <= ?
        ORDER BY scan_date
    """, (min_date,)).fetchall()

    if not rows:
        print("  No candidate_log rows need forward return updates.")
        conn.close()
        return 0

    print(f"  Found {len(rows)} candidate_log rows needing forward returns")

    # Group by ticker
    ticker_rows: Dict[str, list] = {}
    for row in rows:
        ticker = row["ticker"]
        ticker_rows.setdefault(ticker, []).append(dict(row))

    print(f"  Grouped into {len(ticker_rows)} tickers")

    updated = 0
    errors = 0

    for i, (ticker, candidates) in enumerate(ticker_rows.items()):
        try:
            # Get enough price history to cover all forward windows
            earliest_date = min(c["scan_date"] for c in candidates)
            start_dt = dt.datetime.strptime(earliest_date, "%Y-%m-%d").date()
            hist = yf.Ticker(ticker).history(
                start=(start_dt - dt.timedelta(days=5)).isoformat(),
                end=(today + dt.timedelta(days=1)).isoformat()
            )

            if hist.empty:
                errors += 1
                continue

            hist.index = pd.to_datetime(hist.index).tz_localize(None)

            for cand in candidates:
                cand_date = dt.datetime.strptime(cand["scan_date"], "%Y-%m-%d")
                spot_at = cand["spot_close"]
                if not spot_at or spot_at <= 0:
                    # Try to get spot from history
                    snap = hist[hist.index >= cand_date]
                    if snap.empty:
                        continue
                    spot_at = float(snap["Close"].iloc[0])
                    if spot_at <= 0:
                        continue

                updates = {}
                for label, n_days in [("1d", 1), ("3d", 3), ("5d", 5), ("10d", 10), ("20d", 20)]:
                    # Forward return: price at signal + N CALENDAR days (first session on or
                    # after) vs price at signal. Not N trading sessions; see module docstring.
                    target_date = cand_date + dt.timedelta(days=n_days)
                    if target_date.date() > today:
                        continue  # not enough time has passed

                    future = hist[hist.index >= target_date]
                    if not future.empty:
                        actual_date = future.index[0]
                        actual_price = float(future["Close"].iloc[0])
                        ret = round((actual_price - spot_at) / spot_at * 100, 4)
                        updates[f"fwd_{label}_return"] = ret
                        updates[f"fwd_{label}_date"] = actual_date.strftime("%Y-%m-%d")

                # Compute short-side return for 5d
                if "fwd_5d_return" in updates:
                    updates["fwd_5d_return_short"] = -updates["fwd_5d_return"]

                if updates:
                    set_clauses = []
                    params = []
                    for k, v in updates.items():
                        set_clauses.append(f"{k} = ?")
                        params.append(v)
                    params.append(cand["id"])

                    conn.execute(
                        f"UPDATE candidate_log SET {', '.join(set_clauses)} WHERE id = ?",
                        params
                    )
                    updated += 1

            time.sleep(0.3)  # rate limit

            if (i + 1) % 50 == 0:
                conn.commit()
                print(f"  ... {i+1}/{len(ticker_rows)} tickers processed ({updated} rows updated)")

        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  [ERROR] {ticker}: {e}")

    # Compute sector/industry residuals
    _compute_residuals(conn)

    conn.commit()
    conn.close()

    print(f"  Forward returns updated: {updated} rows ({errors} ticker errors)")
    return updated


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


def _compute_residuals(conn: sqlite3.Connection):
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
    try:
        df = pd.read_sql_query("""
            SELECT id, scan_date, sector, industry, fwd_5d_return,
                   sector_residual_5d AS old_s, industry_residual_5d AS old_i
            FROM candidate_log
            WHERE fwd_5d_return IS NOT NULL
        """, conn)
    except Exception:
        return

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
        conn.executemany("UPDATE candidate_log SET sector_residual_5d = ?, "
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
