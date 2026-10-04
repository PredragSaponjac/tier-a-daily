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
    # RE-AUDIT R9 (2026-10-04): score ONE labelling engine at a time. Rows from an older
    # engine (a relabel cut short, a failed download) are left out here, and
    # label_coverage() blocks READY until every qualifier carries the current version.
    import path_labels as PL
    if 'label_version' in p:
        p = p[p['label_version'] == PL.LABEL_VERSION]
    else:
        p = p.iloc[0:0]
    if p.empty:
        return p
    # SELECT * on purpose (fixed 2026-09-26). A hand-written column list silently made
    # S3 (ticker_vix) and S4 (sector_breadth_skew_down) UNSCOREABLE from the day they were
    # registered: they still counted toward the Bonferroni divisor, raising the bar for
    # every other idea, while never producing a verdict. Any registered feature that
    # exists in candidate_log must be reachable without editing this query.
    c = pd.read_sql_query('SELECT * FROM candidate_log', con)
    b = pd.read_sql_query("""SELECT scan_date, COUNT(*) AS washouts FROM candidate_log
           WHERE spot_return_pct<=-8 GROUP BY scan_date""", con)
    d = p.merge(c, on=['ticker', 'scan_date'], how='left').merge(b, on='scan_date', how='left')
    # Same universe as the live bot (2026-09-02): drop leveraged ETFs/ETNs + sector-Unknown.
    # Defensive — path_labels now excludes them too — so stale rows can never leak in.
    try:
        from scanner_reader import EXCLUDED_ETFS
        d = d[~d.ticker.isin(EXCLUDED_ETFS) & (d.sector.fillna('Unknown') != 'Unknown')]
    except Exception:
        pass
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
    """True / False / None. Always a PYTHON bool, never numpy.bool_.

    AUDIT F12 (2026-10-04): this returned numpy.bool_, and the verdict tested
    `agree is True` — which is False for numpy.bool_(True). So from 2026-09-02 no
    entry_filter, post_entry, exit_shadow or portfolio idea could EVER reach READY,
    however strong the evidence. The promotion mechanism was dead on arrival.
    """
    if x.scan_date.nunique() < 4:
        return None
    med = sorted(x.scan_date.unique())[x.scan_date.nunique() // 2]
    e1, e2 = eff_fn(x[x.scan_date < med]), eff_fn(x[x.scan_date >= med])
    if e1 is None or e2 is None or np.isnan(e1) or np.isnan(e2):
        return None
    return bool((e1 > 0) and (e2 > 0))


def _verdict(eff, p, agree, n_total, n_min, p_bar):
    """Pure decision rule, unit-tested in preflight. Uses FULL-PRECISION inputs.

    AUDIT F12: decisions used p rounded to 4 places, so a raw p of 0.0017478 became
    0.0017 and cleared a 0.0017241 bar it actually missed. And speed ideas were waved
    through with agree=None, skipping the both-halves rule every other type obeys.
    Now: READY needs p < bar AND agree is exactly True, for every type.
    """
    if eff is None or not np.isfinite(eff) or eff <= 0:
        return 'NULL'
    if p > 0.5 and n_total >= 2 * n_min:
        return 'NULL'
    # `agree is not None and bool(agree)`, NOT `agree is True`: numpy.bool_(True) is True
    # evaluates False, which is exactly how promotion died. Robust to either type.
    if p < p_bar and agree is not None and bool(agree):
        return 'READY FOR DECISION'
    return 'ACCUMULATING'


def _open_at(x, day):
    """Rows still at risk at the END of `day` — resolution strictly later than `day`.
    AUDIT F14: a post-entry signal is acted on at its landmark, so only positions still
    open then are in its risk set. Counting trades that had already stopped made
    'never green by day 3' look perfect: 15 of the 18 never-green losers had stopped
    before day 3 could act on them.
    RE-AUDIT R8: a path that matured with NEITHER event (legacy EXPIRED rows) has no event
    day and was dropped although it was alive at the landmark; its resolution is the last
    bar seen. OPEN rows stay out: their outcome is unknown (censored), not a failure."""
    res = x['days_to_t1'].fillna(x['days_to_stop'])
    if 'outcome' in x and 'bars_seen' in x:
        res = res.fillna(x['bars_seen'].where(x['outcome'] == 'EXPIRED'))
    return x[res > day]


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
            if h.get('landmark_day') is not None:     # F14: risk set = still open at landmark
                x = _open_at(x, int(h['landmark_day']))
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
        r.update(effect_raw=float(eff), p_raw=float(p), halves_agree=agree,
                 detail=f'hit-T1 {a.hit_t1.mean():.0%} (n={len(a)}) vs {b.hit_t1.mean():.0%} (n={len(b)})')

    elif typ == 'exit_shadow':
        f, base = h['feature'], h['baseline']
        # F21: resolved paths only. OPEN rows carry a provisional mark-to-market r_live.
        x = x[(x['complete'] == 1) & x[f].notna() & x[base].notna()]
        r.update(n_pass=len(x), n_fail=len(x))
        if len(x) < n_min:
            return {**r, 'verdict': 'INSUFFICIENT', 'detail': f'{len(x)} resolved (need {n_min})'}
        diff = x[f] - x[base]
        eff = diff.mean()
        p = stats.wilcoxon(diff, alternative='greater').pvalue if diff.abs().sum() > 0 else 1.0
        agree = _halves_agree(x, lambda z: (z[f] - z[base]).mean() if len(z) >= 3 else None)
        r.update(effect_raw=float(eff), p_raw=float(p), halves_agree=agree,
                 detail=f'R {x[f].mean():+.3f} vs live {x[base].mean():+.3f} (n={len(x)})')

    elif typ == 'portfolio':
        pm = picked_map(ARCHIVES)
        # F21: was `r_live.notna()`, which counted the still-OPEN 9/29 STLA as resolved.
        x = x[x.scan_date.isin(pm) & x.outcome.isin(['T1', 'STOP'])].copy()
        x['picked'] = [pm.get(s) == t for s, t in zip(x.scan_date, x.ticker)]
        a, b = x[x.picked], x[~x.picked]
        r.update(n_pass=len(a), n_fail=len(b))
        if len(a) < n_min or len(b) < n_min:
            return {**r, 'verdict': 'INSUFFICIENT',
                    'detail': f'picked {len(a)} / skipped {len(b)} resolved since archives began (need {n_min} each)'}
        # F12: test the registered alternative DIRECTLY (skipped > picked). The old
        # `1 - p(picked > skipped)` is not the same p-value once ties and the continuity
        # correction are involved.
        p_dir = stats.mannwhitneyu(b.r_live, a.r_live, alternative='greater').pvalue
        eff = b.r_live.mean() - a.r_live.mean()          # positive = skipped did BETTER
        agree = _halves_agree(x, lambda z: (z[~z.picked].r_live.mean() - z[z.picked].r_live.mean())
                              if z.picked.sum() >= 2 and (~z.picked).sum() >= 2 else None)
        r.update(effect_raw=float(eff), p_raw=float(p_dir), halves_agree=agree,
                 detail=f'SKIPPED R {b.r_live.mean():+.3f} (n={len(b)}) vs PICKED {a.r_live.mean():+.3f} '
                        f'(n={len(a)}); p(skipped>picked)={p_dir:.3f}')

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
        # F12: speed now obeys the both-halves rule like every other type. The rho for
        # the chosen feature must point the registered way in BOTH halves of the sample.
        bf = best[0]
        # RE-AUDIT R3 (2026-10-04): with no registered direction each half's rho went
        # through abs(), so rho +1 in one half and -1 in the other "agreed" and could
        # promote. Both halves must now point the same way as the full-sample rho.
        full_sign = 1.0 if best[1] >= 0 else -1.0
        sgn = (lambda rr: -rr) if want_neg else (lambda rr: rr * full_sign)
        def _half_rho(z):
            zz = z[[bf, 'days_to_t1']].dropna()
            return sgn(stats.spearmanr(zz[bf], zz.days_to_t1)[0]) if len(zz) >= 4 else None
        agree = _halves_agree(w, _half_rho)
        r.update(effect_raw=float(eff), p_raw=float(best[2]), halves_agree=agree,
                 detail=f'best {bf} rho={best[1]:+.2f} p={best[2]:.3f} (n={best[3]}, {len(feats)} features counted)')
    else:
        return {**r, 'verdict': 'INSUFFICIENT', 'detail': f'unknown type {typ}'}

    # ---- verdict on FULL-PRECISION values; rounded copies are for display only
    r['verdict'] = _verdict(r['effect_raw'], r['p_raw'], r.get('halves_agree'),
                            r['n_pass'] + r['n_fail'], n_min, p_bar)
    r['effect'], r['p'] = round(r['effect_raw'], 3), round(r['p_raw'], 4)
    return r


def label_coverage(con, today=None) -> dict:
    """RE-AUDIT R9 (2026-10-04): does the path table cover EVERY qualifier with the CURRENT
    labelling engine? A partial relabel would otherwise let two engines' labels be scored as
    one cohort while the job reported success. Qualifiers from the latest scan date are not
    expected yet (no session after entry)."""
    import path_labels as PL
    today = today or dt.date.today()
    if isinstance(today, str):
        today = dt.date.fromisoformat(today)
    try:
        exp = PL.qualifiers(con, today)
        latest = con.execute('SELECT MAX(scan_date) FROM candidate_log').fetchone()[0]
        exp = exp[exp.scan_date < latest]
        have = pd.read_sql_query('SELECT ticker, scan_date, label_version FROM tier_a_paths', con)
    except Exception as e:
        return {'ok': False, 'detail': f'coverage check failed: {type(e).__name__}: {e}'}
    cur = have[have['label_version'] == PL.LABEL_VERSION] if 'label_version' in have else have.iloc[0:0]
    want, got = set(zip(exp.ticker, exp.scan_date)), set(zip(cur.ticker, cur.scan_date))
    missing = sorted(want - got)
    stale = int(len(have) - len(cur))
    return {'ok': not missing and stale == 0, 'version': PL.LABEL_VERSION,
            'expected': len(want), 'current': len(want & got), 'stale_rows': stale,
            'missing': [f'{t} {s}' for t, s in missing[:20]],
            'detail': (f'labels v{PL.LABEL_VERSION}: {len(want & got)} of {len(want)} qualifiers current'
                       + (f', {stale} older-version row(s)' if stale else '')
                       + (f', missing {len(missing)}' if missing else ''))}


_COVERAGE = None


def score_registry(con, registry, today=None, archives_dir=None):
    global ARCHIVES, _FRAME, _COVERAGE
    if archives_dir:
        ARCHIVES = archives_dir
    _COVERAGE = label_coverage(con, today)
    d = load_frame(con)
    _FRAME = d                                   # reused by loser_ledger (no second skew_slope pass)
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
    # R9: no promotion off a cohort that mixes labelling engines or misses qualifiers
    if not _COVERAGE.get('ok'):
        for r in out:
            if r.get('verdict') == 'READY FOR DECISION':
                r['verdict'] = 'ACCUMULATING'
                r['detail'] = f"{r.get('detail', '')} — READY BLOCKED: {_COVERAGE.get('detail')}"
    return out, p_bar, n_tests


_FRAME = None


def loser_ledger(registry):
    """LEARN FROM LOSERS, EVERY WEEK (user request 2026-09-02).

    Replays every registered ENTRY rule against every resolved qualifier and charges
    each rule with the WINNERS it would also have blocked. A rule only earns 'helps' if
    it blocks losers without eating winners. Descriptive — a replay, not a verdict — and
    in-sample for ideas registered before the rows, which is why it never promotes
    anything on its own. Also names the losers NO rule would have caught: those are
    variance until a rule proves otherwise, and treating them as mistakes is how
    overfitting starts.
    """
    d = _FRAME
    if d is None or d.empty or registry is None:
        return []
    r = d[d.outcome.isin(['T1', 'STOP'])].copy()
    L, W = r[r.outcome == 'STOP'], r[r.outcome == 'T1']
    if L.empty:
        return ['📕 LOSER LEDGER: no stopped qualifiers yet.', '']
    lines = [f'📕 LOSER LEDGER — {len(L)} stopped vs {len(W)} hit-T1 among ALL resolved qualifiers '
             f'(descriptive replay; in-sample for older ideas):']
    unavoidable = set(L.ticker + '@' + L.scan_date)
    for h in [h for h in registry['hypotheses'] if h.get('status') == 'active'
              and h['type'] in ('entry_filter', 'post_entry')]:
        f = h['feature']
        if f not in r:
            continue
        x = r.copy()
        if h['type'] == 'post_entry':
            x[f] = x[f].fillna(999)
            # AUDIT F14: a post-entry rule can only act on positions still open at its
            # landmark. Without this the ledger showed H5 as '18 losers / 0 winners' when
            # 15 of those 18 had already stopped before day 3 — a hindsight label.
            if h.get('landmark_day') is not None:
                x = _open_at(x, int(h['landmark_day']))
        x = x[x[f].notna()]
        m = _mask(x, f, h['op'], h['threshold'])
        lb, wb = x[(~m) & (x.outcome == 'STOP')], x[(~m) & (x.outcome == 'T1')]
        if h['type'] == 'entry_filter':          # only ENTRY rules count as "avoidance";
            unavoidable -= set(lb.ticker + '@' + lb.scan_date)   # post-entry rules cannot avoid a trade
        if len(lb) >= len(wb) + 2 and len(wb) <= max(1, len(W) // 10):
            tag = 'helps'
        elif len(wb) and len(wb) >= len(lb):
            tag = 'HURTS'
        else:
            tag = 'wash'
        lines.append(f"  {h['id']:28s} blocks {len(lb):2d} losers / {len(wb):2d} winners  → {tag}")
    lines.append(f'  losers NO registered entry rule would have caught: {len(unavoidable)} of {len(L)} '
                 f'← variance until a rule proves otherwise')
    for _, x in L.sort_values('scan_date').tail(3).iterrows():
        g = '-' if pd.isna(x.first_green_day) else f'd{int(x.first_green_day)}'
        s = '-' if pd.isna(x.days_to_stop) else f'd{int(x.days_to_stop)}'
        lines.append(f'  latest: {x.ticker} {x.scan_date}  first green {g}  peak {x.mfe_pct:+.1f}%  stopped {s}')
    return lines + ['']


# ----------------------------------------------------------------- digest
def digest(results, p_bar, n_tests, con, registry=None):
    tot = con.execute('SELECT COUNT(*), SUM(complete) FROM tier_a_paths').fetchone()
    lines = [f'🔬 TIER A SELF-AUDIT — {dt.date.today()}',
             f'{tot[0]} qualified names path-labeled ({tot[1]} resolved). '
             f'{n_tests} ideas under test → corrected bar p<{p_bar:.4f}.',
             'Each idea scores ONLY on signals after it was registered. Nothing is auto-applied.']
    if _COVERAGE is not None:
        lines.append(('✅ ' if _COVERAGE.get('ok') else '⚠️ READY blocked — ') + str(_COVERAGE.get('detail'))
                     + (f" (missing: {', '.join(_COVERAGE['missing'][:5])})" if _COVERAGE.get('missing') else ''))
    lines.append('')
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
    lines += loser_ledger(registry)
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
    text = digest(results, p_bar, n_tests, con, registry)
    print(text)
    if a.json:
        json.dump({'date': dt.date.today().isoformat(), 'p_bar': p_bar, 'n_tests': n_tests,
                   'label_coverage': _COVERAGE,
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
