# -*- coding: utf-8 -*-
"""PATH LABELS for every Tier A qualifier — the data the self-audit scores against.

WHY (2026-09-02, user goal): "names that start making money as soon as we get in and
reach the target as soon as possible, and fewer losers." Forward close-to-close returns
(label_candidates.py) cannot answer that. This records, for EVERY qualifier — not only
the one we traded — the stop-aware PATH the live rule would have walked:

  first_green_day   first session whose close is above entry (None = never green)
  days_to_t1        session on which the +10% target was touched (None = not yet)
  days_to_stop      session on which the -7% stop was touched (None = not yet)
  mae_pct / mfe_pct deepest drawdown / best excursion before resolution (exit day: see exits.py)
  outcome           T1 | STOP | OPEN (unresolved so far; censored, never a failure)
  r_live            P&L in R for the live rule (+10 / -7)
  r_stop5 r_stop6   SHADOW: what a -5% / -6% stop would have returned (pre-registered Q2)
  r_t12             SHADOW: what a +12% target would have returned (pre-registered Q1)
  exit_ambiguous    1 = the exit bar touched BOTH levels; booked stop-first (an assumption,
                    since a daily bar cannot order intraday prints)
  exit_gap          1 = the exit filled at the open after a gap through a level

Mechanics: entry = signal-day close, walk from the next bar, every bar resolved by
exits.resolve_bar (the live monitor's rule), NO time limit (the live book has none).
The monitor sees intraday order on the day it acts; research sees only the final daily
bar, so the two can still differ on an ambiguous bar — exit_ambiguous marks every such row.
Idempotent: incomplete rows are recomputed every run until they resolve.

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
  call_wall_oi_d2 REAL,
  PRIMARY KEY (ticker, scan_date))"""

# Columns added after the table first shipped (CREATE IF NOT EXISTS cannot add them).
# S5_call_wall_oi_d2 (registered 2026-09-02): call-wall OI LEVEL two scans after entry.
# r_t8 / r_nevergreen_d2 / r_nevergreen_d3 (registered 2026-09-26): see hypotheses.json.
ADD_COLS = [('call_wall_oi_d2', 'REAL'), ('r_t8', 'REAL'),
            ('r_nevergreen_d2', 'REAL'), ('r_nevergreen_d3', 'REAL'),
            ('label_version', 'INTEGER'), ('exit_ambiguous', 'INTEGER'), ('exit_gap', 'INTEGER')]

# LABEL_VERSION — bump whenever the labelling ENGINE changes. Every row whose stored
# version differs is relabelled on the next run, so old and new rows can never silently
# mix two engines. (Audit F02/F03 provenance.)
#   1  original walk: stop-first, stop always filled at exactly -stop_pct
#   2  2026-10-04: shared exits.resolve_bar — gap-downs fill at the open, ambiguous
#      bars booked STOP, identical to the live monitor
#   3  2026-10-04: MAE/MFE exclude prices beyond the exit fill on the exit bar
#      (exits.held_extremes, audit F20)
#   4  2026-10-04 (re-audit R1): NO 20-bar timeout (the live book has none; ADSK took 22
#      sessions), unresolved = OPEN/censored, EXPIRED retired; exit_ambiguous/exit_gap flags
LABEL_VERSION = 4


def walk(fut, entry, t1_pct, stop_pct):
    """Walk bars with the SHARED exit rule. Returns (pnl%, day_hit or None, outcome).

    AUDIT F02/F03 (2026-10-04): this used its own logic — stop-first but always filled at
    exactly -stop_pct, even through a gap — while the live monitor checked the target
    first. Now both call exits.resolve_bar, so research labels and the live book resolve
    every bar identically, including gap fills at the open (pnl can be worse than
    -stop_pct, as it would be in reality).
    """
    from exits import resolve_bar
    up, dn = entry * (1 + t1_pct / 100), entry * (1 - stop_pct / 100)
    for k, (_, r) in enumerate(fut.iterrows(), start=1):
        res = resolve_bar(float(r['Open']), float(r['High']), float(r['Low']), up, dn)
        if res is not None:
            reason, px, _note = res
            return (px / entry - 1) * 100, k, ('T1' if reason == 'TP1' else 'STOP')
    # No time limit (re-audit R1): the live book holds until T1 or the stop, so an
    # unresolved path is OPEN (censored), however many bars it has run.
    return (float(fut.iloc[-1]['Close']) / entry - 1) * 100 if len(fut) else 0.0, None, 'OPEN'


