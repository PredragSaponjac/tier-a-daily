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
                    tp1_pct: float, stop_pct: float, max_days: int = 10) -> dict:
    """Walk daily OHLC; return outcome under TP1-only exit logic."""
    e = dt.datetime.strptime(entry_date, '%Y-%m-%d').date()
    end = e + dt.timedelta(days=max_days + 7)
    try:
        df = yf.Ticker(ticker).history(start=e + dt.timedelta(days=1), end=end, auto_adjust=True)
    except Exception:
        return {'outcome': 'ERROR', 'return_pct': None}
    if df.empty:
        return {'outcome': 'NO_DATA', 'return_pct': None}

    T1 = entry_price * (1 + tp1_pct / 100)
    STOP = entry_price * (1 + stop_pct / 100)
    mae = 0.0; mfe = 0.0
    days_in = 0
    for idx, row in df.iterrows():
        days_in += 1
        high = float(row['High']); low = float(row['Low'])
        hi_pct = (high / entry_price - 1) * 100
        lo_pct = (low / entry_price - 1) * 100
        if hi_pct > mfe: mfe = hi_pct
        if lo_pct < mae: mae = lo_pct
        if high >= T1:
            return {'outcome': 'TP1', 'return_pct': tp1_pct, 'days_in': days_in,
                    'mae_pct': mae, 'mfe_pct': mfe}
        if low <= STOP:
            return {'outcome': 'STOP', 'return_pct': stop_pct, 'days_in': days_in,
                    'mae_pct': mae, 'mfe_pct': mfe}
        if days_in >= max_days:
            close = float(row['Close'])
            return {'outcome': 'TIMEOUT', 'return_pct': (close / entry_price - 1) * 100,
                    'days_in': days_in, 'mae_pct': mae, 'mfe_pct': mfe}
    close = float(df['Close'].iloc[-1])
    return {'outcome': 'WINDOW_END', 'return_pct': (close / entry_price - 1) * 100,
            'days_in': days_in, 'mae_pct': mae, 'mfe_pct': mfe}


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
                                                 c.get('filter_raw', {}).get('z_ncp') or 999))
        # Top survivor with score >= min_score
        top = next((c for c in ranked if c['recomputed_score'] >= min_score), None)
        if top is None:
            continue
        # Simulate
        sim = simulate_trade(top['ticker'], d, top['spot_close'], tp1_pct, stop_pct)
        if sim.get('return_pct') is None:
            continue
        trades.append({
            'date': d, 'ticker': top['ticker'], 'entry': top['spot_close'],
            'score': top['recomputed_score'], **sim
        })

    if not trades:
        return {'trades': [], 'summary': {'n': 0}}

    rets = [t['return_pct'] for t in trades]
    outcomes = Counter(t['outcome'] for t in trades)
    summary = {
        'n': len(trades),
        'total_return_pct': round(sum(rets), 2),
        'avg_return_pct': round(statistics.mean(rets), 2),
        'median_return_pct': round(statistics.median(rets), 2),
        'win_rate_pct': round(100 * sum(1 for r in rets if r > 0) / len(rets), 1),
        'outcomes': dict(outcomes),
        'mae_avg_winners': round(statistics.mean([t['mae_pct'] for t in trades if t['return_pct'] > 0]), 2) if any(t['return_pct'] > 0 for t in trades) else None,
        'mfe_avg_winners': round(statistics.mean([t['mfe_pct'] for t in trades if t['return_pct'] > 0]), 2) if any(t['return_pct'] > 0 for t in trades) else None,
    }
    return {'trades': trades, 'summary': summary, 'parameters_tested': {
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
                  f"{t['return_pct']:+6.1f}% (MAE {t['mae_pct']:+.1f}%, MFE {t['mfe_pct']:+.1f}%)")
