# -*- coding: utf-8 -*-
"""Exploratory prospective scorecard. No sample-count or nominal-p promotion rule.

Legacy research headlines are not independently reproduced by this repository.
Version-2 forward returns are calendar-day, signal-close benchmarks. Version-5
paths use next-session Open and no time stop. They are distinct outcomes.
The fixed inference protocol is implemented in self_audit.py; weekly views never
issue READY. Missing required labels/features fail visibly instead of changing
unknown values into a failing bucket.
"""
import datetime as dt
import sqlite3
from pathlib import Path

import pandas as pd
from scipy import stats

import path_labels as PL
import self_audit as SA
from edge_metrics import SECTOR_IV_RANK_MIN, SKEW_SLOPE_MAX, IV_HV_RATIO_MIN
from scanner_reader import SKEW_DB

START = PL.PROSPECTIVE_START


def bucket_stats(d, mask, name):
    passing, failing = d[mask], d[~mask]
    print(f'  {name}: pass {len(passing)} / fail {len(failing)} (descriptive only)')
    for label, group in [('pass', passing), ('fail', failing)]:
        for outcome in ('big20', 'loss10'):
            observed = group[group[outcome].notna()]
            rate = float(observed[outcome].mean()) if len(observed) else float('nan')
            print(f'    {label} {outcome}: {rate:.3f}, {len(observed)} observed / {len(group)-len(observed)} awaiting benchmark')
    a, b = passing[passing.big20.notna()], failing[failing.big20.notna()]
    if len(a) >= 5 and len(b) >= 5:
        tab = [[int(a.big20.sum()), len(a)-int(a.big20.sum())],
               [int(b.big20.sum()), len(b)-int(b.big20.sum())]]
        p = stats.fisher_exact(tab).pvalue
        print(f'    exploratory IID Fisher p={p:.8g}; not a promotion test')


def main():
    con = sqlite3.connect(f'file:{Path(SKEW_DB).resolve().as_posix()}?mode=ro', uri=True)
    try:
        q = PL.qualifiers(con, dt.date.today())
        print(f'=== EXPLORATORY SCANNER COHORT since {START}: {len(q)} paths ===')
        print('Scanner-qualified hypothetical names, before veto/noise/portfolio selection.')
        if q.empty:
            print('No prospective qualifiers yet; historical engine labels are preserved separately.')
            return
        coverage = SA.label_coverage(con)
        if not coverage['ok']:
            raise PL.LabelDataError(coverage['detail'])
        f = pd.read_sql_query('SELECT * FROM candidate_forward_v2 WHERE label_version=2', con)
        cols = ['ticker', 'scan_date', 'fwd_20d_return', 'fwd_10d_return']
        f = f[cols].rename(columns={c: 'benchmark_' + c for c in cols[2:]})
        d = q.merge(f, on=['ticker', 'scan_date'], how='left')
        d['big20'] = (d.benchmark_fwd_20d_return >= 15).astype(float).where(d.benchmark_fwd_20d_return.notna())
        d['loss10'] = (d.benchmark_fwd_10d_return <= -7).astype(float).where(d.benchmark_fwd_10d_return.notna())
        print(f'{int(d.big20.notna().sum())} completed 20-CALENDAR-day close benchmarks; no interim/formal sample-count promotion threshold.')
        for feature, threshold, name in [('sector_iv_rank', SECTOR_IV_RANK_MIN, 'H1 sector IV rank'),
                                         ('iv_hv_ratio', IV_HV_RATIO_MIN, 'H3 IV/HV ratio')]:
            valid = d[feature].notna()
            if feature == 'sector_iv_rank':
                valid &= d[feature] != 0
            if not valid.all():
                raise PL.LabelDataError(f'{feature}: missing {int((~valid).sum())} expected prospective inputs')
            bucket_stats(d, d[feature] >= threshold, f'{name} >= {threshold}')
        paths = SA.load_frame(con)
        at_risk = SA._open_at(paths, 3)
        print(f'\nH5 actual ever-green through session 3: {len(at_risk)} still open at the landmark.')
        for name, sub in [('ever green by session 3', at_risk[at_risk.first_green_day.fillna(float("inf")) <= 3]),
                          ('no green close through session 3', at_risk[at_risk.first_green_day.fillna(float("inf")) > 3])]:
            labeled = sub[sub.outcome.isin(['T1', 'STOP'])]
            rate = (labeled.outcome == 'STOP').mean() if len(labeled) else float('nan')
            print(f'  {name}: {len(labeled)} resolved, {len(sub)-len(labeled)} censored; eventual STOP rate {rate:.3f}')
        print('No trading rule changes or promotion follow from this descriptive scorecard.')
    finally:
        con.close()


if __name__ == '__main__':
    main()