NOT_YET = 'not_yet'      # no session after entry yet: nothing to label, and not an error


def label_one(g, d, entry):
    ix = g.index[g.date == d]
    if len(ix) == 0:
        return None                            # entry date absent from the price data
    i = int(ix[0])
    fut = g.iloc[i + 1:]                       # every bar since entry: no research timeout
    if len(fut) == 0:
        return NOT_YET
    pnl, day, out = walk(fut, entry, LIVE_T1, LIVE_STOP)
    # path stats up to resolution (or all bars seen). AUDIT F20: on the exit bar only the
    # part the position HELD counts — the same rule the live monitor uses.
    upto = fut.iloc[:day] if day else fut
    lows, highs = list(upto['Low'].astype(float)), list(upto['High'].astype(float))
    ambiguous = gap = None
    if day:
        from exits import resolve_bar, held_extremes
        xb = fut.iloc[day - 1]
        o, h, l = float(xb['Open']), float(xb['High']), float(xb['Low'])
        xres = resolve_bar(o, h, l, entry * (1 + LIVE_T1 / 100), entry * (1 - LIVE_STOP / 100))
        lows[-1], highs[-1] = held_extremes(o, h, l, xres)
        ambiguous, gap = int(xres[2].startswith('AMBIGUOUS')), int(xres[2].startswith('gapped'))
    mae = (min(lows) / entry - 1) * 100
    mfe = (max(highs) / entry - 1) * 100
    green = next((k for k, (_, r) in enumerate(upto.iterrows(), start=1)
                  if float(r['Close']) > entry), None)
    rec = {
        'entry': entry, 'first_green_day': green,
        'days_to_t1': day if out == 'T1' else None,
        'days_to_stop': day if out == 'STOP' else None,
        'mae_pct': round(mae, 3), 'mfe_pct': round(mfe, 3),
        'outcome': out, 'pnl_pct': round(pnl, 3), 'r_live': round(pnl / LIVE_STOP, 4),
        'bars_seen': len(fut), 'complete': int(out in ('T1', 'STOP')),
        'label_version': LABEL_VERSION, 'exit_ambiguous': ambiguous, 'exit_gap': gap,
    }
    # shadows — each walked independently with its own rule; NULL while still OPEN
    for col, t1, sp in (('r_stop5', 10.0, 5.0), ('r_stop6', 10.0, 6.0),
                        ('r_t12', 12.0, 7.0), ('r_t8', 8.0, 7.0)):
        p, _, o = walk(fut, entry, t1, sp)
        rec[col] = round(p / sp, 4) if o != 'OPEN' else None

    # NEVER-GREEN EXIT shadow (registered 2026-09-26). Rule: if the name has not closed
    # above entry even ONCE through day k, exit at that day-k close; otherwise hold to
    # T1/stop as normal. This is NOT "red at day k" — that looser condition catches names
    # that went green then faded, and testing it on 2026-09-02 LOST money (it killed 8
    # winners). "Never once green" contained zero winners across 49 resolved qualifiers.
    for col, k in (('r_nevergreen_d2', 2), ('r_nevergreen_d3', 3)):
        if rec['complete'] != 1:
            rec[col] = None                                   # unresolved, like the others
        elif day is not None and day <= k:
            rec[col] = rec['r_live']                          # already resolved by day k
        elif green is not None and green <= k:
            rec[col] = rec['r_live']                          # showed strength, hold
        elif len(fut) < k:
            rec[col] = rec['r_live']                          # not enough bars to judge
        else:
            rec[col] = round(((float(fut.iloc[k - 1]['Close']) / entry - 1) * 100) / LIVE_STOP, 4)
    return rec


def backfill_call_wall_oi_d2(con):
    """S5 (registered 2026-09-02): call-wall OI LEVEL two scans after entry, for every
    path row that lacks it. Pure SQL against candidate_log — no price download — so it
    also fills rows that were already complete when the column was added."""
    dates = [d for (d,) in con.execute('SELECT DISTINCT scan_date FROM candidate_log ORDER BY scan_date')]
    idx = {d: i for i, d in enumerate(dates)}
    rows = con.execute('SELECT ticker, scan_date FROM tier_a_paths WHERE call_wall_oi_d2 IS NULL').fetchall()
    n = 0
    for tk, sd in rows:
        i = idx.get(sd)
        if i is None or i + 2 >= len(dates):
            continue
        v = con.execute('SELECT call_wall_oi FROM candidate_log WHERE ticker=? AND scan_date=?',
                        (tk, dates[i + 2])).fetchone()
        if v and v[0] is not None:
            con.execute('UPDATE tier_a_paths SET call_wall_oi_d2=? WHERE ticker=? AND scan_date=?',
                        (float(v[0]), tk, sd)); n += 1
    con.commit()
    if n:
        print(f'[paths] call_wall_oi_d2 backfilled for {n} rows')


