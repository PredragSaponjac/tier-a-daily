"""Replay historical signals/*.json archives with any parameters.

After 30+ days of daily archives exist, this lets you ask:
  - "Would a -5% stop have outperformed -7%?"
  - "Would min_score=2 have picked better than min_score=1?"
  - "Which UW conditions actually predicted winners?"

Without re-pulling UW (uses preserved raw scores from archive).
"""
import argparse
import json
import statistics
from pathlib import Path
from collections import Counter
import yfinance as yf
import datetime as dt

import archive


def recompute_score(cand_raw: dict, thresholds: dict) -> int:
    """Given preserved raw metrics + new thresholds, recompute composite score."""
    s = 0
    z = cand_raw.get('z_ncp')
    coi = cand_raw.get('coi_pct')
    poi = cand_raw.get('poi_pct')
    dp = cand_raw.get('dp_blocks_10d')
    if z is not None and z <= thresholds['ncp_z_threshold']:        s += 1
    if coi is not None and coi <= thresholds['coi_pct_threshold']:  s += 1
    if poi is not None and poi <= thresholds['poi_pct_threshold']:  s += 1
    if dp is not None and dp >= thresholds['dp_blocks_threshold']:  s += 1
    return s


def simulate_trade(ticker: str, entry_date: str, entry_price: float,
                   tp1_pct: float, stop_pct: float, max_days=None,
                   entry_policy='legacy_close', asof=None) -> dict:
    """Shared completed-session paper model; an observation limit censors, never sells."""
    from exits import completed_history, walk_bars, split_factor
    from market_time import now_eastern, next_session
    e = dt.date.fromisoformat(entry_date)
    clock, start = now_eastern(asof), next_session(e)
    end = clock.date() + dt.timedelta(days=1)
    df = yf.Ticker(ticker).history(start=start, end=end,
                                  auto_adjust=False, actions=True, interval='1d')
    actions = df
    df = completed_history(df, start, clock)
    if df is None or df.empty:
        return {'outcome': 'NO_DATA', 'return_pct': None, 'complete': False}
    if entry_policy == 'next_regular_open':
        entry_price = float(df.iloc[0]['Open'])
    else:
        entry_price /= split_factor(actions, e)
    if max_days is not None:
        if not isinstance(max_days, int) or max_days < 1:
            raise ValueError('max_days must be a positive observation count')
        df = df.iloc[:max_days]
    result = walk_bars(df, entry_price, tp1_pct, stop_pct)
    result['entry_policy'] = entry_policy
    result['entry'] = entry_price
    result['actual_entry_date'] = df.index[0].date().isoformat() if entry_policy == 'next_regular_open' else e.isoformat()
    result['price_basis'] = 'split_normalized_price_only'
    return result


