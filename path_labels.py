# -*- coding: utf-8 -*-
"""PATH LABELS for every Tier A qualifier — the data the self-audit scores against.

WHY (2026-09-02, user goal): "names that start making money as soon as we get in and
reach the target as soon as possible, and fewer losers." Forward close-to-close returns
(label_candidates.py) cannot answer that. This records, for EVERY qualifier — not only
the one we traded — the stop-aware PATH the live rule would have walked:

  first_green_day   first session whose close is above entry (None = never green)
  days_to_t1        session on which the +10% target was touched (None = not yet)
  days_to_stop      session on which the -7% stop was touched (None = not yet)
  mae_pct / mfe_pct deepest drawdown / best excursion before resolution
  outcome           T1 | STOP | OPEN (unresolved, <20 bars) | EXPIRED (20 bars, neither)
  r_live            P&L in R for the live rule (+10 / -7)
  r_stop5 r_stop6   SHADOW: what a -5% / -6% stop would have returned (pre-registered Q2)
  r_t12             SHADOW: what a +12% target would have returned (pre-registered Q1)

Identical mechanics to monitor.py's day-walk and to every backtest: entry = signal-day
close, walk from the next bar, STOP-first on a same-bar hit, 20-bar window.
Idempotent: incomplete rows are recomputed every run until they resolve or expire.

Run: python path_labels.py        (uses SKEW_DB_PATH or ./skew_history.db)
"""
import datetime as dt
import math
import os
import sqlite3
import time

import numpy as np
import pandas as pd
import yfinance as yf

DB = os.environ.get('SKEW_DB_PATH', 'skew_history.db')
WINDOW = 20
LIVE_T1, LIVE_STOP = 10.0, 7.0

TIER_A = """SELECT ticker, scan_date, spot_close, put_wall_strike, atm_iv, skew
  FROM candidate_log
 WHERE current_signal='BULLISH_REVERSAL' AND near_dte<=6 AND skew_change_5d<=-7
   AND near_skew<=-7 AND spot_return_pct<=-8 AND put_wall_oi_change IS NOT NULL
   AND put_wall_oi_change<=0 AND scan_date <= ?"""

DDL = """CREATE TABLE IF NOT EXISTS tier_a_paths (
  ticker TEXT NOT NULL, scan_date TEXT NOT NULL,
  tradeable INTEGER, n_legs INTEGER, entry REAL,
  first_green_day INTEGER, days_to_t1 INTEGER, days_to_stop INTEGER,
  mae_pct REAL, mfe_pct REAL, outcome TEXT, pnl_pct REAL,
  r_live REAL, r_stop5 REAL, r_stop6 REAL, r_t12 REAL,
  bars_seen INTEGER, complete INTEGER, labeled_at TEXT,
  PRIMARY KEY (ticker, scan_date))"""


def walk(fut, entry, t1_pct, stop_pct):
    """Stop-aware walk. Returns (pnl%, day_hit or None, outcome)."""
    up, dn = entry * (1 + t1_pct / 100), entry * (1 - stop_pct / 100)
    for k, (_, r) in enumerate(fut.iterrows(), start=1):
        if float(r['Low']) <= dn:
            return -stop_pct, k, 'STOP'
        if float(r['High']) >= up:
            return t1_pct, k, 'T1'
    if len(fut) >= WINDOW:
        return (float(fut.iloc[-1]['Close']) / entry - 1) * 100, None, 'EXPIRED'
    return (float(fut.iloc[-1]['Close']) / entry - 1) * 100 if len(fut) else 0.0, None, 'OPEN'


