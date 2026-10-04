"""Final scan marker: exact stored generation and completed decision identity."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile

import db_state
from state_lock import trading_date

DB = os.environ.get('SKEW_DB_PATH', 'skew_history.db')


def write_marker(tag, date=None):
    tag = tag.upper()
    if tag not in ('AM', 'PM'):
        raise ValueError('marker must be AM or PM')
    date = date or trading_date().isoformat()
    dt.date.fromisoformat(date)
    con = sqlite3.connect(Path(DB).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        q = lambda sql: con.execute(sql).fetchone()[0]
        latest = q('SELECT MAX(scan_date) FROM candidate_log')
        if latest != date:
            raise RuntimeError(f'latest scan is {latest}, expected {date}')
        rec = {'scan': tag, 'latest_scan_date': date,
               'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
               'candidate_log_rows': q('SELECT COUNT(*) FROM candidate_log'),
               'skew_daily_rows': q('SELECT COUNT(*) FROM skew_daily'),
               'fixed_strike_vol_rows': q('SELECT COUNT(*) FROM fixed_strike_vol'),
               'db_bytes': os.path.getsize(DB), 'db_generation': db_state.generation()}
    finally:
        con.close()
    if tag == 'PM':
        archive = json.loads(Path('signals', f'{date}.json').read_text(encoding='utf-8'))
        if archive.get('scan_date') != date or archive.get('delivery', {}).get('complete') is not True:
            raise RuntimeError('PM marker requires a completed, durable decision outbox')
        if not archive.get('run_id'):
            raise RuntimeError('PM marker requires a decision run identity')
        rec['decision_run_id'] = archive['run_id']
        decision_gen = archive.get('data_generation')
        if not isinstance(decision_gen, dict) or not decision_gen.get('sha256') or not decision_gen.get('name'):
            raise RuntimeError('PM marker requires the original decision database generation')
        # A recovery scan can be newer than the DB used for the frozen decision.
        # Keep both identities instead of attributing an old decision to new data.
        rec['decision_db_generation'] = decision_gen
    out = Path(f'last_scan_{tag.lower()}.json')
    fd, tmp = tempfile.mkstemp(prefix=f'.{out.name}.', suffix='.tmp', dir=out.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(rec, fh, indent=2)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return rec


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('tag', choices=['AM', 'PM'])
    parser.add_argument('--date')
    args = parser.parse_args()
    rec = write_marker(args.tag, args.date)
    print(f'[marker] {args.tag} {rec["latest_scan_date"]}: {rec["db_generation"]["name"]}')
