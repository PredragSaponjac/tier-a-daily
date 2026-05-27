"""Read Tier A candidates from skew_history.db for a given scan date (default: latest)."""
import os
import sqlite3
from pathlib import Path

# Path to the skew-tracker DB. Override via SKEW_DB_PATH env var (used in CI).
# Default for local dev: sibling C:\Users\18329\Downloads\skew-tracker repo.
SKEW_DB = Path(os.environ.get('SKEW_DB_PATH',
    r'C:\Users\18329\Downloads\skew-tracker\skew_history.db'))

EXCLUDED_ETFS = {'UVIX','UVXY','VXX','VIXY','SVIX','SVXY','SOXL','SOXS','TQQQ','SQQQ',
    'SPXL','SPXU','UPRO','SPXS','TNA','TZA','LABU','LABD','FAS','FAZ','JNUG','JDST','NUGT','DUST',
    'TMF','TMV','BOIL','KOLD','UCO','SCO','AGQ','ZSL','UGL','GLL','YINN','YANG','DPST','URE','SRS',
    'QLD','QID','SSO','SDS','DDM','DXD','ERX','ERY','GUSH','DRIP','TSLL','TSLQ','NVDL','NVD',
    'CONL','CONY','MSTU','MSTX','MSTZ','BITX','BITU','DFDV','FNGU','FNGD','WEBL','WEBS'}


def _keep(r):
    return r['ticker'] not in EXCLUDED_ETFS and (r['sector'] or '') != 'Unknown'


def _is_tier_a(r):
    return (r['spot_return_pct'] is not None and r['spot_return_pct'] <= -8
            and r['skew_change_5d'] is not None and r['skew_change_5d'] <= -7
            and r['near_skew'] is not None and r['near_skew'] <= -7)


def read_tier_a(scan_date: str = None) -> list[dict]:
    """Return list of Tier A candidates for scan_date (or latest if None).

    Each dict has: ticker, scan_date, spot_close, spot_return_pct, skew_change_5d,
    near_skew, near_dte, put_wall_strike, put_wall_oi_change, sector, industry,
    dte_earnings (if present), fwd_5d_return (None for today's signals).
    """
    if not SKEW_DB.exists():
        raise FileNotFoundError(f'Skew DB not found at {SKEW_DB}. Make sure skew-tracker repo is at sibling path.')

    c = sqlite3.connect(str(SKEW_DB))
    c.row_factory = sqlite3.Row
    cur = c.cursor()

    if scan_date is None:
        scan_date = cur.execute('SELECT MAX(scan_date) FROM candidate_log').fetchone()[0]

    base = '''SELECT * FROM candidate_log
              WHERE scan_date = ?
                AND current_signal = 'BULLISH_REVERSAL'
                AND near_dte <= 6
                AND skew_change_5d <= -5
                AND near_skew <= -5
                AND put_wall_oi_change IS NOT NULL
                AND put_wall_oi_change <= 0'''
    rows = [r for r in cur.execute(base, (scan_date,)).fetchall() if _keep(r) and _is_tier_a(r)]
    c.close()

    out = []
    for r in rows:
        out.append({
            'ticker': r['ticker'],
            'scan_date': r['scan_date'],
            'spot_close': r['spot_close'],
            'spot_return_pct': r['spot_return_pct'],
            'skew_change_5d': r['skew_change_5d'],
            'near_skew': r['near_skew'],
            'near_dte': r['near_dte'],
            'put_wall_strike': r['put_wall_strike'],
            'put_wall_oi_change': r['put_wall_oi_change'],
            'sector': r['sector'],
            'industry': r['industry'],
            'dte_earnings': r['dte_earnings'],
        })
    return out, scan_date


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--scan-date', default=None, help='YYYY-MM-DD (default: latest)')
    args = p.parse_args()
    rows, sd = read_tier_a(args.scan_date)
    print(f'Tier A candidates for scan_date {sd}: {len(rows)}')
    for r in rows:
        print(f"  {r['ticker']:6s} close=${r['spot_close']:.2f} ret={r['spot_return_pct']:+.1f}% "
              f"skewd5d={r['skew_change_5d']:+.1f} near_skew={r['near_skew']:+.1f} "
              f"dte={r['near_dte']} pwall=${r['put_wall_strike']} {r['sector']}")
