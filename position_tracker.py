"""Position state + track record log.

Two files:
- open_positions.json : currently-monitored signals
- track_record.csv   : append-only log of closed signals (with MAE/MFE for self-learning)
"""
import csv
import json
import math
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


class PortfolioStateError(RuntimeError):
    """The book could not be read or written safely. Never swallow this."""


def _load_open() -> dict:
    """ABSENT file -> empty book. PRESENT but unreadable/invalid -> RAISE.

    AUDIT F08 (2026-10-04): any read or JSON error used to return an EMPTY book. A
    corrupted file therefore looked like "no positions": the monitor reported nothing
    to watch and succeeded, and the next add_position wrote a one-entry book over the
    corrupt file — silently erasing every open trade. Unreadable is not empty.
    """
    if not OPEN_FILE.exists():
        return {'positions': []}
    try:
        state = json.loads(OPEN_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        raise PortfolioStateError(f'{OPEN_FILE.name} exists but cannot be read ({e}); '
                                  f'refusing to treat it as an empty book') from e
    if not isinstance(state, dict) or not isinstance(state.get('positions'), list):
        raise PortfolioStateError(f'{OPEN_FILE.name} has an invalid schema; refusing to use it')
    for p in state['positions']:
        if not isinstance(p, dict) or not p.get('ticker') or not p.get('entry_date'):
            raise PortfolioStateError('invalid position identity')
        for field in ('entry_price', 'T1', 'T2', 'T3', 'STOP'):
            value = p.get(field)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise PortfolioStateError(f'invalid position {field}')
    return state


def _atomic_write(path: Path, text: str):
    """Write to a sibling temp file then os.replace(). A reader sees either the old
    complete file or the new complete file, never a truncated one (AUDIT F08: the old
    write_text truncated first, opening a window where a crash left a half file)."""
    import os
    import tempfile
    path = Path(path)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as f:
            tmp = Path(f.name)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def _save_open(state: dict):
    _atomic_write(OPEN_FILE, json.dumps(state, indent=2))


def list_open() -> list[dict]:
    return _load_open().get('positions', [])


def has_position(ticker: str, entry_date: str) -> bool:
    return any(_matches(p, ticker, entry_date) for p in list_open())


def _matches(record, ticker, signal_date):
    return record.get('ticker') == ticker and record.get('signal_date', record.get('entry_date')) == signal_date


def add_position(candidate: dict, T1: float, T2: float, T3: float, STOP: float, params_version: str,
                 entry_policy: str = 'legacy_close'):
    """Add a new signal; immutable identity is (ticker, signal_date), even after entry."""
    reference = candidate['spot_close']
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
           for v in (reference, T1, T2, T3, STOP)):
        raise PortfolioStateError('invalid candidate prices')
    if not STOP < reference < T1 <= T2 <= T3:
        raise PortfolioStateError('invalid candidate exit levels')
    datetime.fromisoformat(candidate['scan_date'])
    if has_position(candidate['ticker'], candidate['scan_date']) or closed_record(candidate['ticker'], candidate['scan_date']):
        return False
    state = _load_open()
    f = candidate.get('filter', {})
    raw = f.get('raw', {})
    state['positions'].append({
        'ticker': candidate['ticker'],
        'trade_id': f"{candidate['ticker']}:{candidate['scan_date']}",
        'signal_date': candidate['scan_date'],
        'entry_policy': entry_policy,
        'status': 'PENDING_ENTRY' if entry_policy == 'next_regular_open' else 'OPEN',
        'entry_reference_price': candidate['spot_close'],
        'exit_pcts': {key: (value / candidate['spot_close'] - 1) * 100
                      for key, value in {'tp1': T1, 'tp2': T2, 'tp3': T3, 'stop': STOP}.items()},
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


def update_position(ticker, signal_date, **updates):
    state = _load_open()
    for p in state['positions']:
        if _matches(p, ticker, signal_date):
            p.update(updates)
            _save_open(state)
            return p
    raise PortfolioStateError(f'no open position for {ticker}:{signal_date}')


def activate_position(ticker, signal_date, session_date, opening):
    if not math.isfinite(opening) or opening <= 0:
        raise PortfolioStateError('invalid entry opening price')
    p = next(p for p in list_open() if _matches(p, ticker, signal_date))
    if p.get('status') != 'PENDING_ENTRY':
        return p
    levels = {field: opening * (1 + p['exit_pcts'][key] / 100)
              for field, key in {'T1': 'tp1', 'T2': 'tp2', 'T3': 'tp3', 'STOP': 'stop'}.items()}
    return update_position(ticker, signal_date, status='OPEN', entry_date=session_date,
                           entry_price=opening, original_entry_price=opening,
                           original_levels=levels.copy(), **levels)


def update_excursions(ticker, signal_date, result):
    """Replace a recomputed completed-session path, preserving uncertainty explicitly."""
    return update_position(ticker, signal_date, MAE_pct=result['mae_pct'], MFE_pct=result['mfe_pct'],
                           MAE_date=result['mae_date'], MFE_date=result['mfe_date'],
                           excursion_bounds=result['excursion_bounds'],
                           execution_method=result['execution_method'],
                           observations_asof=datetime.utcnow().isoformat() + 'Z')


def update_mae_mfe(ticker: str, entry_date: str, intraday_low: float, intraday_high: float, on_date: str) -> dict | None:
    """Update MAE/MFE for an open position. Returns updated position or None."""
    state = _load_open()
    updated = None
    for p in state['positions']:
        if p['ticker'] == ticker and p['entry_date'] == entry_date:
            entry = p['entry_price']
            low_pct = (intraday_low / entry - 1) * 100
            high_pct = (intraday_high / entry - 1) * 100
            # *_ts = when the monitor first OBSERVED the new extreme (re-audit R1: a timestamped
            # live observation that a final daily bar cannot reconstruct afterwards)
            now = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
            if low_pct < p['MAE_pct']:
                p['MAE_pct'] = low_pct
                p['MAE_date'] = on_date
                p['MAE_ts'] = now
            if high_pct > p['MFE_pct']:
                p['MFE_pct'] = high_pct
                p['MFE_date'] = on_date
                p['MFE_ts'] = now
            updated = p
            break
    if updated is not None:
        _save_open(state)
    return updated


def close_position(ticker: str, entry_date: str, exit_price: float, exit_reason: str, exit_date: str,
                   exit_note: str = '') -> dict | None:
    """Close a position. ORDER MATTERS — durable record first, removal last.

    AUDIT F07 (2026-10-04): this removed the position from the open book FIRST, then
    wrote the CSV, then the durable closed_trades record. Any failure or interruption in
    between left the trade in NEITHER place — gone from open, never recorded as closed.
    Now: (1) write the durable closed record (idempotent on ticker+entry_date, raises on
    failure), (2) only then remove it from the open book, (3) the CSV is a derived export
    and best-effort. A crash between (1) and (2) is recoverable: the re-run finds the
    position still open, the record write is a no-op, and the removal completes.
    Returns the CANONICAL closed record from closed_trades.json (an existing one wins over
    this call's values), or None if no such open position.
    """
    state = _load_open()
    closed = next((p for p in state['positions']
                   if _matches(p, ticker, entry_date)), None)
    if closed is None:
        return closed_record(ticker, entry_date)
    existing = closed_record(ticker, entry_date)
    if existing is not None:
        # A crash after durable append cannot make the next invocation's proposed
        # exit authoritative, even if its price/date is malformed or unavailable.
        state['positions'] = [p for p in state['positions'] if not _matches(p, ticker, entry_date)]
        _save_open(state)
        return existing
    if not math.isfinite(exit_price) or exit_price <= 0:
        raise PortfolioStateError('invalid exit price')

    # The signal date is the immutable identity; activation may change the fill date.
    entry_date = closed['entry_date']

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
    if not (closed.get('excursion_bounds') or {}).get('exact', False):
        # Outer-bound bar dates need not be the actual held extrema dates.
        time_to_MFE = time_to_MAE = None

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

    # (1) DURABLE record first. Raises PortfolioStateError on any failure, so the
    #     position stays open and the monitor run fails visibly instead of losing it.
    #     RE-AUDIT R4: if a record already exists (an earlier run recorded the close, then
    #     failed), THAT record is the close. It is returned unchanged, never recomputed: a
    #     later bar can resolve differently, and announcing a second result would contradict
    #     the ledger.
    canon, created = _append_closed_trade(closed, row, days_to_exit, exit_note)

    # (2) Remove from the open book ONLY now that the outcome is durably recorded.
    state['positions'] = [p for p in state['positions']
                          if not _matches(p, ticker, closed.get('signal_date', entry_date))]
    _save_open(state)

    # (3) track_record.csv is a gitignored research export, never the record of truth.
    if created:
        try:
            file_exists = RECORD_FILE.exists()
            with RECORD_FILE.open('a', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=RECORD_COLUMNS)
                if not file_exists:
                    w.writeheader()
                w.writerow(row)
        except Exception as e:
            print(f'  [record] track_record.csv export skipped ({e}); closed_trades.json holds the record')
    return canon


CLOSED_FILE_NAME = 'closed_trades.json'


def _read_closed() -> list:
    """closed_trades.json as a list. Absent = []; unreadable or not a list RAISES (F07/F08)."""
    path = ROOT / CLOSED_FILE_NAME
    try:
        trades = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    except Exception as e:
        raise PortfolioStateError(f'{CLOSED_FILE_NAME} unreadable ({e})') from e
    if not isinstance(trades, list):
        raise PortfolioStateError(f'{CLOSED_FILE_NAME} is not a list')
    if any(not isinstance(t, dict) or not t.get('ticker') or not t.get('entry_date') for t in trades):
        raise PortfolioStateError(f'{CLOSED_FILE_NAME} contains an invalid trade')
    return trades


def closed_record(ticker: str, entry_date: str) -> dict | None:
    """The durable close for this trade, if one was recorded."""
    return next((t for t in _read_closed()
                 if _matches(t, ticker, entry_date)), None)


# THE CLOSE OUTBOX (re-audit R4, 2026-10-04). A close is recorded with
# publication = {'telegram': 'pending', 'x': 'pending'}; monitor.publish_pending() announces
# it FROM THIS RECORD and stores each channel's result. Retryable states are listed here.
RETRYABLE = {'telegram': ('pending', 'failed'), 'x': ('pending', 'rejected')}


def pending_publications() -> list:
    """Closed records whose announcement has not completed on some channel."""
    return [t for t in _read_closed() if isinstance(t.get('publication'), dict)
            and any(t['publication'].get(ch) in (*st, 'inflight', 'unknown') for ch, st in RETRYABLE.items())]


def unresolved_publications():
    return [t for t in _read_closed() if isinstance(t.get('publication'), dict)
            and (t['publication'].get('telegram') != 'sent' or t['publication'].get('x') not in ('posted', 'draft'))]


def set_publication(ticker: str, entry_date: str, **fields) -> dict:
    """Atomically record a channel result on one closed record. Returns the record."""
    trades = _read_closed()
    for t in trades:
        if _matches(t, ticker, entry_date):
            t.setdefault('publication', {}).update(fields)
            _atomic_write(ROOT / CLOSED_FILE_NAME, json.dumps(trades, indent=2, ensure_ascii=False))
            return t
    raise PortfolioStateError(f'no closed record for {ticker} {entry_date}')


def begin_publication(ticker, signal_date, channel, attempt):
    """Persist an uncertain/inflight attempt before transport; never blindly replay it."""
    counter = 'tg_attempts' if channel == 'telegram' else 'x_attempts'
    return set_publication(ticker, signal_date, **{channel: 'inflight', counter: attempt,
                           f'{channel}_attempted_at': datetime.utcnow().isoformat() + 'Z'})


def _setup_note(ticker: str, entry_date: str, closed: dict) -> str:
    """Reconstruct the 'why we entered' note from the archived signal.

    The Sheet column is `notes (why we entered)`. Every hand-entered trade has one;
    auto-recorded closes were leaving it blank (RDDT 2026-08-14). Built from the
    archive so it states the MEASUREMENTS that qualified the trade — never adjectives.
    """
    try:
        arc = ROOT / 'signals' / f'{entry_date}.json'
        if not arc.exists():
            return ''
        cands = json.loads(arc.read_text(encoding='utf-8')).get('candidates', [])
        c = next((x for x in cands if x.get('ticker') == ticker), None)
        if not c:
            return ''
        spot, pw = c.get('spot_close'), c.get('put_wall_strike')
        cushion = ((spot / pw - 1) * 100) if (spot and pw) else None
        bits = []
        if c.get('spot_return_pct') is not None:
            bits.append(f"washout {c['spot_return_pct']:+.1f}%/5d")
        if c.get('skew_change_5d') is not None:
            bits.append(f"skewΔ5d {c['skew_change_5d']:+.1f}")
        if c.get('near_skew') is not None:
            bits.append(f"near-skew {c['near_skew']:+.1f}")
        if cushion is not None:
            bits.append(f"{cushion:+.1f}% above put wall ${pw:g}")
        if c.get('put_wall_oi_change') is not None:
            bits.append(f"wall OI {c['put_wall_oi_change']:+,}")
        note = 'Tier A: ' + ', '.join(bits) + '.'
        sc = c.get('filter_score')
        note += f" UW {sc}/4." if sc is not None else " UW not measured."
        e = c.get('edge') or {}
        if e.get('sector_iv_rank') is not None:
            note += (f" [validation-only: sector_iv_rank {e['sector_iv_rank']}, "
                     f"combo {'pass' if e.get('combo_pass') else 'no'}, "
                     f"stop {e.get('stop_atr')}x ATR]")
        return note
    except Exception:
        return ''


def _append_closed_trade(closed: dict, row: dict, days_to_exit: int, exit_note: str = '') -> tuple:
    """ALSO write the durable record to closed_trades.json.

    ROOT-CAUSE FIX (2026-08-14, earned twice: ADSK 7/16 and RDDT 8/14).
    close_position only appended to track_record.csv — which is GITIGNORED — so on the
    GitHub runner the closed record was written and immediately thrown away. Meanwhile
    the Google Sheet Track Record tab, the X/Telegram record line (excursions.py) and
    exit_model.py ALL read closed_trades.json, and nothing wrote it automatically.
    Net effect: a trade closed correctly, posted correctly, then vanished from the
    record and had to be re-entered by hand. Twice. This closes the loop.

    Idempotent: an already-recorded (ticker, entry_date) is returned UNCHANGED, so a re-run
    or a duplicate monitor pass cannot double-count a trade or rewrite its result.
    Returns (record, created).
    """
    path = ROOT / CLOSED_FILE_NAME
    # AUDIT F07: an unreadable file used to print and RETURN, so close_position "succeeded"
    # with no durable record. _read_closed raises instead — the caller keeps the position open.
    trades = _read_closed()
    existing = next((t for t in trades if _matches(t, row['ticker'], closed.get('signal_date', row['entry_date']))), None)
    if existing is not None:
        return existing, False

    reason = row['exit_reason']
    realized = row['realized_return_pct']

    # No network re-pull during persistence: post-exit closes/highs cannot establish
    # held-period day columns. Unavailable facts stay null.
    conserv_day = first_green = also = None

    rec = {
        'ticker': row['ticker'],
        'trade_id': closed.get('trade_id', f"{row['ticker']}:{row['entry_date']}"),
        'signal_date': closed.get('signal_date', row['entry_date']),
        'entry_policy': closed.get('entry_policy', 'legacy_close'),
        'execution_method': closed.get('execution_method', 'legacy_unknown'),
        'excursion_bounds': closed.get('excursion_bounds'),
        'price_basis': closed.get('price_basis', 'legacy_unverified'),
        'original_entry_price': closed.get('original_entry_price', row['entry_price']),
        'corporate_actions': closed.get('corporate_actions', []),
        'split_factor': closed.get('split_factor', 1.0),
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
        'setup': _setup_note(row['ticker'], closed.get('signal_date', row['entry_date']), closed),
        'src': 'monitor',
        'computed_on': datetime.utcnow().date().isoformat(),
        # AUDIT F02/F03: gap fills and ambiguous bars are recorded on the trade itself.
        'note': 'auto-recorded by monitor close' + (f'; {exit_note}' if exit_note else ''),
        'conserv_day': conserv_day,
        'tp1_day': days_to_exit if reason == 'TP1' else None,
        'tp2_day': None,
        'tp3_day': None,
        'stop_day': days_to_exit if reason == 'STOP' else None,
        'also_reached': also or '—',
        'uw_score': closed.get('filter_score') or 0,
        'detected_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        # the close OUTBOX: announced from this record by monitor.publish_pending()
        'publication': {'telegram': 'pending', 'x': 'pending', 'tg_attempts': 0, 'x_attempts': 0},
    }
    trades.append(rec)
    _atomic_write(path, json.dumps(trades, indent=2, ensure_ascii=False))
    print(f"  [record] {row['ticker']} appended to closed_trades.json "
          f"({len(trades)} total)")
    return rec, True


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
