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

TR_HEADER = PT.RECORD_COLUMNS
OP_HEADER = ['ticker','entry_date','entry_price','T1','T2','T3','STOP',
             'MAE_pct','MAE_date','MFE_pct','MFE_date',
             'filter_score','z_ncp','coi_pct','poi_pct','dp_blocks_10d',
             'parameters_version','added_at']


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
    """Replace the Track Record tab with current track_record.csv contents."""
    if not PT.RECORD_FILE.exists():
        print('[sheet] no track_record.csv yet, nothing to sync')
        return False
    import csv
    rows = []
    with PT.RECORD_FILE.open() as f:
        for r in csv.DictReader(f):
            rows.append([r.get(c, '') for c in TR_HEADER])

    sh = _open_sheet()
    ws = _ensure_tab(sh, 'Track Record', TR_HEADER)
    ws.clear()
    ws.update('A1', [TR_HEADER] + rows)
    print(f'[sheet] Track Record synced: {len(rows)} closed signals')
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
