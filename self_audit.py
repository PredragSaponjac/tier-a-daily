# -*- coding: utf-8 -*-
"""SELF-AUDIT — score every registered hypothesis on data that arrived AFTER it was
registered, correct for how many ideas are being tried, and report. NEVER implement.

WHAT THIS IS (2026-09-02, user goal): a system that "rechecks itself and suggests
improvements without overfitting". The honest version of that is NOT a search — a search
over rules is an overfitting machine. It is:

  1. a REGISTRY (hypotheses.json): every idea written down with a date and a frozen
     threshold BEFORE its data exists;
  2. scoring each idea ONLY on rows with scan_date > registered  (out-of-sample by
     construction);
  3. a Bonferroni bar of 0.05 / (number of active ideas) — adding an idea raises the bar
     for all the others;
  4. both halves of the post-registration period must agree in direction;
  5. three verdicts: READY FOR DECISION / ACCUMULATING / NULL — and a human decides.

It also answers the user's standing question directly: "we picked one name; what did the
other qualified names do?" (picked-vs-skipped, from the signal archives), and "which
entry metrics predict SPEED to target?" (prospectively, every feature counted).

Run:  python self_audit.py            prints the digest
      python self_audit.py --send     also sends it to Telegram
      python self_audit.py --json     also writes audit_latest.json (committed record)
"""
import argparse
import datetime as dt
import glob
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
from scipy import stats

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('SKEW_DB_PATH', os.path.join(HERE, 'skew_history.db'))
REGISTRY = os.path.join(HERE, 'hypotheses.json')
ARCHIVES = os.path.join(HERE, 'signals')

SPEED_FEATURES = ['skew', 'skew_change_5d', 'near_skew', 'spot_return_pct', 'cushion_pct',
                  'vol_cushion', 'atm_iv', 'hv_10d', 'iv_hv_ratio', 'sector_iv_rank',
                  'near_dte', 'washouts', 'n_legs']


# ----------------------------------------------------------------- data frame
def load_frame(con):
    """One row per Tier A qualifier with its path labels + entry features + breadth."""
    p = pd.read_sql_query('SELECT * FROM tier_a_paths', con)
    if p.empty:
        return p
    c = pd.read_sql_query("""SELECT ticker, scan_date, sector, spot_close, spot_return_pct,
           put_wall_strike, atm_iv, hv_10d, iv_hv_ratio, skew, skew_change_5d, near_skew,
           near_dte, sector_iv_rank FROM candidate_log""", con)
    b = pd.read_sql_query("""SELECT scan_date, COUNT(*) AS washouts FROM candidate_log
           WHERE spot_return_pct<=-8 GROUP BY scan_date""", con)
    d = p.merge(c, on=['ticker', 'scan_date'], how='left').merge(b, on='scan_date', how='left')
    d['cushion_pct'] = (d.spot_close / d.put_wall_strike - 1) * 100
    d['vol_cushion'] = d.cushion_pct / (d.atm_iv / np.sqrt(252))
    d['sector_iv_rank'] = d['sector_iv_rank'].replace(0.0, np.nan)      # 0.0 = missing-coded
    d['hit_t1'] = (d.outcome == 'T1').astype(float)
    d.loc[d.outcome == 'OPEN', 'hit_t1'] = np.nan                        # unresolved = unlabeled
    try:                                                                  # combo needs skew_slope
        from edge_metrics import skew_slope, SKEW_SLOPE_MAX, SECTOR_IV_RANK_MIN
        sl = [skew_slope(t, s, db_path=DB) for t, s in zip(d.ticker, d.scan_date)]
        d['skew_slope'] = sl
        d['combo_pass'] = ((d.sector_iv_rank >= SECTOR_IV_RANK_MIN) &
                           (d.skew_slope <= SKEW_SLOPE_MAX)).astype(float)
        d.loc[d.sector_iv_rank.isna() | d.skew_slope.isna(), 'combo_pass'] = np.nan
    except Exception:
        d['combo_pass'] = np.nan
    return d


def picked_map(archives_dir):
    """scan_date -> picked ticker, from the committed signal archives."""
    out = {}
    for f in glob.glob(os.path.join(archives_dir, '*.json')):
        try:
            a = json.load(open(f, encoding='utf-8'))
            if a.get('picked_ticker'):
                out[a['scan_date']] = a['picked_ticker']
        except Exception:
            pass
    return out


