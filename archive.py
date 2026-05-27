"""Daily archive — write signals/YYYY-MM-DD.json after each run.

This is the SINGLE MOST IMPORTANT file for long-term self-improvement:
every Tier A candidate seen, every UW score, every veto, every pick decision
preserved per day. After 30+ days, enables retrospective backtests:
  - "What if we'd used different filter thresholds?"
  - "What if we'd used score >= 2 instead of >= 1?"
  - "Which UW conditions actually predicted winners?"

Without UW re-pulls, since the raw metric values are saved.
"""
import json
from pathlib import Path
from datetime import datetime

ARCHIVE_DIR = Path(__file__).parent / 'signals'
ARCHIVE_DIR.mkdir(exist_ok=True)


def archive_daily_run(scan_date: str, enriched: list[dict], picked_ticker: str | None,
                       min_score_used: int, parameters_version: str,
                       notes: str = '') -> Path:
    """Write a complete record of today's run to signals/YYYY-MM-DD.json."""
    record = {
        'scan_date': scan_date,
        'run_at': datetime.utcnow().isoformat() + 'Z',
        'parameters_version': parameters_version,
        'min_filter_score_used': min_score_used,
        'picked_ticker': picked_ticker,
        'notes': notes,
        'candidates': [],
    }

    for c in enriched:
        f = c.get('filter', {}) or {}
        v = c.get('vetoes', {}) or {}
        record['candidates'].append({
            'ticker': c['ticker'],
            # Skew Tracker fields (these will let us recompute is_tier_a if filter changes)
            'spot_close': c['spot_close'],
            'spot_return_pct': c['spot_return_pct'],
            'skew_change_5d': c['skew_change_5d'],
            'near_skew': c['near_skew'],
            'near_dte': c['near_dte'],
            'put_wall_strike': c['put_wall_strike'],
            'put_wall_oi_change': c['put_wall_oi_change'],
            'sector': c.get('sector'),
            'industry': c.get('industry'),
            'dte_earnings': c.get('dte_earnings'),
            # UW filter outputs (preserved so backtests don't need re-pulls)
            'filter_score': f.get('score'),
            'filter_raw': f.get('raw', {}),
            'filter_conditions_pass': {k: v['pass'] for k, v in f.get('conditions', {}).items()},
            # Vetoes
            'veto_pass': v.get('pass'),
            'veto_reasons': v.get('reasons', []),
            'veto_details': {
                'earnings_next': v.get('details', {}).get('earnings', {}).get('next_earnings'),
                'liquidity_oi': v.get('details', {}).get('liquidity', {}).get('total_oi'),
            },
        })

    file_path = ARCHIVE_DIR / f'{scan_date}.json'
    file_path.write_text(json.dumps(record, indent=2))
    return file_path


def load_archive(scan_date: str) -> dict | None:
    """Load a specific day's archive."""
    file_path = ARCHIVE_DIR / f'{scan_date}.json'
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text())


def list_archives() -> list[str]:
    """List all archived scan dates."""
    return sorted([p.stem for p in ARCHIVE_DIR.glob('*.json')])


if __name__ == '__main__':
    archives = list_archives()
    print(f'Daily archives: {len(archives)}')
    for d in archives[-10:]:
        a = load_archive(d)
        n = len(a.get('candidates', []))
        pick = a.get('picked_ticker') or '(none)'
        print(f'  {d}: {n} Tier A candidates, picked {pick}')
