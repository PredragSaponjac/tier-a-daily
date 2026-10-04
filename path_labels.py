# -*- coding: utf-8 -*-
"""Prospective, hypothetical scanner-qualified paths under the completed-bar paper rule.

Version 5 starts with signals on 2026-10-05. Entry is the next regular session's
Open, including that session's barriers. Yahoo price-only OHLC and entry share
one split-rebased basis (auto_adjust=False, actions=True). No time stop: OPEN is
right-censored, never a loss. All shadows use the SAME 7% R denominator because
portfolio slots are fixed dollars. Earlier label versions are preserved.

This cohort precedes veto/noise/portfolio selection; it is not the live trade book.
Daily OHLC cannot determine held-period extrema on an exit bar; explicit bounds
are stored, and compatibility mae_pct/mfe_pct are pessimistic bounds.
"""
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
DB = os.environ.get('SKEW_DB_PATH', str(HERE / 'skew_history.db'))
LIVE_T1, LIVE_STOP = 10.0, 7.0
LABEL_VERSION = 5
PROSPECTIVE_START = '2026-10-05'
NOT_YET = 'not_yet'
SHADOWS = ('r_stop5', 'r_stop6', 'r_t12', 'r_t8', 'r_nevergreen_d2', 'r_nevergreen_d3')
TIER_A = """SELECT * FROM candidate_log
 WHERE current_signal='BULLISH_REVERSAL' AND near_dte<=6 AND skew_change_5d<=-7
   AND near_skew<=-7 AND spot_return_pct<=-8 AND put_wall_oi_change IS NOT NULL
   AND put_wall_oi_change<=0 AND scan_date BETWEEN ? AND ?"""
DDL = """CREATE TABLE IF NOT EXISTS tier_a_paths (
 ticker TEXT NOT NULL, scan_date TEXT NOT NULL, tradeable INTEGER, n_legs INTEGER,
 entry REAL, first_green_day INTEGER, days_to_t1 INTEGER, days_to_stop INTEGER,
 mae_pct REAL, mfe_pct REAL, outcome TEXT, pnl_pct REAL, r_live REAL,
 r_stop5 REAL, r_stop6 REAL, r_t12 REAL, bars_seen INTEGER, complete INTEGER,
 labeled_at TEXT, call_wall_oi_d2 REAL, PRIMARY KEY(ticker,scan_date))"""
ADD_COLS = [(c, 'REAL') for c in ('r_t8', 'r_nevergreen_d2', 'r_nevergreen_d3',
    'pnl_stop5', 'pnl_stop6', 'pnl_t12', 'pnl_t8', 'pnl_nevergreen_d2', 'pnl_nevergreen_d3',
    'mae_lower_pct', 'mae_upper_pct', 'mfe_lower_pct', 'mfe_upper_pct')]
ADD_COLS += [(c, 'INTEGER') for c in ('label_version', 'exit_ambiguous', 'exit_gap', 'extrema_exact')]
ADD_COLS += [(c, 'TEXT') for c in ('entry_date', 'landmark_d2_date', 'observed_through',
    'exit_date', 'price_basis', 'price_hash', 'price_inputs_json', 'signal_features_json',
    'entry_policy', 'cohort', 'extrema_note', 'call_wall_oi_d2_inputs_json')]
ADD_COLS += [('mark_pnl_pct', 'REAL')]


class LabelDataError(RuntimeError):
    """A missing/invalid expected input; the workflow must fail and retain its DB."""


def protocol():
    return json.loads((HERE / 'hypotheses.json').read_text(encoding='utf-8'))['inference_protocol']


def observation_end(today):
    return min(today, dt.date.fromisoformat(protocol()['observation_cutoff']))


