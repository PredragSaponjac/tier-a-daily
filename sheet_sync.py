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
TR_HEADER = ['ticker', 'entry_date', 'entry_price',
             'result @TP1 (+10%)', 'days→TP1', 'also reached',
             'result @+7.5% (conserv)', 'days→+7.5%',
             'MAE_% (heat)', 'MFE_% (peak)', 'days→MFE',
             'outcome', 'UW_confirmed', 'notes (why we entered)']
OP_HEADER = ['ticker','entry_date','entry_price','now_price','pnl_pct_now','days_held',
             'T1','T2','T3','STOP',
             'MAE_pct','MAE_date','MFE_pct','MFE_date',
             'filter_score','z_ncp','coi_pct','poi_pct','dp_blocks_10d',
             'parameters_version','added_at']


def _current_price(ticker):
    """Latest close for an open ticker (None if unavailable / throttled)."""
    try:
        import yfinance as yf
        d = yf.Ticker(ticker).history(period='5d', interval='1d', auto_adjust=True)
        if d is None or len(d) == 0:
            return None
        return float(d['Close'].iloc[-1])
    except Exception:
        return None


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


def _d(x):
    """Format a trading-day count as 'dN' or em-dash."""
    return f"d{x}" if x else '—'


def _bot_rows():
    """Bot (UW) closed trades from track_record.csv -> unified exit-model schema."""
    import csv
    if not PT.RECORD_FILE.exists():
        return []
    out = []
    with PT.RECORD_FILE.open() as f:
        for r in csv.DictReader(f):
            score = r.get('filter_score', '')
            try:
                uw = 'Yes' if int(float(score)) >= 3 else 'No'
            except Exception:
                uw = '—'
            tp1d = r.get('time_to_TP1_days', '')
            res_tp1 = '+10.0%' if str(tp1d).strip() not in ('', 'None') else _pct(r.get('realized_return_pct'))
            out.append([
                r.get('ticker', ''), r.get('entry_date', ''), r.get('entry_price', ''),
                res_tp1, _d(tp1d) if str(tp1d).strip() not in ('', 'None') else '—', '—',
                '', '',                       # bot doesn't model the +7.5% conserv exit
                _pct(r.get('MAE_pct')), _pct(r.get('MFE_pct')), r.get('time_to_MFE_days', ''),
                'WIN' if _num(r.get('realized_return_pct')) > 0 else 'LOSS', uw, '',
            ])
    return out


def _num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def _manual_rows():
    """Manual (pure-skew) closed trades from closed_trades.json -> exit-model schema."""
    import json
    path = os.path.join(os.path.dirname(__file__), 'closed_trades.json')
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        trades = json.load(f)
    out = []
    for t in trades:
        peak = t.get('peak_pct')
        tp1d = t.get('tp1_day')
        conservd = t.get('conserv_day')
        stopped = t.get('stop_day') is not None and tp1d is None and conservd is None
        # result @TP1
        if tp1d:
            res_tp1 = '+10.0%'
        elif stopped:
            res_tp1 = '−7% stop'
        else:
            res_tp1 = f"no (peak {peak:+.1f}%)" if peak is not None else 'no'
        # result @+7.5% conservative
        if conservd:
            res_con = '+7.5%'
        elif stopped:
            res_con = '−7% stop'
        else:
            res_con = 'no'
        days_to_mfe = (t['peak_day'] - 1) if t.get('peak_day') else ''   # trading days after entry
        # UW verdict: Yes if the UW composite (>=3/4) confirms it, else No (with score).
        uw_score = t.get('uw_score')
        if uw_score is None:
            uw = '—'
        elif uw_score >= 3:
            uw = f'Yes ({uw_score}/4)'
        else:
            uw = f'No ({uw_score}/4)'
        out.append([
            t['ticker'], t['entry_date'], t.get('entry_price', ''),
            res_tp1, _d(tp1d), t.get('also_reached', '—'),
            res_con, _d(conservd),
            _pct(t.get('heat_pct')), _pct(peak), days_to_mfe,
            t.get('outcome', ''), uw, t.get('setup', ''),
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
    _color_outcomes(ws, rows)   # green = win, red = loss (re-applied every sync)
    print(f'[sheet] Track Record synced: {len(rows)} closed trades (bot + manual)')
    return True


# Outcome row colors (ws.clear() wipes formatting, so we re-apply on every sync).
_GREEN = {'red': 0.85, 'green': 0.93, 'blue': 0.83}   # win
_RED = {'red': 0.96, 'green': 0.80, 'blue': 0.80}     # loss
_GREY = {'red': 0.95, 'green': 0.95, 'blue': 0.95}    # open/neutral


def _color_outcomes(ws, rows):
    """Shade each data row green (win) / red (loss) by the 'outcome' column."""
    last_col = chr(ord('A') + len(TR_HEADER) - 1)
    oc_idx = TR_HEADER.index('outcome')
    requests = []
    for i, row in enumerate(rows, start=2):
        oc = str(row[oc_idx]).upper()
        color = _RED if oc.startswith('LOSS') else (_GREEN if oc.startswith('WIN') else _GREY)
        requests.append({'range': f'A{i}:{last_col}{i}',
                         'format': {'backgroundColor': color}})
    try:
        ws.batch_format(requests)
    except Exception as e:
        print(f'[sheet] row coloring skipped: {e}')


def sync_open_positions():
    """Replace the Open Positions tab with current open_positions.json."""
    sh = _open_sheet()
    ws = _ensure_tab(sh, 'Open Positions', OP_HEADER)
    ws.clear()
    import datetime as _dt
    rows = []
    for p in PT.list_open():
        p = dict(p)  # copy so we can inject live fields
        entry = p.get('entry_price')
        now = _current_price(p.get('ticker'))
        if now and entry:
            p['now_price'] = round(now, 2)
            p['pnl_pct_now'] = f"{(now / entry - 1) * 100:+.1f}%"
        else:
            p['now_price'] = ''
            p['pnl_pct_now'] = ''
        p['days_held'] = _days_between(p.get('entry_date'), _dt.date.today().isoformat())
        rows.append([p.get(c, '') for c in OP_HEADER])
    ws.update(values=[OP_HEADER] + rows, range_name='A1')
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
