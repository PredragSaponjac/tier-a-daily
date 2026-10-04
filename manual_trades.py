"""Manual Skew-Tracker trade log (open + closed) — feeds the 'Manual Skew Trades'
Google Sheet tab and the heat&peak excursion block.

Two stores (JSON, in tier-a-daily/ where the post code + creds already live):
  open_trades.json    — currently-live manual calls (status OPEN)
  closed_trades.json  — finished trades (also powers excursions.format_excursion_block)

MANUAL = pure skew, NO UW. This is logging/track-record only — it does NOT touch
signal generation, so it does not violate the manual/GitHub UW separation.

CLI:
  # call a trade (logs it OPEN, appears in the sheet immediately):
  python manual_trades.py --open OSCR 2026-06-02 20.85 --t1 22.94 --t2 23.14 --t3 25.02 --stop 19.39 --note "skew capitulation, +34% put-wall cushion"
  # close a trade (computes heat/peak while data is fresh, moves OPEN->closed):
  python manual_trades.py --close OSCR 2026-06-04 23.14 WIN --reason TP2
  python manual_trades.py --list
"""
import json
import os
from state_lock import portfolio_lock, checkpoint

HERE = os.path.dirname(__file__)
OPEN_FILE = os.path.join(HERE, 'open_trades.json')
CLOSED_FILE = os.path.join(HERE, 'closed_trades.json')   # shared with excursions.py


def _load(path):
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _save(path, data):
    from pathlib import Path
    from position_tracker import _atomic_write
    _atomic_write(Path(path), json.dumps(data, indent=2))


def get_open():
    return _load(OPEN_FILE)


def get_closed():
    return _load(CLOSED_FILE)


def _open_trade(ticker, entry_date, entry_price, t1=None, t2=None, t3=None,
               stop=None, setup='', note=''):
    opens = get_open()
    if any(t['ticker'] == ticker and t.get('status') == 'OPEN' for t in opens):
        print(f'[manual] {ticker} already OPEN — skip')
        return
    opens.append({
        'ticker': ticker, 'status': 'OPEN', 'entry_date': entry_date,
        'entry_price': float(entry_price),
        'T1': t1 and float(t1), 'T2': t2 and float(t2),
        'T3': t3 and float(t3), 'stop': stop and float(stop),
        'setup': setup, 'note': note,
    })
    opens.sort(key=lambda t: t['entry_date'])
    _save(OPEN_FILE, opens)
    print(f'[manual] OPENED {ticker} @ ${float(entry_price):.2f} ({entry_date})')


def _close_trade(ticker, exit_date, exit_price, outcome, reason=''):
    """Persist the canonical manual close before removing it from the open book."""
    import excursions
    opens = get_open()
    match = next((t for t in opens if t['ticker'] == ticker and t.get('status') == 'OPEN'), None)
    if match is None:
        print(f'[manual] no OPEN {ticker} found — pass entry via --open first')
        return
    closed = get_closed()
    existing = next((t for t in closed if t['ticker'] == ticker and t['entry_date'] == match['entry_date']), None)
    if existing is not None:
        _save(OPEN_FILE, [t for t in opens if t is not match])
        print(f'[manual] {ticker} already closed; finished removing its open entry')
        return existing
    exc = excursions.compute_excursion(ticker, match['entry_date'], match['entry_price'], exit_date, float(exit_price))
    if exc is None:
        print(f'[manual] WARNING: held-period daily bounds unavailable for {ticker}')
        exc = {'heat_pct': None, 'peak_pct': None, 'peak_day': None, 'first_green_day': None, 'src': 'none'}
    normalized_entry = exc.get('normalized_entry_price', match['entry_price'])
    normalized_exit = exc.get('normalized_exit_price', float(exit_price))
    result_pct = round((normalized_exit / normalized_entry - 1) * 100, 1)
    factor = exc.get('quantity_factor', 1.0)
    rec = {
        'ticker': ticker, 'entry_date': match['entry_date'],
        'entry_price': normalized_entry, 'original_entry_price': match['entry_price'],
        'outcome': outcome.upper(),
        'exit_date': exit_date, 'exit_price': normalized_exit,
        'result_pct': result_pct, 'exit_reason': reason,
        'T1': match.get('T1') / factor if match.get('T1') else None,
        'T2': match.get('T2') / factor if match.get('T2') else None,
        'T3': match.get('T3') / factor if match.get('T3') else None,
        'stop': match.get('stop') / factor if match.get('stop') else None,
        'setup': match.get('setup', ''),
        **exc, 'computed_on': exit_date, 'note': match.get('note', ''),
    }
    closed.append(rec)
    closed.sort(key=lambda t: t['entry_date'])
    _save(CLOSED_FILE, closed)
    checkpoint(paths=('closed_trades.json',))
    opens = [t for t in opens if not (t['ticker'] == ticker and t.get('status') == 'OPEN')]
    _save(OPEN_FILE, opens)
    print(f'[manual] CLOSED {ticker} @ ${float(exit_price):.2f} ({reason}) = {result_pct:+.1f}% | '
          f'heat {exc["heat_pct"]}% peak {exc["peak_pct"]}%')
    return rec


def open_trade(*args, **kwargs):
    with portfolio_lock():
        result = _open_trade(*args, **kwargs)
        checkpoint(paths=('open_trades.json',))
        return result


def close_trade(*args, **kwargs):
    with portfolio_lock():
        result = _close_trade(*args, **kwargs)
        checkpoint(paths=('closed_trades.json', 'open_trades.json'))
        return result


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--open', nargs=3, metavar=('TICKER', 'DATE', 'ENTRY'))
    p.add_argument('--t1'); p.add_argument('--t2'); p.add_argument('--t3'); p.add_argument('--stop')
    p.add_argument('--close', nargs=4, metavar=('TICKER', 'DATE', 'EXIT', 'OUTCOME'))
    p.add_argument('--reason', default='')
    p.add_argument('--note', default='')
    p.add_argument('--list', action='store_true')
    a = p.parse_args()
    if a.open:
        open_trade(a.open[0], a.open[1], a.open[2], a.t1, a.t2, a.t3, a.stop, a.note)
    if a.close:
        close_trade(a.close[0], a.close[1], a.close[2], a.close[3], a.reason)
    if a.list or not (a.open or a.close):
        print('OPEN:'); [print('  ', t) for t in get_open()]
        print(f'CLOSED: {len(get_closed())} trades')