def prepare_prices(g, asof=None):
    """Use the shared completed-session OHLC/actions validation contract."""
    from exits import completed_history
    from market_time import EASTERN, last_completed_session
    g = g.copy()
    if 'date' not in g:
        g['date'] = pd.to_datetime(g.index).date.astype(str)
    if g.empty:
        raise LabelDataError('empty completed-session price history')
    g['date'] = pd.to_datetime(g['date']).dt.date.astype(str)
    cutoff = asof or last_completed_session()
    # Provider end is exclusive, so requests stop after this last completed session.
    # New-entry prices and their anchor use one current split-rebased basis.
    indexed = g.copy()
    indexed.index = pd.to_datetime(indexed.date)
    clock = dt.datetime.combine(cutoff, dt.time(23, 59), tzinfo=EASTERN)
    try:
        completed = completed_history(indexed, dt.date.fromisoformat(g.iloc[0].date), asof=clock)
    except Exception as e:
        raise LabelDataError(str(e)) from e
    return completed.reset_index(drop=True)


def price_hash(g):
    cols = ['date', 'Open', 'High', 'Low', 'Close'] + [c for c in ('Stock Splits', 'Dividends') if c in g]
    return hashlib.sha256(g[cols].to_csv(index=False, float_format='%.12g').encode()).hexdigest()


def price_inputs(g):
    """Retain the actual frozen provider inputs, not just an unreproducible digest."""
    cols = ['date', 'Open', 'High', 'Low', 'Close'] + [c for c in ('Stock Splits', 'Dividends') if c in g]
    rows = g[cols].astype(object).where(pd.notna(g[cols]), None).to_dict('records')
    return json.dumps({'auto_adjust': False, 'actions_requested': True, 'bars': rows},
                      sort_keys=True, separators=(',', ':'), allow_nan=False)


def freeze_signal_features(con, row):
    """Backward-looking inputs plus the peer-count input, frozen on first labeling."""
    clean = {k: None if pd.isna(v) else v for k, v in row.items() if k != 'Index'}
    ticker, sd = clean['ticker'], clean['scan_date']
    clean['washouts'] = con.execute('SELECT COUNT(*) FROM candidate_log WHERE scan_date=? '
                                   'AND spot_return_pct<=-8', (sd,)).fetchone()[0]
    pts = con.execute('SELECT date,skew FROM skew_daily WHERE ticker=? AND date<=? '
                      'ORDER BY date DESC LIMIT 6', (ticker, sd)).fetchall()
    date = dt.date.fromisoformat(sd)
    pts = sorted((d, v) for d, v in pts if v is not None
                 and (date - dt.date.fromisoformat(d[:10])).days <= 14)
    clean['skew_slope'] = float(np.polyfit(range(len(pts)), [v for _, v in pts], 1)[0]) if len(pts) >= 3 else None
    from edge_metrics import SECTOR_IV_RANK_MIN, SKEW_SLOPE_MAX
    rank, slope = clean.get('sector_iv_rank'), clean['skew_slope']
    clean['combo_pass'] = float(rank >= SECTOR_IV_RANK_MIN and slope <= SKEW_SLOPE_MAX) if rank not in (None, 0) and slope is not None else None
    return json.dumps(clean, sort_keys=True, default=str, allow_nan=False)


def walk(fut, entry, t1_pct, stop_pct):
    """No timeout. Returns (raw P&L%, event session or None, T1/STOP/OPEN)."""
    from exits import resolve_bar
    up, dn = entry * (1 + t1_pct / 100), entry * (1 - stop_pct / 100)
    for k, (_, r) in enumerate(fut.iterrows(), 1):
        res = resolve_bar(float(r.Open), float(r.High), float(r.Low), up, dn)
        if res is not None:
            reason, px, _ = res
            return (px / entry - 1) * 100, k, 'T1' if reason == 'TP1' else 'STOP'
    return (float(fut.iloc[-1].Close) / entry - 1) * 100 if len(fut) else 0.0, None, 'OPEN'


