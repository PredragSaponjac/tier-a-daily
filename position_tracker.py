"""Position state + track record log.

Two files:
- open_positions.json : currently-monitored signals
- track_record.csv   : append-only log of closed signals (with MAE/MFE for self-learning)
"""
import csv
import json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent
OPEN_FILE = ROOT / 'open_positions.json'
RECORD_FILE = ROOT / 'track_record.csv'

RECORD_COLUMNS = [
    'ticker', 'entry_date', 'entry_price',
    'exit_date', 'exit_price', 'exit_reason',
    'realized_return_pct',
    'MAE_pct', 'MAE_date', 'MFE_pct', 'MFE_date',
    'time_to_TP1_days', 'time_to_stop_days', 'time_to_MFE_days', 'time_to_MAE_days',
    'filter_score', 'z_ncp', 'coi_pct', 'poi_pct', 'dp_blocks_10d', 'dp_cumul_10d_M',
    'parameters_version',
]


def _load_open() -> dict:
    if not OPEN_FILE.exists():
        return {'positions': []}
    try:
        return json.loads(OPEN_FILE.read_text())
    except Exception:
        return {'positions': []}


def _save_open(state: dict):
    OPEN_FILE.write_text(json.dumps(state, indent=2))


def list_open() -> list[dict]:
    return _load_open().get('positions', [])


def has_position(ticker: str, entry_date: str) -> bool:
    return any(p['ticker'] == ticker and p['entry_date'] == entry_date for p in list_open())


def add_position(candidate: dict, T1: float, T2: float, T3: float, STOP: float, params_version: str):
    """Add a new signal to monitored positions. Idempotent on (ticker, entry_date)."""
    if has_position(candidate['ticker'], candidate['scan_date']):
        return False
    state = _load_open()
    f = candidate.get('filter', {})
    raw = f.get('raw', {})
    state['positions'].append({
        'ticker': candidate['ticker'],
        'entry_date': candidate['scan_date'],
        'entry_price': candidate['spot_close'],
        'T1': T1, 'T2': T2, 'T3': T3, 'STOP': STOP,
        # MAE/MFE tracking — initialized to 0 (entry == entry)
        'MAE_pct': 0.0,
        'MAE_date': None,
        'MFE_pct': 0.0,
        'MFE_date': None,
        # Filter metadata for retrospective analysis
        'filter_score': f.get('score'),
        'z_ncp': raw.get('z_ncp'),
        'coi_pct': raw.get('coi_pct'),
        'poi_pct': raw.get('poi_pct'),
        'dp_blocks_10d': raw.get('dp_blocks_10d'),
        'dp_cumul_10d_M': raw.get('dp_cumul_10d_M'),
        'parameters_version': params_version,
        'added_at': datetime.utcnow().isoformat() + 'Z',
    })
    _save_open(state)
    return True


def update_mae_mfe(ticker: str, entry_date: str, intraday_low: float, intraday_high: float, on_date: str) -> dict | None:
    """Update MAE/MFE for an open position. Returns updated position or None."""
    state = _load_open()
    updated = None
    for p in state['positions']:
        if p['ticker'] == ticker and p['entry_date'] == entry_date:
            entry = p['entry_price']
            low_pct = (intraday_low / entry - 1) * 100
            high_pct = (intraday_high / entry - 1) * 100
            if low_pct < p['MAE_pct']:
                p['MAE_pct'] = low_pct
                p['MAE_date'] = on_date
            if high_pct > p['MFE_pct']:
                p['MFE_pct'] = high_pct
                p['MFE_date'] = on_date
            updated = p
            break
    if updated is not None:
        _save_open(state)
    return updated


def close_position(ticker: str, entry_date: str, exit_price: float, exit_reason: str, exit_date: str) -> dict | None:
    """Remove from open + append to track_record.csv. Returns the closed record or None."""
    state = _load_open()
    closed = None
    remaining = []
    for p in state['positions']:
        if p['ticker'] == ticker and p['entry_date'] == entry_date and closed is None:
            closed = p
        else:
            remaining.append(p)
    if closed is None:
        return None
    state['positions'] = remaining
    _save_open(state)

    # Compute days-to-event
    e = datetime.fromisoformat(entry_date).date()
    x = datetime.fromisoformat(exit_date).date()
    days_to_exit = (x - e).days

    # time_to_TP1 / time_to_stop based on exit_reason
    time_to_TP1 = days_to_exit if exit_reason == 'TP1' else None
    time_to_stop = days_to_exit if exit_reason == 'STOP' else None
    time_to_MFE = None
    if closed.get('MFE_date'):
        time_to_MFE = (datetime.fromisoformat(closed['MFE_date']).date() - e).days
    time_to_MAE = None
    if closed.get('MAE_date'):
        time_to_MAE = (datetime.fromisoformat(closed['MAE_date']).date() - e).days

    realized = (exit_price / closed['entry_price'] - 1) * 100

    row = {
        'ticker': ticker,
        'entry_date': entry_date,
        'entry_price': closed['entry_price'],
        'exit_date': exit_date,
        'exit_price': exit_price,
        'exit_reason': exit_reason,
        'realized_return_pct': round(realized, 3),
        'MAE_pct': round(closed.get('MAE_pct', 0), 3),
        'MAE_date': closed.get('MAE_date'),
        'MFE_pct': round(closed.get('MFE_pct', 0), 3),
        'MFE_date': closed.get('MFE_date'),
        'time_to_TP1_days': time_to_TP1,
        'time_to_stop_days': time_to_stop,
        'time_to_MFE_days': time_to_MFE,
        'time_to_MAE_days': time_to_MAE,
        'filter_score': closed.get('filter_score'),
        'z_ncp': closed.get('z_ncp'),
        'coi_pct': closed.get('coi_pct'),
        'poi_pct': closed.get('poi_pct'),
        'dp_blocks_10d': closed.get('dp_blocks_10d'),
        'dp_cumul_10d_M': closed.get('dp_cumul_10d_M'),
        'parameters_version': closed.get('parameters_version'),
    }

    file_exists = RECORD_FILE.exists()
    with RECORD_FILE.open('a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=RECORD_COLUMNS)
        if not file_exists:
            w.writeheader()
        w.writerow(row)

    return row


if __name__ == '__main__':
    print('Open positions:')
    for p in list_open():
        print(f"  {p['ticker']} entry {p['entry_date']} @ ${p['entry_price']:.2f}  "
              f"MAE {p['MAE_pct']:+.1f}%  MFE {p['MFE_pct']:+.1f}%")
    if RECORD_FILE.exists():
        with RECORD_FILE.open() as f:
            rows = list(csv.DictReader(f))
        print(f'\nClosed signals in track_record.csv: {len(rows)}')
        for r in rows[-5:]:
            print(f"  {r['ticker']} {r['entry_date']} → {r['exit_date']} "
                  f"({r['exit_reason']}) {r['realized_return_pct']}%")
    else:
        print('\nNo track_record.csv yet (no closed signals).')
