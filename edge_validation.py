# -*- coding: utf-8 -*-
"""Prospective edge-validation scorecard — run anytime: python edge_validation.py

Pre-registered 2026-07-27 (BEFORE any fresh data existed). Tests, on ALL full-Tier-A
candidates with scan_date >= 2026-07-28, whether the edge-hunt findings hold
out-of-sample:

  H1 CONFIRMED-in-backtest: sector_iv_rank >= 60            (big-winner separator)

  ⚠️ 2026-08-24 HEAD-TO-HEAD on 51 qualified candidates (the current live gates,
  stop-aware outcomes, Bonferroni bar p<0.0125 for 4 metrics). NOTHING survived
  correction, and one component was measured NULL:
      stop_atr    >= 0.5   gap +4.63pp  p=0.083  both periods agree  (fail bucket n=7)
      sector_iv_rank >=60  gap +4.01pp  p=0.116  late period untestable (6 usable rows)
      iv_hv_ratio >= 1.1   gap +3.00pp  p=0.127  BOTH periods agree, full n=51
      skew_slope  <= -1.6  gap +0.48pp  p=0.482  periods DISAGREE  <-- NULL
  => H2 (the combo) carries a dead component: its apparent power is H1 alone, with
     slope adding noise. Read H2 as confirmatory of H1, never as independent evidence.
  => H3 (iv_hv_ratio) has the BEST robustness profile (full sample, both halves agree)
     and deserves equal billing with H1 despite the smaller point estimate.
  Thresholds remain FROZEN — this note records measurements, it does not retune them.
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
    # PROGRESS IS GATED BY LABELS, NOT BY CANDIDATE COUNT. Showing "15/10" while only
    # 2 rows have forward returns reads as "threshold passed" when nothing is testable.
    print(f'progress: {n_lab} LABELED / 10 for the interim look, /15 for the formal '
          f'decision   ({len(d)} candidates collected, {len(d)-n_lab} still awaiting '
          f'their 20-day forward return)\n')

    d['big20'] = (d['fwd_20d_return'] >= 15).astype(float).where(d['fwd_20d_return'].notna())
    d['loss10'] = (d['fwd_10d_return'] <= -7).astype(float).where(d['fwd_10d_return'].notna())
    d['sr'] = d['sector_iv_rank'].replace(0.0, pd.NA).astype(float)
    d['ss'] = [skew_slope(t, s) for t, s in zip(d['ticker'], d['scan_date'])]

    h1 = d['sr'].notna()
    print(f'sector_iv_rank coverage: {int(h1.sum())}/{len(d)} (0.0-coded rows excluded)')
    if int(h1.sum()) < len(d):
        print(f'  ⚠️ {len(d)-int(h1.sum())} fresh row(s) still missing sector_iv_rank — '
              'if this is nonzero AFTER 2026-07-28 the 5/28 regression is back '
              '(preflight has a guard, but check skew_tracker compute_sector_ranks).')
    dd = d[h1].copy()
    if len(dd):
        bucket_stats(dd, dd['sr'] >= SECTOR_IV_RANK_MIN, f'H1: sector_iv_rank >= {SECTOR_IV_RANK_MIN:.0f}')
        combo = (dd['sr'] >= SECTOR_IV_RANK_MIN) & (dd['ss'].notna()) & (dd['ss'] <= SKEW_SLOPE_MAX)
        bucket_stats(dd, combo, f'H2: combo (rank >= {SECTOR_IV_RANK_MIN:.0f} AND slope <= {SKEW_SLOPE_MAX})')
        print('    ^ NOTE: skew_slope measured NULL on 51 historical candidates '
              '(gap +0.48pp, p=0.482,\n      periods disagree). H2 is expected to '
              'merely track H1 — not independent evidence.')
    bucket_stats(d, d['iv_hv_ratio'].fillna(0) >= IV_HV_RATIO_MIN,
                 f'H3: iv_hv_ratio >= {IV_HV_RATIO_MIN}  [CO-PRIMARY with H1 — best '
                 f'robustness: full sample, both period halves agree]')

    # ---- H4: stop-width diagnostic (2026-07-31 study). LOG ONLY — live stop stays -7%.
    from edge_metrics import atr_profile, LIVE_STOP_PCT, STOP_ATR_TIGHT
    prof = [atr_profile(t, s) for t, s in zip(d['ticker'], d['scan_date'])]
    sa = pd.Series([p['stop_atr'] for p in prof if p], dtype=float)
    print(f'\n  H4 stop width: the fixed -{LIVE_STOP_PCT:.0f}% stop in ATR terms')
    if len(sa):
        tight = (sa < STOP_ATR_TIGHT).mean()
        print(f'    n={len(sa)}  median {sa.median():.2f}x ATR  (p10 {sa.quantile(.10):.2f} / '
              f'p90 {sa.quantile(.90):.2f})')
        print(f'    inside 1 daily range (<{STOP_ATR_TIGHT}x ATR): {tight:.0%}   '
              f'[backtest baseline: Tier A median 0.77x, 90% <1.5x]')
        print('    -> if fresh candidates keep landing <1x ATR, the -7% stop is being hit by')
        print('       noise, not thesis breaks. Decide with the same 10/15-sample protocol.')
    else:
        print('    no ATR data yet')

    # ---- H5: the DAY-3 behavioural rule (2026-08-17). TRACKING ONLY — not a rule.
    # Verified in the 102-candidate edge hunt (green by day 3 -> loss10 3% vs 34%,
    # Bonferroni-surviving) and perfectly separated in our own 11 closed trades
    # (all 8 winners green by day 3; all 3 losers never green). This is a POST-ENTRY
    # management signal, not an entry filter — you cannot know it when you enter.
    d3 = d[d['fwd_3d_return'].notna()].copy()
    print('\n  H5 day-3 behaviour (post-entry management signal, NOT an entry filter)')
    if len(d3) >= 4:
        d3['green3'] = d3['fwd_3d_return'] >= 0
        for lab, sub in [('green by day 3', d3[d3.green3]), ('red through day 3', d3[~d3.green3])]:
            lab_n = sub[sub['loss10'].notna()]
            if len(lab_n):
                print(f'    {lab:20s} n={len(sub):3d} (labeled {len(lab_n)})  '
                      f'loss10={lab_n["loss10"].mean():.2f}  big20={sub["big20"].mean() if sub["big20"].notna().any() else float("nan"):.2f}')
            else:
                print(f'    {lab:20s} n={len(sub):3d}  (no 10d labels yet)')
        print('    baseline from the study: loss10 3% if green by d3 vs 34% if red.')
    else:
        print(f'    only {len(d3)} fresh candidates with 3d data — too few yet')

    print('\nreminder: wire-in requires (a) n>=10 AND Fisher p<0.05, or (b) n>=15 review.')
    print('Selection logic AND the -7% stop stay untouched until you sign off on a scorecard.')


if __name__ == '__main__':
    main()