# ----------------------------------------------------------------- scoring
def _halves_agree(x, eff_fn):
    if x.scan_date.nunique() < 4:
        return None
    med = sorted(x.scan_date.unique())[x.scan_date.nunique() // 2]
    e1, e2 = eff_fn(x[x.scan_date < med]), eff_fn(x[x.scan_date >= med])
    if e1 is None or e2 is None or np.isnan(e1) or np.isnan(e2):
        return None
    return (e1 > 0) == (e2 > 0) and e1 > 0


def _mask(d, feat, op, thr):
    v = d[feat]
    if op == '>=':  return v >= thr
    if op == '<=':  return v <= thr
    if op == '<':   return v < thr
    if op == '==':  return v == thr
    raise ValueError(op)


def score_one(h, d, n_min, p_bar):
    r = {'id': h['id'], 'type': h['type'], 'registered': h['registered'], 'p_bar': p_bar}
    x = d[d.scan_date > h['registered']].copy()
    typ = h['type']

    if typ in ('entry_filter', 'post_entry'):
        feat = h['feature']
        if feat not in x:
            return {**r, 'verdict': 'INSUFFICIENT', 'detail': f'feature {feat} unavailable'}
        if typ == 'post_entry':                       # never-green counts as NOT <= k
            x[feat] = x[feat].fillna(999)
        x = x[x[feat].notna() & x.hit_t1.notna()]
        m = _mask(x, feat, h['op'], h['threshold'])
        a, b = x[m], x[~m]
        r.update(n_pass=len(a), n_fail=len(b))
        if len(a) < n_min or len(b) < n_min:
            return {**r, 'verdict': 'INSUFFICIENT',
                    'detail': f'pass {len(a)} / fail {len(b)} labeled (need {n_min} each)'}
        eff = a.hit_t1.mean() - b.hit_t1.mean()
        tab = [[int(a.hit_t1.sum()), len(a) - int(a.hit_t1.sum())],
               [int(b.hit_t1.sum()), len(b) - int(b.hit_t1.sum())]]
        p = stats.fisher_exact(tab, alternative='greater')[1]
        agree = _halves_agree(x, lambda z: (z[_mask(z, feat, h['op'], h['threshold'])].hit_t1.mean()
                                            - z[~_mask(z, feat, h['op'], h['threshold'])].hit_t1.mean())
                              if len(z) >= 4 else None)
        r.update(effect=round(eff, 3), p=round(p, 4), halves_agree=agree,
                 detail=f'hit-T1 {a.hit_t1.mean():.0%} (n={len(a)}) vs {b.hit_t1.mean():.0%} (n={len(b)})')

    elif typ == 'exit_shadow':
        f, base = h['feature'], h['baseline']
        x = x[x[f].notna() & x[base].notna()]
        r.update(n_pass=len(x), n_fail=len(x))
        if len(x) < n_min:
            return {**r, 'verdict': 'INSUFFICIENT', 'detail': f'{len(x)} resolved (need {n_min})'}
        diff = x[f] - x[base]
        eff = diff.mean()
        p = stats.wilcoxon(diff, alternative='greater').pvalue if diff.abs().sum() > 0 else 1.0
        agree = _halves_agree(x, lambda z: (z[f] - z[base]).mean() if len(z) >= 3 else None)
        r.update(effect=round(eff, 3), p=round(p, 4), halves_agree=agree,
                 detail=f'R {x[f].mean():+.3f} vs live {x[base].mean():+.3f} (n={len(x)})')

    elif typ == 'portfolio':
        pm = picked_map(ARCHIVES)
        x = x[x.scan_date.isin(pm) & x.r_live.notna()].copy()
        x['picked'] = [pm.get(s) == t for s, t in zip(x.scan_date, x.ticker)]
        a, b = x[x.picked], x[~x.picked]
        r.update(n_pass=len(a), n_fail=len(b))
        if len(a) < n_min or len(b) < n_min:
            return {**r, 'verdict': 'INSUFFICIENT',
                    'detail': f'picked {len(a)} / skipped {len(b)} resolved since archives began (need {n_min} each)'}
        p_rank = stats.mannwhitneyu(a.r_live, b.r_live, alternative='greater').pvalue
        eff = b.r_live.mean() - a.r_live.mean()          # positive = skipped did BETTER
        agree = _halves_agree(x, lambda z: (z[~z.picked].r_live.mean() - z[z.picked].r_live.mean())
                              if z.picked.sum() >= 2 and (~z.picked).sum() >= 2 else None)
        r.update(effect=round(eff, 3), p=round(1 - p_rank, 4), halves_agree=agree,
                 detail=f'SKIPPED R {b.r_live.mean():+.3f} (n={len(b)}) vs PICKED {a.r_live.mean():+.3f} '
                        f'(n={len(a)}); p(picked>skipped)={p_rank:.2f}')

    elif typ == 'speed':
        w = x[x.hit_t1 == 1.0]
        feats = SPEED_FEATURES if h['feature'] == '*' else [h['feature']]
        rows = []
        for f in feats:
            s = w[[f, 'days_to_t1']].dropna() if f in w else pd.DataFrame()
            if len(s) >= n_min:
                rho, p = stats.spearmanr(s[f], s.days_to_t1)
                rows.append((f, rho, p, len(s)))
        r.update(n_pass=len(w), n_fail=0, features_tested=len(feats))
        if not rows:
            return {**r, 'verdict': 'INSUFFICIENT', 'detail': f'{len(w)} winners with days_to_t1 (need {n_min})'}
        best = min(rows, key=lambda t: t[2])
        want_neg = h.get('direction') == 'negative_rho'
        eff = -best[1] if want_neg else abs(best[1])
        r.update(effect=round(eff, 3), p=round(best[2], 4), halves_agree=None,
                 detail=f'best {best[0]} rho={best[1]:+.2f} p={best[2]:.3f} (n={best[3]}, {len(feats)} features counted)')
    else:
        return {**r, 'verdict': 'INSUFFICIENT', 'detail': f'unknown type {typ}'}

    # ---- verdict
    eff, p, agree = r['effect'], r['p'], r.get('halves_agree')
    if eff <= 0 or (p > 0.5 and (r['n_pass'] + r['n_fail']) >= 2 * n_min):
        r['verdict'] = 'NULL'
    elif p < p_bar and (agree is True or agree is None and typ == 'speed'):
        r['verdict'] = 'READY FOR DECISION'
    else:
        r['verdict'] = 'ACCUMULATING'
    return r


def score_registry(con, registry, today=None, archives_dir=None):
    global ARCHIVES
    if archives_dir:
        ARCHIVES = archives_dir
    d = load_frame(con)
    active = [h for h in registry['hypotheses'] if h.get('status') == 'active']
    # multiplicity: every active idea, and every feature a wildcard speed test touches
    n_tests = sum(len(SPEED_FEATURES) if (h['type'] == 'speed' and h['feature'] == '*') else 1
                  for h in active)
    p_bar = 0.05 / max(n_tests, 1)
    n_min = int(registry.get('n_min', 10))
    if d.empty:
        return [{'id': h['id'], 'type': h['type'], 'registered': h['registered'], 'p_bar': p_bar,
                 'verdict': 'INSUFFICIENT', 'detail': 'no path labels yet'} for h in active], p_bar, n_tests
    out = []
    for h in active:
        try:
            out.append(score_one(h, d, n_min, p_bar))
        except Exception as e:
            out.append({'id': h['id'], 'type': h['type'], 'registered': h['registered'],
                        'p_bar': p_bar, 'verdict': 'ERROR', 'detail': f'{type(e).__name__}: {e}'})
    return out, p_bar, n_tests


# ----------------------------------------------------------------- digest
def digest(results, p_bar, n_tests, con):
    tot = con.execute('SELECT COUNT(*), SUM(complete) FROM tier_a_paths').fetchone()
    lines = [f'🔬 TIER A SELF-AUDIT — {dt.date.today()}',
             f'{tot[0]} qualified names path-labeled ({tot[1]} resolved). '
             f'{n_tests} ideas under test → corrected bar p<{p_bar:.4f}.',
             'Each idea scores ONLY on signals after it was registered. Nothing is auto-applied.', '']
    order = ['READY FOR DECISION', 'ACCUMULATING', 'INSUFFICIENT', 'NULL', 'ERROR']
    icon = {'READY FOR DECISION': '🟢', 'ACCUMULATING': '🟡', 'INSUFFICIENT': '⚪', 'NULL': '🔴', 'ERROR': '⚠️'}
    for v in order:
        grp = [r for r in results if r['verdict'] == v]
        if not grp:
            continue
        lines.append(f'{icon[v]} {v} ({len(grp)})')
        for r in grp:
            extra = ''
            if 'p' in r:
                extra = f"  eff {r['effect']:+.3f}  p={r['p']:.3f}" + \
                        (f"  halves {'agree' if r['halves_agree'] else ('SPLIT' if r['halves_agree'] is False else 'n/a')}")
            lines.append(f"  • {r['id']} (reg {r['registered']}): {r['detail']}{extra}")
        lines.append('')
    if any(r['verdict'] == 'READY FOR DECISION' for r in results):
        lines.append('🟢 = cleared its bar on fresh data. This is a DECISION for you, not a change — '
                     'say the word and it gets implemented; say no and it keeps tracking.')
    else:
        lines.append('No idea has cleared its bar yet. Rules unchanged. Keep collecting.')
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    registry = json.load(open(REGISTRY, encoding='utf-8'))
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS tier_a_paths (ticker TEXT, scan_date TEXT, tradeable INTEGER,
        n_legs INTEGER, entry REAL, first_green_day INTEGER, days_to_t1 INTEGER, days_to_stop INTEGER,
        mae_pct REAL, mfe_pct REAL, outcome TEXT, pnl_pct REAL, r_live REAL, r_stop5 REAL, r_stop6 REAL,
        r_t12 REAL, bars_seen INTEGER, complete INTEGER, labeled_at TEXT, PRIMARY KEY (ticker, scan_date))""")
    results, p_bar, n_tests = score_registry(con, registry)
    text = digest(results, p_bar, n_tests, con)
    print(text)
    if a.json:
        json.dump({'date': dt.date.today().isoformat(), 'p_bar': p_bar, 'n_tests': n_tests,
                   'results': results}, open(os.path.join(HERE, 'audit_latest.json'), 'w',
                                             encoding='utf-8'), indent=2, default=str)
        print('\n[audit] wrote audit_latest.json')
    if a.send:
        try:
            from dotenv import load_dotenv; load_dotenv()
        except Exception:
            pass
        from alert import send_telegram
        print('[audit] telegram:', 'sent' if send_telegram(text) else 'FAILED')
    con.close()


if __name__ == '__main__':
    main()