def replay(min_score: int = 1, thresholds: dict = None, tp1_pct: float = 10.0,
            stop_pct: float = -7.0, since: str = None) -> dict:
    """Replay all archived signal days with the given parameters.

    Returns aggregate stats + per-trade results.
    """
    if thresholds is None:
        thresholds = {
            'ncp_z_threshold': -0.5,
            'coi_pct_threshold': -5.0,
            'poi_pct_threshold': 0.0,
            'dp_blocks_threshold': 30,
        }

    days = archive.list_archives()
    if since:
        days = [d for d in days if d >= since]

    trades = []
    for d in days:
        a = archive.load_archive(d)
        cands = a.get('candidates', [])
        # Filter to passing vetoes
        passing = [c for c in cands if c.get('veto_pass')]
        if not passing:
            continue
        # Recompute score with new thresholds
        for c in passing:
            c['recomputed_score'] = recompute_score(c.get('filter_raw', {}), thresholds)
        # Rank: score desc, tiebreaker z_ncp asc (more negative wins)
        ranked = sorted(passing, key=lambda c: (-c['recomputed_score'],
                                                 c.get('filter_raw', {}).get('z_ncp')
                                                 if c.get('filter_raw', {}).get('z_ncp') is not None else 999))
        # Top survivor with score >= min_score
        top = next((c for c in ranked if c['recomputed_score'] >= min_score), None)
        if top is None:
            continue
        # Simulate
        entry_policy = a.get('entry_policy') or (
            'next_regular_open' if str(a.get('execution_model', '')).startswith('next_regular_open') else 'legacy_close')
        sim = simulate_trade(top['ticker'], d, top['spot_close'], tp1_pct, stop_pct,
                             entry_policy=entry_policy)
        if sim.get('outcome') in ('NO_DATA', 'ERROR'):
            continue
        trades.append({
            'date': d, 'ticker': top['ticker'], 'entry': top['spot_close'],
            'score': top['recomputed_score'], **sim
        })

    if not trades:
        return {'trades': [], 'summary': {'n': 0}}

    resolved = [t for t in trades if t.get('complete')]
    rets = [t['return_pct'] for t in resolved]
    outcomes = Counter(t['outcome'] for t in trades)
    summary = {
        'n': len(trades),
        'sum_trade_returns_pct': round(sum(rets), 2),
        'resolved_n': len(resolved), 'censored_n': len(trades) - len(resolved),
        'avg_return_pct': round(statistics.mean(rets), 2) if rets else None,
        'median_return_pct': round(statistics.median(rets), 2) if rets else None,
        'win_rate_pct': round(100 * sum(1 for r in rets if r > 0) / len(rets), 1) if rets else None,
        'outcomes': dict(outcomes),
        'mae_avg_winners': round(statistics.mean([t['mae_pct'] for t in resolved if t['return_pct'] > 0]), 2) if any(t['return_pct'] > 0 for t in resolved) else None,
        'mfe_avg_winners': round(statistics.mean([t['mfe_pct'] for t in resolved if t['return_pct'] > 0]), 2) if any(t['return_pct'] > 0 for t in resolved) else None,
    }
    return {'model': 'legacy_single_pick_research; not a capital-constrained portfolio',
            'trades': trades, 'summary': summary, 'parameters_tested': {
        'min_score': min_score, 'thresholds': thresholds,
        'tp1_pct': tp1_pct, 'stop_pct': stop_pct,
    }}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--min-score', type=int, default=1)
    p.add_argument('--tp1', type=float, default=10.0)
    p.add_argument('--stop', type=float, default=-7.0)
    p.add_argument('--ncp-z', type=float, default=-0.5)
    p.add_argument('--coi-pct', type=float, default=-5.0)
    p.add_argument('--poi-pct', type=float, default=0.0)
    p.add_argument('--dp-blocks', type=int, default=30)
    p.add_argument('--since', default=None, help='YYYY-MM-DD start date filter')
    args = p.parse_args()
    th = {
        'ncp_z_threshold': args.ncp_z,
        'coi_pct_threshold': args.coi_pct,
        'poi_pct_threshold': args.poi_pct,
        'dp_blocks_threshold': args.dp_blocks,
    }
    result = replay(min_score=args.min_score, thresholds=th,
                     tp1_pct=args.tp1, stop_pct=args.stop, since=args.since)
    print(f"\n=== BACKTEST REPLAY ===")
    print(f"Parameters: min_score={args.min_score}, TP1={args.tp1}%, STOP={args.stop}%")
    print(f"             ncp_z<={args.ncp_z}, coi%<={args.coi_pct}, poi%<={args.poi_pct}, dp>={args.dp_blocks}")
    print(f"\nResults: {result['summary']}")
    if result['trades']:
        print(f"\nPer-trade:")
        for t in result['trades']:
            print(f"  {t['date']} {t['ticker']:6s} score={t['score']} → {t['outcome']:8s} "
                  f"realized={t['return_pct']} (MAE/MFE bounds: {t.get('excursion_bounds')})")
