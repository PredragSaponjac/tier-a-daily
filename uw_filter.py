"""UW composite filter — score Tier A candidates by the research-derived pattern.

Per research_uw_picker_v1.md, score = 0-4 across:
  1) net_call_premium entry-day z-score <= -0.5 (vs prior 10d baseline)
  2) call_OI 10-day % change <= -5%
  3) put_OI 10-day % change <= 0%
  4) [bonus] dark pool 10d large blocks >= 30 (only available for entry >= 30d API cap from today)

Returns enriched candidate dict with score + breakdown.
"""
import statistics
from uw_client import cached_get
import parameters as P


def _f(x):
    try: return float(x)
    except: return None


def _zscore(value, baseline):
    b = [x for x in baseline if x is not None]
    if len(b) < 3 or value is None:
        return None
    m = statistics.mean(b)
    s = statistics.stdev(b)
    if s == 0: return None
    return (value - m) / s


def _pre_window(rows_sorted, entry_date, n=15):
    """Return up to n+1 rows ending with entry_date (sorted ascending by date)."""
    idx = next((i for i, r in enumerate(rows_sorted) if r.get('date') == entry_date), None)
    if idx is None: return None
    start = max(0, idx - n)
    return rows_sorted[start:idx + 1]


def compute_score(ticker: str, entry_date: str, pull_dp: bool = True,
                  vol_days: int = None, dp_days: int = None) -> dict:
    """Compute composite filter score for one candidate.

    Args:
      vol_days: options-volume rows to pull (default from parameters.json, ~20).
                For backtests on older dates, may need 40-80.
      dp_days: dark pool trading days before entry to pull (default ~10).

    Returns dict with:
      score: 0-4
      conditions: dict of each condition + pass/fail + value
      raw: raw metric values
    """
    if vol_days is None:
        vol_days = P.options_volume_days()
    if dp_days is None:
        dp_days = P.darkpool_days()

    # 1) Pull options-volume history (rolling window — cache invalidates daily
    #    so each daily run gets today's row)
    vol_data = cached_get(f'/api/stock/{ticker}/options-volume',
                          params={'limit': str(vol_days)},
                          cache_date=entry_date)  # cache key includes entry date
    if not vol_data or 'data' not in vol_data:
        return {'score': None, 'conditions': {}, 'raw': {}, 'error': 'no options-volume data'}

    rows_sorted = sorted(vol_data['data'], key=lambda r: r.get('date', ''))
    window = _pre_window(rows_sorted, entry_date, n=15)
    if not window or len(window) < 6:
        return {'score': None, 'conditions': {}, 'raw': {}, 'error': 'insufficient pre-window'}

    entry = window[-1]
    prior = window[:-1]
    # Last 10 prior rows for z-score baseline
    prior10 = prior[-10:] if len(prior) >= 10 else prior

    ncp_entry = _f(entry.get('net_call_premium'))
    ncp_prior = [_f(r.get('net_call_premium')) for r in prior10]
    z_ncp = _zscore(ncp_entry, ncp_prior)

    coi_entry = _f(entry.get('call_open_interest'))
    poi_entry = _f(entry.get('put_open_interest'))
    coi_10ago = next((_f(r.get('call_open_interest')) for r in prior10 if _f(r.get('call_open_interest')) is not None), None)
    poi_10ago = next((_f(r.get('put_open_interest')) for r in prior10 if _f(r.get('put_open_interest')) is not None), None)

    coi_pct = ((coi_entry / coi_10ago - 1) * 100) if (coi_entry and coi_10ago and coi_10ago > 0) else None
    poi_pct = ((poi_entry / poi_10ago - 1) * 100) if (poi_entry and poi_10ago and poi_10ago > 0) else None

    # Dark pool (optional bonus)
    dp_blocks_10d = None
    dp_cumul_10d_M = None
    if pull_dp:
        # Pull dark pool for each of the N trading days prior to entry
        window_dates = [r['date'] for r in window]
        # Last (dp_days+1) entries: dp_days prior + entry itself; take prior only
        prior_dates = window_dates[-(dp_days + 1):-1]
        block_count = 0
        dollar_sum = 0
        any_data = False
        for d in prior_dates:
            dp = cached_get(f'/api/darkpool/{ticker}', params={'date': d, 'limit': '500'})
            if dp and 'data' in dp:
                any_data = True
                for p in dp['data']:
                    premium = _f(p.get('premium')) or 0
                    dollar_sum += premium
                    if premium >= 500_000:
                        block_count += 1
        if any_data:
            dp_blocks_10d = block_count
            dp_cumul_10d_M = dollar_sum / 1e6

    # Score conditions (thresholds from parameters.json — adaptive)
    th = P.filter_thresholds()
    conditions = {
        'ncp_entry_z': {
            'value': z_ncp,
            'pass': (z_ncp is not None and z_ncp <= th['ncp_z_threshold']),
            'desc': f"Entry-day net call premium z-score <= {th['ncp_z_threshold']} (capitulation flush)"
        },
        'call_oi_10d_pct': {
            'value': coi_pct,
            'pass': (coi_pct is not None and coi_pct <= th['coi_pct_threshold']),
            'desc': f"Call OI 10-day % change <= {th['coi_pct_threshold']}% (call structure deleveraging)"
        },
        'put_oi_10d_pct': {
            'value': poi_pct,
            'pass': (poi_pct is not None and poi_pct <= th['poi_pct_threshold']),
            'desc': f"Put OI 10-day % change <= {th['poi_pct_threshold']}% (put structure not building)"
        },
        'dp_large_blocks_10d': {
            'value': dp_blocks_10d,
            'pass': (dp_blocks_10d is not None and dp_blocks_10d >= th['dp_blocks_threshold']),
            'desc': f"Dark pool large blocks (10d) >= {th['dp_blocks_threshold']}"
        },
    }
    score = sum(1 for c in conditions.values() if c['pass'])

    return {
        'score': score,
        'conditions': conditions,
        'raw': {
            'z_ncp': z_ncp,
            'coi_pct': coi_pct,
            'poi_pct': poi_pct,
            'dp_blocks_10d': dp_blocks_10d,
            'dp_cumul_10d_M': dp_cumul_10d_M,
        },
    }