def label_one(g, signal_date, legacy_entry=None):
    """Version-5 entry: next regular session Open. legacy_entry is never used.

    Price preparation/completion filtering is the caller's responsibility; this pure
    function verifies that every expected session through the supplied last bar exists.
    """
    from market_time import next_session, sessions_between
    from exits import resolve_bar, held_extreme_bounds
    expected = next_session(dt.date.fromisoformat(signal_date)).isoformat()
    fut = g[g.date >= expected].copy().reset_index(drop=True)
    if fut.empty:
        return NOT_YET
    if fut.iloc[0].date != expected:
        raise LabelDataError(f'missing entry session {expected}')
    expected_dates = [s.isoformat() for s in sessions_between(
        dt.date.fromisoformat(expected), dt.date.fromisoformat(fut.iloc[-1].date))]
    if list(fut.date) != expected_dates:
        raise LabelDataError('missing or duplicate session in forward price path')
    entry = float(fut.iloc[0].Open)
    if not np.isfinite(entry) or entry <= 0:
        raise LabelDataError('invalid next-session entry Open')
    pnl, day, out = walk(fut, entry, LIVE_T1, LIVE_STOP)
    upto = fut.iloc[:day] if day else fut
    # Include entry as an observed held price; closes on an exit session occur after exit.
    closes_held = upto.iloc[:-1] if day else upto
    green = next((k for k, r in enumerate(closes_held.itertuples(), 1) if float(r.Close) > entry), None)
    low_min = low_max = high_min = high_max = entry
    notes, exact, ambiguous, gap = [], True, None, None
    for k, r in enumerate(upto.itertuples(), 1):
        res = resolve_bar(float(r.Open), float(r.High), float(r.Low),
                          entry * 1.10, entry * .93) if day == k else None
        bounds = held_extreme_bounds(float(r.Open), float(r.High), float(r.Low), res,
                                    T1=entry * 1.10, STOP=entry * .93)
        low_min = min(low_min, bounds['low_min']); low_max = min(low_max, bounds['low_max'])
        high_min = max(high_min, bounds['high_min']); high_max = max(high_max, bounds['high_max'])
        exact = exact and bounds['exact']
        if bounds['note']:
            notes.append(bounds['note'])
        if res:
            ambiguous = int(res[2].startswith('AMBIGUOUS')); gap = int(res[2].startswith('gapped'))
    pct = lambda price: round((price / entry - 1) * 100, 3)
    rec = dict(entry=entry, entry_date=expected, first_green_day=green,
        days_to_t1=day if out == 'T1' else None, days_to_stop=day if out == 'STOP' else None,
        mae_pct=pct(low_min), mfe_pct=pct(high_max), mae_lower_pct=pct(low_min),
        mae_upper_pct=pct(low_max), mfe_lower_pct=pct(high_min), mfe_upper_pct=pct(high_max),
        extrema_exact=int(exact), extrema_note='; '.join(sorted(set(notes))),
        outcome=out, pnl_pct=round(pnl, 6) if out != 'OPEN' else None,
        r_live=round(pnl / LIVE_STOP, 8) if out != 'OPEN' else None, mark_pnl_pct=round(pnl, 6),
        bars_seen=len(fut), complete=int(out != 'OPEN'), label_version=LABEL_VERSION,
        exit_ambiguous=ambiguous, exit_gap=gap, observed_through=fut.iloc[-1].date,
        exit_date=fut.iloc[day - 1].date if day else None,
        entry_policy='next_regular_open', cohort='hypothetical_scanner_qualified',
        price_basis='yahoo_price_only_split_rebased_auto_adjust_false', price_hash=price_hash(fut),
        price_inputs_json=price_inputs(fut),
        landmark_d2_date=fut.iloc[1].date if len(fut) >= 2 else None)
    for col, t1, sp in (('stop5', 10., 5.), ('stop6', 10., 6.), ('t12', 12., 7.), ('t8', 8., 7.)):
        p, _, o = walk(fut, entry, t1, sp)
        rec['pnl_' + col] = round(p, 6) if o != 'OPEN' else None
        rec['r_' + col] = round(p / LIVE_STOP, 8) if o != 'OPEN' else None
    for col, k in (('nevergreen_d2', 2), ('nevergreen_d3', 3)):
        if day is not None and day <= k:
            p = pnl
        elif len(fut) < k:
            p = None
        elif green is not None and green <= k:
            p = pnl if out != 'OPEN' else None
        else:
            # This shadow can be resolved at the landmark even while the baseline is censored.
            p = (float(fut.iloc[k - 1].Close) / entry - 1) * 100
        rec['pnl_' + col] = round(p, 6) if p is not None else None
        rec['r_' + col] = round(p / LIVE_STOP, 8) if p is not None else None
    return rec


