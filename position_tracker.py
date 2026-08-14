"""Position state + track record log.

Two files:
- open_positions.json : currently-monitored signals
- track_record.csv   : append-only log of closed signals (with MAE/MFE for self-learning)
"""
import csv
import json
from pathlib import Path
from datetime import datetime, timedelta

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

    _append_closed_trade(closed, row, days_to_exit)
    return row


def _append_closed_trade(closed: dict, row: dict, days_to_exit: int) -> None:
    """ALSO write the durable record to closed_trades.json.

    ROOT-CAUSE FIX (2026-08-14, earned twice: ADSK 7/16 and RDDT 8/14).
    close_position only appended to track_record.csv — which is GITIGNORED — so on the
    GitHub runner the closed record was written and immediately thrown away. Meanwhile
    the Google Sheet Track Record tab, the X/Telegram record line (excursions.py) and
    exit_model.py ALL read closed_trades.json, and nothing wrote it automatically.
    Net effect: a trade closed correctly, posted correctly, then vanished from the
    record and had to be re-entered by hand. Twice. This closes the loop.

    Idempotent: an already-recorded (ticker, entry_date) is a no-op, so a re-run or a
    duplicate monitor pass cannot double-count a trade.
    """
    path = ROOT / 'closed_trades.json'
    try:
        trades = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    except Exception as e:
        print(f'  [record] could not read closed_trades.json ({e}) — NOT overwriting')
        return
    if any(t.get('ticker') == row['ticker'] and t.get('entry_date') == row['entry_date']
           for t in trades):
        return

    reason = row['exit_reason']
    realized = row['realized_return_pct']

    # Fill the day-columns from real price action, else the Sheet renders a WIN as
    # "no" in the conservative column (the NNE 2026-06-15 bug: a +10% winner showed
    # "no (peak +14.4%)"). Best-effort — a data hiccup must never block the record.
    conserv_day = first_green = also = None
    try:
        import yfinance as yf
        e_px = row['entry_price']
        e_dt = datetime.fromisoformat(row['entry_date']).date()
        df = yf.Ticker(row['ticker']).history(
            start=(e_dt + timedelta(days=1)).isoformat(),
            end=(datetime.fromisoformat(row['exit_date']).date() + timedelta(days=1)).isoformat(),
            interval='1d', auto_adjust=True)
        for i, (ts, r) in enumerate(df.iterrows(), start=1):
            d = (ts.date() - e_dt).days
            if conserv_day is None and float(r['High']) >= e_px * 1.075:
                conserv_day = d
            if first_green is None and float(r['Close']) > e_px:
                first_green = d
        peak = float(df['High'].max()) if len(df) else None
        if peak and closed.get('T3') and peak >= closed['T3']:
            also = f"TP3 +{(closed['T3']/e_px-1)*100:.0f}%"
        elif peak and closed.get('T2') and peak >= closed['T2']:
            also = f"TP2 +{(closed['T2']/e_px-1)*100:.0f}%"
    except Exception as e:
        print(f'  [record] day-columns not computed ({e}) — record still written')

    trades.append({
        'ticker': row['ticker'],
        'entry_date': row['entry_date'],
        'entry_price': row['entry_price'],
        'outcome': 'WIN' if realized > 0 else 'LOSS',
        'exit_date': row['exit_date'],
        'exit_price': row['exit_price'],
        'result_pct': round(realized, 1),
        'exit_reason': reason,
        'T1': closed.get('T1'), 'T2': closed.get('T2'), 'T3': closed.get('T3'),
        'stop': closed.get('STOP'),
        'heat_pct': row['MAE_pct'],
        'peak_pct': row['MFE_pct'],
        'peak_day': row.get('time_to_MFE_days'),
        'days_to_mfe': row.get('time_to_MFE_days'),
        'first_green_day': first_green,
        'setup': '',
        'src': 'monitor',
        'computed_on': datetime.utcnow().date().isoformat(),
        'note': 'auto-recorded by monitor close',
        'conserv_day': conserv_day,
        'tp1_day': days_to_exit if reason == 'TP1' else None,
        'tp2_day': None,
        'tp3_day': None,
        'stop_day': days_to_exit if reason == 'STOP' else None,
        'also_reached': also or '—',
        'uw_score': closed.get('filter_score') or 0,
    })
    path.write_text(json.dumps(trades, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"  [record] {row['ticker']} appended to closed_trades.json "
          f"({len(trades)} total)")


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