def enrich_candidates(candidates: list[dict], pull_dp: bool = True,
                       vol_days: int = None, dp_days: int = None) -> list[dict]:
    """Add 'filter' key with composite score to each candidate. Returns enriched list sorted by score desc."""
    for c in candidates:
        c['filter'] = compute_score(c['ticker'], c['scan_date'], pull_dp=pull_dp,
                                    vol_days=vol_days, dp_days=dp_days)
    # Sort by score desc; tiebreaker by z_ncp (more negative = stronger flush)
    def sort_key(c):
        s = c['filter'].get('score', -1) if c['filter'].get('score') is not None else -1
        z = c['filter']['raw'].get('z_ncp') if c['filter'].get('raw') else None
        z_val = z if z is not None else 999
        return (-s, z_val)
    return sorted(candidates, key=sort_key)


if __name__ == '__main__':
    import argparse
    from scanner_reader import read_tier_a
    p = argparse.ArgumentParser()
    p.add_argument('--scan-date', default=None)
    p.add_argument('--no-dp', action='store_true', help='skip dark pool (faster)')
    args = p.parse_args()
    cands, sd = read_tier_a(args.scan_date)
    print(f'Scoring {len(cands)} Tier A candidates for {sd}...')
    enriched = enrich_candidates(cands, pull_dp=not args.no_dp)
    print(f'\n{"rank":>4s} {"tkr":6s} {"score":>5s} {"z_ncp":>7s} {"coi%":>7s} {"poi%":>7s} {"dp_blks":>8s} {"dp_$M":>8s}  sector')
    for i, c in enumerate(enriched, 1):
        f = c['filter']
        r = f['raw']
        def fmt(x, d=2): return f'{x:.{d}f}' if x is not None else '—'
        print(f"  {i:>3d} {c['ticker']:6s} {str(f.get('score','—')):>5s} {fmt(r.get('z_ncp')):>7s} "
              f"{fmt(r.get('coi_pct'),1):>7s} {fmt(r.get('poi_pct'),1):>7s} "
              f"{fmt(r.get('dp_blocks_10d'),0):>8s} {fmt(r.get('dp_cumul_10d_M'),1):>8s}  {c.get('sector','')}")