def label_one(g, d, entry):
    ix = g.index[g.date == d]
    if len(ix) == 0:
        return None
    i = int(ix[0])
    fut = g.iloc[i + 1:i + 1 + WINDOW]
    if len(fut) == 0:
        return None
    pnl, day, out = walk(fut, entry, LIVE_T1, LIVE_STOP)
    # path stats up to resolution (or all bars seen)
    upto = fut.iloc[:day] if day else fut
    mae = (float(upto['Low'].min()) / entry - 1) * 100
    mfe = (float(upto['High'].max()) / entry - 1) * 100
    green = next((k for k, (_, r) in enumerate(upto.iterrows(), start=1)
                  if float(r['Close']) > entry), None)
    rec = {
        'entry': entry, 'first_green_day': green,
        'days_to_t1': day if out == 'T1' else None,
        'days_to_stop': day if out == 'STOP' else None,
        'mae_pct': round(mae, 3), 'mfe_pct': round(mfe, 3),
        'outcome': out, 'pnl_pct': round(pnl, 3), 'r_live': round(pnl / LIVE_STOP, 4),
        'bars_seen': len(fut), 'complete': int(out in ('T1', 'STOP', 'EXPIRED')),
    }
    # shadows — each walked independently with its own rule; NULL while still OPEN
    for col, t1, sp in (('r_stop5', 10.0, 5.0), ('r_stop6', 10.0, 6.0), ('r_t12', 12.0, 7.0)):
        p, _, o = walk(fut, entry, t1, sp)
        rec[col] = round(p / sp, 4) if o != 'OPEN' else None
    return rec


def main():
    con = sqlite3.connect(DB)
    con.execute(DDL)
    today = dt.date.today()
    q = pd.read_sql_query(TIER_A, con, params=((today - dt.timedelta(days=1)).isoformat(),))
    # SAME UNIVERSE AS THE LIVE BOT (fixed 2026-09-02). scanner_reader.read_tier_a drops
    # leveraged ETFs/ETNs and sector-Unknown names; the raw SQL here did not, so 7 of 57
    # "qualifiers" (LABU, NUGT, SOXS, UVIX, UVXY) were names the bot could never pick.
    # Every research query since 8/17 shared this leak; this table is the fix going forward.
    from scanner_reader import EXCLUDED_ETFS
    sec = pd.read_sql_query('SELECT ticker, scan_date, sector FROM candidate_log', con)
    q = q.merge(sec, on=['ticker', 'scan_date'], how='left')
    q = q[~q.ticker.isin(EXCLUDED_ETFS) & (q.sector.fillna('Unknown') != 'Unknown')]
    q['cushion_pct'] = (q.spot_close / q.put_wall_strike - 1) * 100
    q['vol_cushion'] = q.cushion_pct / (q.atm_iv / math.sqrt(252))
    q = q[(q.cushion_pct >= 0) & (q.cushion_pct <= 100)]
    q = q[(q['skew'] <= -7) | (q.vol_cushion >= 3.0)].copy()
    q['n_legs'] = (q['skew'] <= -7).astype(int) + (q.vol_cushion >= 3.0).astype(int)

    done = pd.read_sql_query('SELECT ticker, scan_date FROM tier_a_paths WHERE complete=1', con)
    key = set(zip(done.ticker, done.scan_date))
    todo = q[[(a, b) not in key for a, b in zip(q.ticker, q.scan_date)]]
    print(f'[paths] qualifiers {len(q)}, complete {len(key)}, to (re)label {len(todo)}')
    if todo.empty:
        con.close(); return 0

    px = yf.download(sorted(todo.ticker.unique().tolist()),
                     start=(pd.to_datetime(todo.scan_date.min()) - pd.Timedelta(days=3)).date().isoformat(),
                     end=(today + dt.timedelta(days=1)).isoformat(),
                     interval='1d', auto_adjust=True, progress=False, group_by='ticker', threads=True)
    n = 0
    for tk, grp in todo.groupby('ticker'):
        try:
            g = px[tk][['Open', 'High', 'Low', 'Close']].dropna().reset_index()
        except Exception:
            continue
        g['date'] = pd.to_datetime(g['Date']).dt.date.astype(str)
        for _, row in grp.iterrows():
            rec = label_one(g, row.scan_date, float(row.spot_close))
            if not rec:
                continue
            rec.update({'ticker': tk, 'scan_date': row.scan_date, 'tradeable': 1,
                        'n_legs': int(row.n_legs), 'labeled_at': today.isoformat()})
            cols = ', '.join(rec); qs = ', '.join('?' * len(rec))
            con.execute(f'INSERT OR REPLACE INTO tier_a_paths ({cols}) VALUES ({qs})', list(rec.values()))
            n += 1
    con.commit()
    tot = con.execute('SELECT COUNT(*), SUM(complete) FROM tier_a_paths').fetchone()
    print(f'[paths] wrote {n} rows; table now {tot[0]} rows, {tot[1]} complete')
    con.close()
    return n


if __name__ == '__main__':
    main()
