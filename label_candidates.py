#!/usr/bin/env python3
"""
Label Candidates — Fill in forward returns for candidate_log rows.

For each candidate_log row with NULL forward returns, fetches price data
via yfinance and fills 1d/3d/5d/10d/20d returns.

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
                    # Forward return: price at signal+N days vs price at signal
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


def _compute_residuals(conn: sqlite3.Connection):
    """Compute sector and industry residual returns for 5d horizon.

    sector_residual_5d = fwd_5d_return - median(fwd_5d_return for same sector on same date)
    industry_residual_5d = fwd_5d_return - median(fwd_5d_return for same industry on same date)
    """
    try:
        df = pd.read_sql_query("""
            SELECT id, ticker, scan_date, sector, industry, fwd_5d_return
            FROM candidate_log
            WHERE fwd_5d_return IS NOT NULL
            AND (sector_residual_5d IS NULL OR industry_residual_5d IS NULL)
        """, conn)
    except Exception:
        return

    if df.empty:
        return

    updated = 0

    # Sector residuals
    for (date, sector), grp in df.groupby(["scan_date", "sector"]):
        if not sector or sector in ("Unknown", "", "ETF") or len(grp) < 3:
            continue
        median_ret = grp["fwd_5d_return"].median()
        for _, row in grp.iterrows():
            residual = round(row["fwd_5d_return"] - median_ret, 4)
            conn.execute(
                "UPDATE candidate_log SET sector_residual_5d = ? WHERE id = ?",
                (residual, row["id"])
            )
            updated += 1

    # Industry residuals
    for (date, industry), grp in df.groupby(["scan_date", "industry"]):
        if not industry or industry in ("Unknown", "") or len(grp) < 3:
            continue
        median_ret = grp["fwd_5d_return"].median()
        for _, row in grp.iterrows():
            residual = round(row["fwd_5d_return"] - median_ret, 4)
            conn.execute(
                "UPDATE candidate_log SET industry_residual_5d = ? WHERE id = ?",
                (residual, row["id"])
            )

    if updated > 0:
        print(f"  [RESIDUALS] Computed sector/industry residuals for {updated} rows")


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
