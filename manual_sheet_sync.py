"""Sync MANUAL skew trades to a 'Manual Skew Trades' tab on the SAME Google Sheet
as the GitHub bot (GOOGLE_SHEET_ID), using the same service-account creds.

Open trades show LIVE unrealized P/L (current price via yfinance); closed trades
show realized profit + heat/peak. Manual = pure skew, so NO UW columns here.

Run:  python manual_sheet_sync.py            # writes the tab
      python manual_sheet_sync.py --dry-run  # print rows, don't write
"""
import os
import base64
import json

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_OK = True
except ImportError:
    GSPREAD_OK = False

import manual_trades as MT

TAB = 'Manual Skew Trades'
HEADER = ['ticker', 'status', 'entry_date', 'entry_price',
          'current/exit', 'result_%', 'days_held', 'outcome/reason',
          'T1', 'T2', 'T3', 'stop',
          'MAE % (heat first)', 'MFE % (peak)', 'peak_day', 'green_day',
          'why we entered (setup)', 'note']


def _days_held(entry_date, exit_date=None):
    """Calendar days entry->exit (or entry->today for open trades)."""
    import datetime as _dt
    try:
        e = _dt.date.fromisoformat(entry_date[:10])
        end = _dt.date.fromisoformat(exit_date[:10]) if exit_date else _dt.date.today()
        return (end - e).days
    except Exception:
        return ''


def _client():
    if not GSPREAD_OK:
        raise RuntimeError('gspread not installed — pip install gspread google-auth')
    creds_b64 = os.environ.get('GOOGLE_SHEETS_CREDS_BASE64', '')
    if not creds_b64:
        raise RuntimeError('GOOGLE_SHEETS_CREDS_BASE64 not set')
    creds = Credentials.from_service_account_info(
        json.loads(base64.b64decode(creds_b64)),
        scopes=['https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive'])
    return gspread.authorize(creds)


def _live_price(ticker):
    try:
        import yfinance as yf
        d = yf.download(ticker, period='2d', interval='1d', progress=False, auto_adjust=False)
        if d is None or len(d) == 0:
            return None
        if hasattr(d.columns, 'levels'):
            d.columns = [c[0] for c in d.columns]
        return float(d['Close'].iloc[-1])
    except Exception:
        return None


def _fmt(x, dec=2, pct=False):
    if x is None or x == '':
        return ''
    if pct:
        return f'{x:+.1f}%'
    return f'{x:.{dec}f}'


def build_rows():
    rows = []
    # OPEN first (live P/L)
    for t in MT.get_open():
        px = _live_price(t['ticker'])
        unreal = round((px / t['entry_price'] - 1) * 100, 1) if px else None
        rows.append([
            t['ticker'], 'OPEN', t['entry_date'], _fmt(t['entry_price']),
            _fmt(px), _fmt(unreal, pct=True), _days_held(t['entry_date']), 'live',
            _fmt(t.get('T1')), _fmt(t.get('T2')), _fmt(t.get('T3')), _fmt(t.get('stop')),
            '', '', '', '', t.get('setup', ''), t.get('note', ''),
        ])
    # CLOSED (realized + heat/peak)
    for t in MT.get_closed():
        rows.append([
            t['ticker'], 'CLOSED', t['entry_date'], _fmt(t['entry_price']),
            _fmt(t.get('exit_price')), _fmt(t.get('result_pct'), pct=True),
            _days_held(t['entry_date'], t.get('exit_date')),
            f"{t.get('outcome','')}/{t.get('exit_reason','') or '-'}",
            _fmt(t.get('T1')), _fmt(t.get('T2')), _fmt(t.get('T3')), _fmt(t.get('stop')),
            _fmt(t.get('heat_pct'), pct=True), _fmt(t.get('peak_pct'), pct=True),
            t.get('peak_day') if t.get('peak_day') is not None else '',
            t.get('first_green_day') if t.get('first_green_day') is not None else '',
            t.get('setup', ''), t.get('note', ''),
        ])
    return rows


def sync(dry_run=False):
    rows = build_rows()
    if dry_run:
        print('  ' + ' | '.join(HEADER))
        for r in rows:
            print('  ' + ' | '.join(str(x) for x in r))
        print(f'\n[dry-run] {len(rows)} rows (not written)')
        return True
    if not os.environ.get('GOOGLE_SHEET_ID') or not os.environ.get('GOOGLE_SHEETS_CREDS_BASE64'):
        print('[manual-sheet] creds/sheet not configured — skip')
        return False
    sh = _client().open_by_key(os.environ['GOOGLE_SHEET_ID'])
    try:
        ws = sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB, rows=200, cols=len(HEADER) + 2)
    ws.clear()
    ws.update(values=[HEADER] + rows, range_name='A1')
    print(f'[manual-sheet] "{TAB}" synced: {len(rows)} trades')
    return True


if __name__ == '__main__':
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    sync(dry_run=a.dry_run)