def qualifiers(con, today) -> pd.DataFrame:
    """Every Tier A qualifier up to yesterday that the live gates would admit: the cohort
    the path table must cover completely. Shared with self_audit's coverage check (R9)."""
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
    return q


def main():
    con = sqlite3.connect(DB)
    con.execute(DDL)
    for col, typ in ADD_COLS:                       # idempotent schema migration
        try:
            con.execute(f'ALTER TABLE tier_a_paths ADD COLUMN {col} {typ}')
        except sqlite3.OperationalError:
            pass                                    # already there
    backfill_call_wall_oi_d2(con)
    today = dt.date.today()
    q = qualifiers(con, today)

    # A row counts as done only if EVERY shadow is filled, so adding a new shadow column
    # self-heals: existing rows are relabelled once to populate it.
    # AUDIT F21: EVERY shadow must be resolved, not just the newest ones. A +10% winner
    # can satisfy the target-8 and never-green columns while the target-12 shadow is still
    # walking, and the old filter would then freeze r_t12 as NULL forever. With no time
    # limit (R1) an unresolved row is simply relabelled each run until it resolves.
    done = pd.read_sql_query(f"""SELECT ticker, scan_date FROM tier_a_paths
        WHERE complete=1 AND label_version = {LABEL_VERSION}
          AND r_stop5 IS NOT NULL AND r_stop6 IS NOT NULL AND r_t12 IS NOT NULL
          AND r_t8 IS NOT NULL AND r_nevergreen_d2 IS NOT NULL AND r_nevergreen_d3 IS NOT NULL""", con)
    key = set(zip(done.ticker, done.scan_date))
    todo = q[[(a, b) not in key for a, b in zip(q.ticker, q.scan_date)]]
    print(f'[paths] qualifiers {len(q)}, complete {len(key)}, to (re)label {len(todo)}')
    if todo.empty:
        con.close(); return 0

    px = yf.download(sorted(todo.ticker.unique().tolist()),
                     start=(pd.to_datetime(todo.scan_date.min()) - pd.Timedelta(days=3)).date().isoformat(),
                     end=(today + dt.timedelta(days=1)).isoformat(),
                     interval='1d', auto_adjust=True, progress=False, group_by='ticker', threads=True)
    n, unlabelled = 0, []
    for tk, grp in todo.groupby('ticker'):
        try:
            g = px[tk][['Open', 'High', 'Low', 'Close']].dropna().reset_index()
        except Exception:
            # Re-audit R9: this was a silent `continue`, so a failed download left a
            # qualifier unlabelled (or on an old label version) while the job reported
            # success. It is now named, and self_audit blocks READY while coverage is short.
            unlabelled += [f'{tk} {d}' for d in grp.scan_date]
            continue
        g['date'] = pd.to_datetime(g['Date']).dt.date.astype(str)
        for _, row in grp.iterrows():
            rec = label_one(g, row.scan_date, float(row.spot_close))
            if rec == NOT_YET:
                continue
            if not rec:
                unlabelled.append(f'{tk} {row.scan_date}')
                continue
            rec.update({'ticker': tk, 'scan_date': row.scan_date, 'tradeable': 1,
                        'n_legs': int(row.n_legs), 'labeled_at': today.isoformat()})
            cols = ', '.join(rec); qs = ', '.join('?' * len(rec))
            con.execute(f'INSERT OR REPLACE INTO tier_a_paths ({cols}) VALUES ({qs})', list(rec.values()))
            n += 1
    con.commit()
    backfill_call_wall_oi_d2(con)     # again: INSERT OR REPLACE above wiped it on relabeled rows
    tot = con.execute('SELECT COUNT(*), SUM(complete) FROM tier_a_paths').fetchone()
    print(f'[paths] wrote {n} rows; table now {tot[0]} rows, {tot[1]} complete')
    if unlabelled:
        print(f'::warning::[paths] {len(unlabelled)} qualifier(s) NOT labelled (no usable '
              f'price data): {", ".join(unlabelled[:20])}')
    con.close()
    return n


if __name__ == '__main__':
    main()
