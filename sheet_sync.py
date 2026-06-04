"""Sync track record + open positions to a public Google Sheet.

Uses the existing GOOGLE_SHEETS_CREDS service account from MEMORY.md
(orca-sheet-bot@vix-code.iam.gserviceaccount.com).

User must create the sheet and share it with that service account email
(see PHASE2.md). Sheet ID goes in env var GOOGLE_SHEET_ID.

Two tabs auto-managed:
- "Track Record"   : every closed signal, append-only, with MAE/MFE
- "Open Positions" : currently monitored signals, refreshed each sync
"""
import os
import base64
import json
from pathlib import Path

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_OK = True
except ImportError:
    GSPREAD_OK = False

import position_tracker as PT

# UNIFIED Track Record schema (manual skew trades + bot UW trades in ONE tab).
# The detailed UW columns (z_ncp, coi_pct, poi_pct, dp_blocks_10d, dp_cumul_10d_M,
# parameters_version) are collapsed into a single 'UW_confirmed' flag. 'notes'
# holds a brief why-we-entered description (the setup).
TR_HEADER = ['ticker', 'entry_date', 'entry_price', 'exit_date', 'exit_price',
             'result_%', 'days_held', 'MAE_% (heat)', 'MFE_% (peak)', 'days_to_MFE',
             'outcome/reason', 'UW_confirmed', 'notes (why we entered)']
OP_HEADER = ['ticker','entry_date','entry_price','T1','T2','T3','STOP',
             'MAE_pct','MAE_date','MFE_pct','MFE_date',
             'filter_score','z_ncp','coi_pct','poi_pct','dp_blocks_10d',
             'parameters_version','added_at']


def _days_between(d0, d1):
    import datetime as _dt
    try:
        a = _dt.date.fromisoformat(str(d0)[:10])
        b = _dt.date.fromisoformat(str(d1)[:10])
        return (b - a).days
    except Exception:
        return ''


def _pct(x):
    try:
        return f'{float(x):+.1f}%'
    except Exception:
        return ''


def _bot_rows():
    """Bot (UW) closed trades from track_record.csv -> unified schema."""
    import csv
    if not PT.RECORD_FILE.exists():
        return []
    out = []
    with PT.RECORD_FILE.open() as f:
        for r in csv.DictReader(f):
            score = r.get('filter_score', '')
            try:
                uw = f"{int(float(score))}/4 ✅" if int(float(score)) >= 3 else f"{int(float(score))}/4"
            except Exception:
                uw = '—'
            out.append([
                r.get('ticker', ''), r.get('entry_date', ''), r.get('entry_price', ''),
                r.get('exit_date', ''), r.get('exit_price', ''),
                _pct(r.get('realized_return_pct')),
                _days_between(r.get('entry_date'), r.get('exit_date')),
                _pct(r.get('MAE_pct')), _pct(r.get('MFE_pct')), r.get('time_to_MFE_days', ''),
                r.get('exit_reason', ''), uw, '',
            ])
    return out


def _manual_rows():
    """Manual (pure-skew) closed trades from closed_trades.json -> unified schema."""
    import json
    path = os.path.join(os.path.dirname(__file__), 'closed_trades.json')
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        trades = json.load(f)
    out = []
    for t in trades:
        out.append([
            t['ticker'], t['entry_date'], t.get('entry_price', ''),
            t.get('exit_date', ''), t.get('exit_price', ''),
            _pct(t.get('result_pct')),
            _days_between(t.get('entry_date'), t.get('exit_date')),
            _pct(t.get('heat_pct')), _pct(t.get('peak_pct')),
            t.get('days_to_mfe') if t.get('days_to_mfe') is not None else '',
            f"{t.get('outcome','')}/{t.get('exit_reason','') or '-'}",
            'Skew only', t.get('setup', ''),
        ])
    return out


def _client():
    if not GSPREAD_OK:
        raise RuntimeError('gspread not installed — pip install gspread google-auth')
    creds_b64 = os.environ.get('GOOGLE_SHEETS_CREDS_BASE64', '')
    if not creds_b64:
        raise RuntimeError('GOOGLE_SHEETS_CREDS_BASE64 env var not set')
    creds_json = json.loads(base64.b64decode(creds_b64))
    creds = Credentials.from_service_account_info(creds_json, scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ])
    return gspread.authorize(creds)


def _open_sheet():
    sheet_id = os.environ.get('GOOGLE_SHEET_ID', '')
    if not sheet_id:
        raise RuntimeError('GOOGLE_SHEET_ID env var not set')
    return _client().open_by_key(sheet_id)


def _ensure_tab(sh, title, header):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=200, cols=len(header) + 2)
        ws.append_row(header)
    # Ensure header row matches
    first_row = ws.row_values(1) if ws.row_count >= 1 else []
    if first_row != header:
        ws.update('A1', [header])
    return ws


def sync_track_record():
    """Replace the unified Track Record tab: bot (UW) + manual (skew) closed trades,
    sorted by entry_date. Also removes the legacy 'Manual Skew Trades' tab if present."""
    rows = _bot_rows() + _manual_rows()
    rows.sort(key=lambda r: str(r[1]))   # by entry_date
    if not rows:
        print('[sheet] no closed trades yet (bot or manual), nothing to sync')
        return False

    sh = _open_sheet()
    # Retire the old separate manual tab if it still exists.
    try:
        old = sh.worksheet('Manual Skew Trades')
        sh.del_worksheet(old)
        print('[sheet] removed legacy "Manual Skew Trades" tab (now unified)')
    except gspread.WorksheetNotFound:
        pass

    ws = _ensure_tab(sh, 'Track Record', TR_HEADER)
    ws.clear()
    ws.update(values=[TR_HEADER] + rows, range_name='A1')
    print(f'[sheet] Track Record synced: {len(rows)} closed trades (bot + manual)')
    return True


def sync_open_positions():
    """Replace the Open Positions tab with current open_positions.json."""
    sh = _open_sheet()
    ws = _ensure_tab(sh, 'Open Positions', OP_HEADER)
    ws.clear()
    rows = []
    for p in PT.list_open():
        rows.append([p.get(c, '') for c in OP_HEADER])
    ws.update('A1', [OP_HEADER] + rows)
    print(f'[sheet] Open Positions synced: {len(rows)} open')
    return True


def sync_all():
    if not os.environ.get('GOOGLE_SHEET_ID') or not os.environ.get('GOOGLE_SHEETS_CREDS_BASE64'):
        print('[sheet] Google Sheet not configured (env vars missing), skipping')
        return False
    try:
        sync_track_record()
        sync_open_positions()
        return True
    except Exception as e:
        print(f'[sheet] sync failed: {e}')
        return False


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    sync_all()