def backfill_call_wall_oi_d2(con):
    """Day-2 close observation from the EXACT price-session date, never 'two scans later'.

    Missing scanner observation remains NULL and is reported as unavailable, rather
    than leaking a later scan or being turned into a passing high OI value.
    """
    rows = con.execute('SELECT ticker,scan_date,landmark_d2_date FROM tier_a_paths '
                       'WHERE label_version=? AND landmark_d2_date IS NOT NULL '
                       'AND call_wall_oi_d2 IS NULL', (LABEL_VERSION,)).fetchall()
    for tk, sd, obs in rows:
        v = con.execute('SELECT call_wall_oi FROM candidate_log WHERE ticker=? AND scan_date=?', (tk, obs)).fetchone()
        if v and v[0] is not None:
            value = float(v[0])
            if not np.isfinite(value) or value < 0:
                raise LabelDataError(f'{tk} {obs}: invalid call-wall OI')
            frozen = json.dumps({'ticker': tk, 'observation_session': obs, 'call_wall_oi': value}, sort_keys=True)
            con.execute('UPDATE tier_a_paths SET call_wall_oi_d2=?,call_wall_oi_d2_inputs_json=? '
                        'WHERE ticker=? AND scan_date=? AND label_version=?', (value, frozen, tk, sd, LABEL_VERSION))


def qualifiers(con, today):
    """Frozen prospective scanner cohort; deliberately not claimed to be live eligibility."""
    from scanner_reader import EXCLUDED_ETFS
    p = protocol()
    upper = min((today - dt.timedelta(days=1)).isoformat(), p['entry_cutoff'])
    q = pd.read_sql_query(TIER_A, con, params=(PROSPECTIVE_START, upper))
    q = q[~q.ticker.isin(EXCLUDED_ETFS) & (q.sector.fillna('Unknown') != 'Unknown')].copy()
    q['cushion_pct'] = (q.spot_close / q.put_wall_strike - 1) * 100
    q['vol_cushion'] = q.cushion_pct / (q.atm_iv / math.sqrt(252))
    q = q[(q.cushion_pct >= 0) & (q.cushion_pct <= 100)]
    q = q[(q['skew'] <= -7) | (q.vol_cushion >= 3.)].copy()
    if not q.empty:
        if any(c not in q for c in ('screen_version', 'window_sessions')):
            raise LabelDataError('prospective qualifiers lack versioned screen metadata')
        correct = (q.screen_version == 'nine-session-v2') & (q.window_sessions == 9)
        if not correct.all():
            bad = [f'{r.ticker} {r.scan_date}' for r in q[~correct].itertuples()]
            raise LabelDataError('prospective qualifier(s) from incompatible screen: ' + ', '.join(bad[:20]))
    q['n_legs'] = (q['skew'] <= -7).astype(int) + (q.vol_cushion >= 3.).astype(int)
    return q


def expected_qualifiers(con, today=None):
    from market_time import last_completed_session, next_session
    today = today or dt.date.today()
    q = qualifiers(con, today)
    if q.empty:
        return q
    completed = min(last_completed_session(), dt.date.fromisoformat(protocol()['observation_cutoff']))
    return q.loc[[next_session(dt.date.fromisoformat(s)) <= completed for s in q.scan_date]]


def ensure_schema(con):
    con.execute(DDL)
    have = {r[1] for r in con.execute('PRAGMA table_info(tier_a_paths)')}
    for col, typ in ADD_COLS:
        if col not in have:
            con.execute(f'ALTER TABLE tier_a_paths ADD COLUMN {col} {typ}')


