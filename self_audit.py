# -*- coding: utf-8 -*-
"""Weekly descriptive research, with a frozen prospective single-checkpoint protocol.

Historical registration dates are evidence history, not proof of preregistration.
IID Fisher/Wilcoxon/Spearman statistics are exploratory: correlated trades and
repeated weekly looks invalidate automatic promotion. Version 5 never issues READY.
One fixed checkpoint produces an immutable descriptive review with cluster-aware
sensitivity evidence and visible censoring. Calibrated promotion requires a NEW,
validated protocol frozen before a new future cohort, not reuse of these results.

Run --freeze-protocol explicitly before the prospective cohort starts, after all
engine changes are final. --json preserves immutable history; --send sends a digest.
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('SKEW_DB_PATH', os.path.join(HERE, 'skew_history.db'))
REGISTRY = os.path.join(HERE, 'hypotheses.json')
ARCHIVES = os.path.join(HERE, 'signals')
PROTOCOL_LOCK = os.path.join(HERE, 'inference_protocol_lock.json')
ENGINE_FILES = ('self_audit.py', 'path_labels.py', 'exits.py', 'market_time.py',
                'parameters.json', 'skew_tracker.py', 'scanner_reader.py', 'legs.py',
                'data_quality.py')

SPEED_FEATURES = ['skew', 'skew_change_5d', 'near_skew', 'spot_return_pct', 'cushion_pct',
                  'vol_cushion', 'atm_iv', 'hv_10d', 'iv_hv_ratio', 'sector_iv_rank',
                  'near_dte', 'washouts', 'n_legs']


# ----------------------------------------------------------------- data frame
def load_frame(con):
    """One row per Tier A qualifier with its path labels + entry features + breadth."""
    p = pd.read_sql_query('SELECT * FROM tier_a_paths', con)
    if p.empty:
        return p
    # Version 5 is a new prospective engine; older rows are preserved, never relabeled
    # or pooled with it. Coverage is assessed on this prospective cohort only.
    import path_labels as PL
    if 'label_version' in p:
        p = p[p['label_version'] == PL.LABEL_VERSION]
    else:
        p = p.iloc[0:0]
    if p.empty:
        return p
    # Replay the exact first-label feature snapshot, not today's candidate/peer history.
    if 'signal_features_json' not in p or p.signal_features_json.isna().any():
        raise ValueError('version-5 signal feature snapshots missing')
    c = pd.DataFrame([json.loads(v) for v in p.signal_features_json])
    # Keep path labels authoritative when same-name fields exist in scanner snapshots.
    fields = ['ticker', 'scan_date'] + [col for col in c if col not in p.columns]
    d = p.merge(c[fields], on=['ticker', 'scan_date'], how='left', validate='one_to_one')
    # Same universe as the live bot (2026-09-02): drop leveraged ETFs/ETNs + sector-Unknown.
    # Defensive — path_labels now excludes them too — so stale rows can never leak in.
    from scanner_reader import EXCLUDED_ETFS
    d = d[~d.ticker.isin(EXCLUDED_ETFS) & (d.sector.fillna('Unknown') != 'Unknown')]
    d['cushion_pct'] = (d.spot_close / d.put_wall_strike - 1) * 100
    d['vol_cushion'] = d.cushion_pct / (d.atm_iv / np.sqrt(252))
    d['sector_iv_rank'] = d['sector_iv_rank'].replace(0.0, np.nan)      # 0.0 = missing-coded
    d['hit_t1'] = (d.outcome == 'T1').astype(float)
    d.loc[~d.outcome.isin(['T1', 'STOP']), 'hit_t1'] = np.nan
    return d


def picked_map(archives_dir):
    """scan_date -> picked ticker, from the committed signal archives."""
    out = {}
    for f in glob.glob(os.path.join(archives_dir, '*.json')):
        try:
            a = json.loads(Path(f).read_text(encoding='utf-8'))
            if a.get('picked_ticker'):
                out[a['scan_date']] = a['picked_ticker']
        except Exception as e:
            raise ValueError(f'unreadable signal archive {f}: {e}') from e
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
    if p is None or not np.isfinite(p):
        return 'INSUFFICIENT'
    if p > 0.5 and n_total >= 2 * n_min:
        return 'NULL'
    # `agree is not None and bool(agree)`, NOT `agree is True`: numpy.bool_(True) is True
    # evaluates False, which is exactly how promotion died. Robust to either type.
    if p < p_bar and agree is not None and bool(agree):
        return 'READY FOR DECISION'
    return 'ACCUMULATING'


def _open_at(x, day):
    """Risk set at the completed landmark: alive and observed through that session.

    Include censored paths that were alive then; outcome availability is a separate
    scoring restriction. An event on the landmark is already resolved at its close.
    Legacy EXPIRED has a known observation horizon, not an event or a known failure.
    """
    res = x['days_to_t1'].fillna(x['days_to_stop'])
    observed = x['bars_seen'] >= day if 'bars_seen' in x else res > day
    return x[observed & (res.isna() | (res > day))]


def _post_feature(x, feature):
    # Only first_green_day has a defined NULL meaning (no observed green close).
    # Missing OI is unknown, never a passing high value such as 999.
    return x[feature].fillna(np.inf) if feature == 'first_green_day' else x[feature]


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
            x[feat] = _post_feature(x, feat)
            if h.get('landmark_day') is not None:     # F14: risk set = still open at landmark
                x = _open_at(x, int(h['landmark_day']))
        r['n_at_risk'] = len(x)
        r['n_censored'] = int(x.hit_t1.isna().sum())
        r['n_missing_feature'] = int(x[feat].isna().sum())
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
            if len(s) >= n_min and s[f].nunique() > 1 and s.days_to_t1.nunique() > 1:
                rho, p = stats.spearmanr(s[f], s.days_to_t1)
                if np.isfinite(rho) and np.isfinite(p):
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
    """Expected completed next-open entries in the frozen prospective scanner cohort.

    Old label versions outside this cohort are deliberately preserved. Missing,
    stale or untraceable expected rows are failures, not merely a promotion warning.
    """
    import path_labels as PL
    today = today or dt.date.today()
    if isinstance(today, str):
        today = dt.date.fromisoformat(today)
    try:
        exp = PL.expected_qualifiers(con, today)
        have = pd.read_sql_query('SELECT * FROM tier_a_paths', con)
    except Exception as e:
        return {'ok': False, 'detail': f'coverage check failed: {type(e).__name__}: {e}'}
    cur = have[have['label_version'] == PL.LABEL_VERSION] if 'label_version' in have else have.iloc[0:0]
    want, got = set(zip(exp.ticker, exp.scan_date)), set(zip(cur.ticker, cur.scan_date))
    missing = sorted(want - got)
    legacy = int(len(have) - len(cur))
    stale = sum((t, s) in want and v != PL.LABEL_VERSION
                for t, s, v in have[['ticker', 'scan_date', 'label_version']].itertuples(index=False, name=None))
    broken = []
    from market_time import last_completed_session
    asof = min(last_completed_session(), dt.date.fromisoformat(PL.protocol()['observation_cutoff'])).isoformat()
    for row in cur.to_dict('records'):
        if (row['ticker'], row['scan_date']) not in want:
            continue
        if not row.get('price_hash') or not row.get('entry_date') or not row.get('observed_through'):
            broken.append(f"{row['ticker']} {row['scan_date']}: missing price/session provenance")
        if row.get('entry_policy') != 'next_regular_open' or row.get('outcome') not in ('T1', 'STOP', 'OPEN'):
            broken.append(f"{row['ticker']} {row['scan_date']}: incompatible execution/censoring")
        if str(row.get('observed_through') or '') > PL.protocol()['observation_cutoff']:
            broken.append(f"{row['ticker']} {row['scan_date']}: observed after frozen protocol cutoff")
        try:
            frozen = pd.DataFrame(json.loads(row['price_inputs_json'])['bars'])
            if PL.price_hash(frozen) != row['price_hash']:
                raise ValueError('input payload hash mismatch')
            if frozen.empty or frozen.iloc[0].date != row['entry_date'] or frozen.iloc[-1].date != row['observed_through']:
                raise ValueError('input session provenance mismatch')
            replay = PL.label_one(frozen, row['scan_date'])
            for field in ('entry', 'entry_date', 'exit_date', 'first_green_day', 'outcome',
                          'days_to_t1', 'days_to_stop', 'pnl_pct', 'r_live', *PL.SHADOWS):
                actual, expected = row.get(field), replay[field]
                if pd.isna(actual) and expected is None:
                    continue
                if isinstance(expected, (int, float)) and actual is not None and pd.notna(actual):
                    same = bool(np.isclose(float(actual), float(expected), rtol=0, atol=1e-6))
                else:
                    same = actual == expected
                if not same:
                    raise ValueError(f'{field} does not replay from the frozen inputs')
            signal = json.loads(row['signal_features_json'])
            if signal.get('ticker') != row['ticker'] or signal.get('scan_date') != row['scan_date']:
                raise ValueError('signal feature snapshot key mismatch')
        except Exception as e:
            broken.append(f"{row['ticker']} {row['scan_date']}: input provenance invalid: {e}")
        all_resolved = row.get('complete') == 1 and all(pd.notna(row.get(c)) for c in PL.SHADOWS)
        if not all_resolved and row.get('observed_through') != asof:
            broken.append(f"{row['ticker']} {row['scan_date']}: unresolved path/shadow not observed through {asof}")
    return {'ok': not missing and not broken, 'version': PL.LABEL_VERSION,
            'expected': len(want), 'current': len(want & got), 'stale_rows': stale,
            'legacy_rows_preserved': legacy, 'invalid': broken[:20],
            'missing': [f'{t} {s}' for t, s in missing[:20]],
            'detail': (f'labels v{PL.LABEL_VERSION}: {len(want & got)} of {len(want)} qualifiers current'
                       + (f', {legacy} historical row(s) preserved' if legacy else '')
                       + (f', invalid {len(broken)}' if broken else '')
                       + (f', missing {len(missing)}' if missing else ''))}


_COVERAGE = None
_INFERENCE_STATUS = None
_CLUSTER_EVIDENCE = None


def protocol_fingerprint(registry, root=HERE):
    """Rules, planned looks and engine bytes are frozen; prose/history notes are not tests."""
    keys = ('id', 'registered', 'prospective_registered', 'status', 'type', 'feature',
            'baseline', 'op', 'threshold', 'outcome', 'direction', 'landmark_day',
            'inference_version', 'normalization')
    specs = [{k: h[k] for k in keys if k in h} for h in registry['hypotheses']]
    # Git enforces LF, while a Windows working tree may still contain CRLF/BOM.
    # Canonical text hashing makes the same source match on Windows and CI.
    engines = {f: hashlib.sha256((Path(root) / f).read_text(encoding='utf-8-sig')
                                .replace('\r\n', '\n').encode('utf-8')).hexdigest() for f in ENGINE_FILES}
    payload = {'protocol': registry['inference_protocol'], 'n_min': registry['n_min'],
               'hypotheses': specs, 'engines': engines, 'speed_features': SPEED_FEATURES}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), payload


def freeze_protocol(registry, lock_path=PROTOCOL_LOCK, today=None, root=HERE):
    """Explicit operator action before the cohort begins; never run automatically weekly."""
    today = today or dt.date.today()
    p = registry['inference_protocol']
    if p['cohort_start'] <= today.isoformat():
        raise ValueError('Cannot freeze after prospective data began; register a new future cohort/version')
    fingerprint, payload = protocol_fingerprint(registry, root)
    record = {'frozen_on': today.isoformat(), 'fingerprint': fingerprint, **payload}
    target = Path(lock_path)
    if target.exists():
        if json.loads(target.read_text(encoding='utf-8'))['fingerprint'] != fingerprint:
            raise ValueError('Existing inference lock differs; preserve it and register a new version')
        return record
    with target.open('x', encoding='utf-8') as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write('\n')
    return record


def inference_status(registry, today=None, lock_path=PROTOCOL_LOCK):
    today = today or dt.date.today()
    if isinstance(today, str):
        today = dt.date.fromisoformat(today)
    p = registry.get('inference_protocol', {})
    try:
        lock = json.loads(Path(lock_path).read_text(encoding='utf-8'))
        fingerprint, _ = protocol_fingerprint(registry)
        if lock['fingerprint'] != fingerprint:
            raise ValueError('frozen registry/engine changed; new future protocol required')
        if lock['frozen_on'] >= p['cohort_start']:
            raise ValueError('protocol was not frozen before this cohort')
        if p.get('promotion_enabled') or p.get('planned_looks') != 1:
            raise ValueError('v1 is a single descriptive review, not a calibrated promotion test')
    except Exception as e:
        return {'ok': False, 'detail': f'inference protocol invalid: {type(e).__name__}: {e}'}
    return {'ok': True, 'version': p['version'], 'checkpoint_due': today.isoformat() >= p['checkpoint_date'],
            'fingerprint': fingerprint, 'checkpoint_date': p['checkpoint_date'],
            'detail': f"{p['version']}: descriptive weekly report; one review on {p['checkpoint_date']}; automatic READY disabled"}


def cluster_evidence(d, registry):
    """Resample whole connected components, preserving date/ticker/holding-window dependence.

    This sensitivity interval does not turn observational data into an independent
    experiment. OPEN outcomes are unknown; censoring counts remain visible.
    """
    p = registry['inference_protocol']
    out = {'method': p['cluster_method'], 'n_paths': len(d), 'n_resolved': 0,
           'n_censored': 0, 'n_clusters': 0, 'promotion_evidence': False}
    if d.empty:
        return out
    x = d.reset_index(drop=True)
    out['n_censored'] = int((~x.outcome.isin(['T1', 'STOP'])).sum())
    parent = list(range(len(x)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    def join(a, b):
        parent[find(b)] = find(a)
    intervals = []
    for row in x.to_dict('records'):
        start = str(row.get('entry_date') or row['scan_date'])
        end = row.get('exit_date') if row.get('outcome') in ('T1', 'STOP') else None
        if end is None or pd.isna(end):
            end = row.get('observed_through') or start
        intervals.append((start, str(end)))
    for i in range(len(x)):
        for j in range(i):
            overlap = max(intervals[i][0], intervals[j][0]) <= min(intervals[i][1], intervals[j][1])
            if x.at[i, 'scan_date'] == x.at[j, 'scan_date'] or x.at[i, 'ticker'] == x.at[j, 'ticker'] or overlap:
                join(i, j)
    x['_cluster'] = [find(i) for i in range(len(x))]
    out['n_clusters'] = x._cluster.nunique()
    resolved = x[x.outcome.isin(['T1', 'STOP']) & x.pnl_pct.notna()]
    out['n_resolved'] = len(resolved)
    if resolved.empty:
        return out
    out['mean_resolved_pnl_pct'] = float(resolved.pnl_pct.mean())
    grouped = resolved.groupby('_cluster').pnl_pct.agg(['sum', 'count'])
    out['n_resolved_clusters'] = len(grouped)
    if len(grouped) < p['min_clusters_for_interval']:
        out['interval_unavailable'] = f"need {p['min_clusters_for_interval']} independent components; have {len(grouped)}"
        return out
    rng = np.random.default_rng(p['bootstrap_seed'])
    draws = rng.integers(0, len(grouped), size=(p['bootstrap_draws'], len(grouped)))
    samples = grouped['sum'].to_numpy()[draws].sum(axis=1) / grouped['count'].to_numpy()[draws].sum(axis=1)
    out['descriptive_95pct_interval'] = [float(v) for v in np.quantile(samples, [.025, .975])]
    out['warning'] = 'Resolved-only, price-only hypothetical P&L; censoring/selection bias and assumptions remain. Not a promotion test.'
    return out


def feature_coverage(d, registry):
    """Missing-by-design future landmarks and censored shadows are not data failures."""
    errors = []
    for h in registry['hypotheses']:
        if h.get('status') != 'active' or d.empty:
            continue
        x = d[d.scan_date >= registry['inference_protocol']['cohort_start']]
        if h['type'] == 'post_entry':
            x = _open_at(x, int(h['landmark_day']))
        if h['type'] == 'exit_shadow' or h['type'] == 'portfolio' or x.empty:
            continue
        feats = SPEED_FEATURES if h.get('feature') == '*' else [h.get('feature')]
        for f in feats:
            if f == 'first_green_day':
                continue  # NULL is the observed never-green state, not missing input.
            missing = len(x) if f not in x else int(x[f].isna().sum())
            if missing:
                errors.append(f"{h['id']}: {f} missing on {missing}/{len(x)} expected rows")
    return errors


def score_registry(con, registry, today=None, archives_dir=None):
    global ARCHIVES, _FRAME, _COVERAGE, _INFERENCE_STATUS, _CLUSTER_EVIDENCE
    if archives_dir:
        ARCHIVES = archives_dir
    _COVERAGE = label_coverage(con, today)
    _INFERENCE_STATUS = inference_status(registry, today)
    d = load_frame(con)
    p = registry['inference_protocol']
    if not d.empty:
        d = d[(d.scan_date >= p['cohort_start']) & (d.scan_date <= p['entry_cutoff'])].copy()
    _FRAME = d                                   # reused by loser_ledger (no second skew_slope pass)
    _CLUSTER_EVIDENCE = cluster_evidence(d, registry)
    active = [h for h in registry['hypotheses'] if h.get('status') == 'active']
    # multiplicity: every active idea, and every feature a wildcard speed test touches
    n_tests = sum(len(SPEED_FEATURES) if (h['type'] == 'speed' and h['feature'] == '*') else 1
                  for h in active)
    if n_tests != p['planned_tests']:
        _INFERENCE_STATUS = {'ok': False, 'detail': 'registry test count differs from frozen protocol'}
    n_tests = p['planned_tests']
    p_bar = p['family_alpha'] / n_tests
    n_min = int(registry.get('n_min', 10))
    errors = feature_coverage(d, registry)
    if errors:
        _COVERAGE = {**_COVERAGE, 'ok': False, 'feature_errors': errors,
                     'detail': _COVERAGE['detail'] + '; required feature data missing: ' + '; '.join(errors[:5])}
    if not _COVERAGE.get('ok') or not _INFERENCE_STATUS.get('ok'):
        detail = '; '.join(v['detail'] for v in (_COVERAGE, _INFERENCE_STATUS) if not v.get('ok'))
        return [{'id': h['id'], 'type': h['type'], 'registered': h['registered'], 'p_bar': p_bar,
                 'verdict': 'ERROR', 'detail': detail} for h in active], p_bar, n_tests
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
    for r in out:
        if r.get('verdict') not in ('INSUFFICIENT', 'ERROR'):
            r['nominal_iid_verdict'] = r['verdict']
            r['verdict'] = 'CHECKPOINT REVIEW REQUIRED' if _INFERENCE_STATUS['checkpoint_due'] else 'DESCRIPTIVE'
            r['detail'] += ' — exploratory IID statistic; automatic promotion disabled'
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
            x[f] = _post_feature(x, f)
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
        lines.append(f'  latest: {x.ticker} {x.scan_date}  first green {g}  peak upper bound {x.mfe_pct:+.1f}%  stopped {s}')
    return lines + ['']


# ----------------------------------------------------------------- digest
def digest(results, p_bar, n_tests, con, registry=None):
    tot = (len(_FRAME), int(_FRAME.complete.sum())) if _FRAME is not None and not _FRAME.empty else (0, 0)
    lines = [f'🔬 TIER A SELF-AUDIT — {dt.date.today()}',
             f'{tot[0]} qualified names path-labeled ({tot[1]} resolved). '
             f'{n_tests} frozen exploratory tests → nominal per-test bar p<{p_bar:.4f}.',
             'Hypothetical scanner cohort, before portfolio/veto/noise selection; OPEN outcomes remain unknown.',
             'Weekly IID p-values are descriptive. Automatic READY is disabled.']
    if _COVERAGE is not None:
        lines.append(('✅ ' if _COVERAGE.get('ok') else '⚠️ READY blocked — ') + str(_COVERAGE.get('detail'))
                     + (f" (missing: {', '.join(_COVERAGE['missing'][:5])})" if _COVERAGE.get('missing') else ''))
    lines.append('')
    if _INFERENCE_STATUS:
        lines.append(('✅ ' if _INFERENCE_STATUS.get('ok') else '⚠️ ') + _INFERENCE_STATUS['detail'])
    if _CLUSTER_EVIDENCE:
        e = _CLUSTER_EVIDENCE
        lines.append(f"Cluster sensitivity: {e['n_clusters']} connected components; {e['n_resolved']} resolved / {e['n_censored']} censored paths.")
        if e.get('descriptive_95pct_interval'):
            lo, hi = e['descriptive_95pct_interval']
            lines.append(f"Resolved mean {e['mean_resolved_pnl_pct']:+.3f}%; descriptive cluster 95% interval [{lo:+.3f}, {hi:+.3f}]%. No promotion claim.")
        elif e.get('interval_unavailable'):
            lines.append(e['interval_unavailable'])
    lines.append('')
    order = ['CHECKPOINT REVIEW REQUIRED', 'DESCRIPTIVE', 'INSUFFICIENT', 'ERROR']
    icon = {'CHECKPOINT REVIEW REQUIRED': '🟡', 'DESCRIPTIVE': '🔬', 'INSUFFICIENT': '⚪', 'ERROR': '⚠️'}
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
    lines.append('Research does not change trading rules. A calibrated, separately preregistered future protocol is required for promotion.')
    return '\n'.join(lines)


def save_record(record, registry, directory=HERE):
    """Append-only descriptive history and ONE immutable checkpoint snapshot."""
    root = Path(directory)
    safe = json.dumps(record, sort_keys=True, indent=2, allow_nan=False, default=str) + '\n'
    digest = hashlib.sha256(safe.encode()).hexdigest()
    version = registry['inference_protocol']['version']
    history = root / 'audit_history' / version
    history.mkdir(parents=True, exist_ok=True)
    target = history / f"{record['date']}-{digest[:12]}.json"
    if not target.exists():
        with target.open('x', encoding='utf-8') as f:
            f.write(safe)
    if _INFERENCE_STATUS and _INFERENCE_STATUS.get('checkpoint_due'):
        checkpoint = root / 'inference_checkpoints' / f'{version}.json'
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        # Fixed labels stop observing at the protocol cutoff; preserve first review.
        if not checkpoint.exists():
            rows = [] if _FRAME is None else _FRAME.astype(object).where(pd.notna(_FRAME), None).to_dict('records')
            frozen = {'protocol': registry['inference_protocol'], 'lock': _INFERENCE_STATUS,
                      'results': record['results'], 'cluster_evidence': _CLUSTER_EVIDENCE,
                      'label_coverage': _COVERAGE, 'rows': rows, 'first_review_date': record['date']}
            with checkpoint.open('x', encoding='utf-8') as f:
                json.dump(frozen, f, indent=2, allow_nan=False, default=str)
                f.write('\n')
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--freeze-protocol', action='store_true', help='Explicitly freeze rules/engine BEFORE the new cohort begins')
    a = ap.parse_args()
    registry = json.loads(Path(REGISTRY).read_text(encoding='utf-8'))
    if a.freeze_protocol:
        record = freeze_protocol(registry)
        print('[audit] protocol frozen:', record['fingerprint'])
        return
    con = sqlite3.connect(f'file:{Path(DB).resolve().as_posix()}?mode=ro', uri=True)
    results, p_bar, n_tests = score_registry(con, registry)
    text = digest(results, p_bar, n_tests, con, registry)
    print(text)
    if a.json:
        record = {'date': dt.date.today().isoformat(), 'p_bar': p_bar, 'n_tests': n_tests,
                  'label_coverage': _COVERAGE, 'inference_status': _INFERENCE_STATUS,
                  'cluster_evidence': _CLUSTER_EVIDENCE, 'results': results}
        save_record(record, registry, directory=HERE)
        with open(os.path.join(HERE, 'audit_latest.json'), 'w', encoding='utf-8') as f:
            json.dump(record, f, indent=2, allow_nan=False, default=str)
        print('\n[audit] wrote audit_latest.json')
    send_failed = False
    if a.send:
        try:
            from dotenv import load_dotenv; load_dotenv()
        except Exception:
            pass
        from alert import send_telegram
        send_failed = not send_telegram(text)
        print('[audit] telegram:', 'FAILED' if send_failed else 'sent')
    con.close()
    if send_failed or any(r['verdict'] == 'ERROR' for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
