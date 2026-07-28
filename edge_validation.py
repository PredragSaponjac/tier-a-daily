# -*- coding: utf-8 -*-
"""Prospective edge-validation scorecard — run anytime: python edge_validation.py

Pre-registered 2026-07-27 (BEFORE any fresh data existed). Tests, on ALL full-Tier-A
candidates with scan_date >= 2026-07-28, whether the edge-hunt findings hold
out-of-sample:

  H1 CONFIRMED-in-backtest: sector_iv_rank >= 60            (big-winner separator)
  H2 CONFIRMED-in-backtest: H1 AND skew_slope <= -1.6       (combo, 77%/0% in study)
  H3 HYPOTHESIS:            iv_hv_ratio >= 1.1              (disaster avoidance)

Decision protocol (user-approved): interim look at n>=10 fresh candidates — wire into
selection ONLY if overwhelming (Fisher p<0.05 on fresh data alone); formal decision
at n>=15-20. Until then the live bot's selection logic is UNTOUCHED.

Uses candidate_log (features captured at scan time) + auto-labeled forward returns;
skew_slope is recomputed from skew_daily history (deterministic, backward-looking).
sector_iv_rank == 0.0 is missing-coded -> excluded from H1/H2 scoring.
"""
import sqlite3

import pandas as pd
from scipy import stats

from edge_metrics import skew_slope, SECTOR_IV_RANK_MIN, SKEW_SLOPE_MAX, IV_HV_RATIO_MIN
from scanner_reader import SKEW_DB, EXCLUDED_ETFS

START = '2026-07-28'   # pre-registered: everything from here is out-of-sample

TIER_A = """SELECT * FROM candidate_log
 WHERE scan_date >= ? AND current_signal='BULLISH_REVERSAL' AND near_dte<=6
   AND skew_change_5d<=-7 AND near_skew<=-7 AND spot_return_pct<=-8
   AND put_wall_oi_change IS NOT NULL AND put_wall_oi_change<=0"""


def bucket_stats(d, mask, name):
    p, f = d[mask], d[~mask]
    def rate(x, col):
        x = x[x[col].notna()]
        return (len(x), float(x[col].mean()) if len(x) else float('nan'))
    n10p, big_p = rate(p, 'big20'); n10f, big_f = rate(f, 'big20')
    _, loss_p = rate(p, 'loss10'); _, loss_f = rate(f, 'loss10')
    print(f'  {name}')
    print(f'    pass n={len(p)} (labeled {n10p}): big20={big_p:.2f} loss10={loss_p:.2f}')
    print(f'    fail n={len(f)} (labeled {n10f}): big20={big_f:.2f} loss10={loss_f:.2f}')
    # Fisher on big20 when both buckets have labels
    pp, pf = p[p['big20'].notna()], f[f['big20'].notna()]
    if len(pp) >= 5 and len(pf) >= 5:
        tab = [[int(pp['big20'].sum()), len(pp) - int(pp['big20'].sum())],
               [int(pf['big20'].sum()), len(pf) - int(pf['big20'].sum())]]
        _, pv = stats.fisher_exact(tab)
        print(f'    Fisher (big20): p={pv:.4f}  {"<-- OVERWHELMING (wire-eligible)" if pv < 0.05 else ""}')
    else:
        print('    Fisher: not yet (need >=5 labeled per bucket)')


def main():
    con = sqlite3.connect(str(SKEW_DB))
    d = pd.read_sql_query(TIER_A, con, params=(START,))
    d = d[~d['ticker'].isin(EXCLUDED_ETFS)]
    d = d[d['sector'].fillna('Unknown') != 'Unknown']
    print(f'=== EDGE VALIDATION SCORECARD (fresh full-Tier-A since {START}) ===')
    print(f'fresh candidates: {len(d)} across {d["scan_date"].nunique()} scan dates')
    if len(d) == 0:
        print('no fresh Tier A candidates yet — nothing to score. Re-run after signals appear.')
        return
    n_lab = int(d['fwd_20d_return'].notna().sum())
    print(f'labeled with fwd_20d: {n_lab}  (labels auto-fill ~20 trading days after each scan)')
    print(f'progress: {len(d)}/10 to interim look, /15 to formal decision\n')

    d['big20'] = (d['fwd_20d_return'] >= 15).astype(float).where(d['fwd_20d_return'].notna())
    d['loss10'] = (d['fwd_10d_return'] <= -7).astype(float).where(d['fwd_10d_return'].notna())
    d['sr'] = d['sector_iv_rank'].replace(0.0, pd.NA).astype(float)
    d['ss'] = [skew_slope(t, s) for t, s in zip(d['ticker'], d['scan_date'])]

    h1 = d['sr'].notna()
    print(f'sector_iv_rank coverage: {int(h1.sum())}/{len(d)} (0.0-coded rows excluded)')
    dd = d[h1].copy()
    if len(dd):
        bucket_stats(dd, dd['sr'] >= SECTOR_IV_RANK_MIN, f'H1: sector_iv_rank >= {SECTOR_IV_RANK_MIN:.0f}')
        combo = (dd['sr'] >= SECTOR_IV_RANK_MIN) & (dd['ss'].notna()) & (dd['ss'] <= SKEW_SLOPE_MAX)
        bucket_stats(dd, combo, f'H2: combo (rank >= {SECTOR_IV_RANK_MIN:.0f} AND slope <= {SKEW_SLOPE_MAX})')
    bucket_stats(d, d['iv_hv_ratio'].fillna(0) >= IV_HV_RATIO_MIN, f'H3: iv_hv_ratio >= {IV_HV_RATIO_MIN}')

    print('\nreminder: wire-in requires (a) n>=10 AND Fisher p<0.05, or (b) n>=15 review.')
    print('Selection logic stays untouched until the user signs off on a scorecard.')


if __name__ == '__main__':
    main()