def main():
    from market_time import last_completed_session
    con = sqlite3.connect(DB)
    try:
        ensure_schema(con)
        today = dt.date.today()
        q = expected_qualifiers(con, today)
        asof = min(last_completed_session(), dt.date.fromisoformat(protocol()['observation_cutoff']))
        old = pd.read_sql_query('SELECT ticker,scan_date,label_version,observed_through,complete,'
            + ','.join(SHADOWS) + ' FROM tier_a_paths', con)
        current = old[old.label_version == LABEL_VERSION]
        # Censored paths also become immutable once the fixed observation cutoff is
        # reached. Their unknown outcomes never become failures or gain later data.
        settled = (current.complete == 1) & current[list(SHADOWS)].notna().all(axis=1)
        frozen_cutoff = current.observed_through.fillna('') >= protocol()['observation_cutoff']
        done = current[settled | frozen_cutoff]
        keys = set(zip(done.ticker, done.scan_date))
        todo = q.loc[[(t,s) not in keys for t,s in zip(q.ticker,q.scan_date)]]
        print(f'[paths] v{LABEL_VERSION} prospective qualifiers {len(q)}, pending {len(todo)}; legacy rows preserved')
        if todo.empty:
            backfill_call_wall_oi_d2(con); con.commit(); return 0
        data = yf.download(sorted(todo.ticker.unique().tolist()),
            start=todo.scan_date.min(), end=(asof + dt.timedelta(days=1)).isoformat(),
            interval='1d', auto_adjust=False, actions=True, progress=False, group_by='ticker', threads=True)
        count, errors = 0, []
        for tk, grp in todo.groupby('ticker'):
            try:
                g = data[tk] if isinstance(data.columns, pd.MultiIndex) else data
                g = prepare_prices(g, asof=asof)
                if g.empty:
                    raise LabelDataError('empty completed-session history')
                if g.iloc[-1].date != asof.isoformat():
                    raise LabelDataError(f'provider history stale: expected {asof}, got {g.iloc[-1].date}')
            except Exception as e:
                errors.append(f'{tk}: {type(e).__name__}: {e}'); continue
            for row in grp.itertuples():
                try:
                    rec = label_one(g, row.scan_date)
                    if rec == NOT_YET:
                        raise LabelDataError('expected entry session is missing')
                    existing = con.execute('SELECT label_version,signal_features_json FROM tier_a_paths WHERE ticker=? AND scan_date=?',
                                           (tk, row.scan_date)).fetchone()
                    if existing and existing[0] != LABEL_VERSION:
                        raise LabelDataError('refusing to overwrite a legacy label row')
                    rec.update(ticker=tk, scan_date=row.scan_date, tradeable=None,
                               n_legs=int(row.n_legs), labeled_at=today.isoformat(),
                               signal_features_json=(existing[1] if existing and existing[1]
                                                     else freeze_signal_features(con, row._asdict())))
                    cols = list(rec)
                    con.execute('INSERT INTO tier_a_paths (' + ','.join(cols) + ') VALUES ('
                        + ','.join('?' for _ in cols) + ') ON CONFLICT(ticker,scan_date) DO UPDATE SET '
                        + ','.join(f'{c}=excluded.{c}' for c in cols if c not in ('ticker','scan_date')), list(rec.values()))
                    count += 1
                except Exception as e:
                    errors.append(f'{tk} {row.scan_date}: {type(e).__name__}: {e}')
        if errors:
            con.rollback()
            raise LabelDataError('path labeling incomplete; transaction rolled back: ' + '; '.join(errors[:20]))
        backfill_call_wall_oi_d2(con)
        con.commit()
        print(f'[paths] wrote {count} v{LABEL_VERSION} paths; OPEN paths remain right-censored')
        return count
    finally:
        con.close()


if __name__ == '__main__':
    main()
