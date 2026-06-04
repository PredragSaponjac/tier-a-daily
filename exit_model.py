"""Rule-based exit model — daily High/Low backtest of each trade.

Standardized exit = TP1 (+10%): the day the daily HIGH first tags +10%, the
trade is marked closed at TP1. We also record:
  - whether it later tagged TP2 (+11%) / TP3 (+20%)  -> upside left on the table
  - a CONSERVATIVE +7.5% exit (75% of TP1) -> catches trades that stall just short
  - the STOP (-7%): if the daily LOW hits it BEFORE any target, the trade is a loss

Scanning starts the trading day AFTER entry (so an entry-day pre-entry high never
counts) and runs the 10-day max-hold window. Day counts are trading days from entry.

Daily data is always available (no 60-day limit), but we still store the computed
fields in closed_trades.json so the sheet renders instantly without re-pulling.

CLI:  python exit_model.py AAPL 2026-05-01 100.00
"""
from datetime import datetime, timedelta

TP1, TP2, TP3, CONSERV, STOP = 10.0, 11.0, 20.0, 7.5, -7.0
WINDOW_DAYS = 16  # ~10-11 trading days = the 10-day max hold


def model_exits(ticker, entry_date, entry_price, window_days=WINDOW_DAYS):
    """Return dict of exit-model fields, or None if no price data."""
    import yfinance as yf
    import pandas as pd
    e = datetime.fromisoformat(entry_date[:10])
    start = (e + timedelta(days=1)).strftime('%Y-%m-%d')   # day AFTER entry
    end = (e + timedelta(days=window_days)).strftime('%Y-%m-%d')
    try:
        df = yf.download(ticker, start=start, end=end, interval='1d',
                         progress=False, auto_adjust=False)
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    def first_high_cross(pct):
        tgt = entry_price * (1 + pct / 100)
        for i, (_, row) in enumerate(df.iterrows(), 1):
            if float(row['High']) >= tgt:
                return i
        return None

    stop_day = None
    st = entry_price * (1 + STOP / 100)
    for i, (_, row) in enumerate(df.iterrows(), 1):
        if float(row['Low']) <= st:
            stop_day = i
            break

    def hit_before_stop(pct):
        d = first_high_cross(pct)
        if d is None:
            return None
        if stop_day is not None and stop_day < d:
            return None          # stopped out before reaching this level
        return d

    conserv_day = hit_before_stop(CONSERV)
    tp1_day = hit_before_stop(TP1)
    tp2_day = hit_before_stop(TP2)
    tp3_day = hit_before_stop(TP3)

    if tp3_day:
        also = 'TP3 +20%'
    elif tp2_day:
        also = 'TP2 +11%'
    else:
        also = '—'

    if tp1_day:
        outcome = 'WIN'
    elif conserv_day:
        outcome = 'WIN (conserv)'
    elif stop_day:
        outcome = 'LOSS'
    else:
        outcome = 'open/timeout'

    return {
        'conserv_day': conserv_day, 'tp1_day': tp1_day,
        'tp2_day': tp2_day, 'tp3_day': tp3_day, 'stop_day': stop_day,
        'also_reached': also, 'outcome': outcome,
    }


if __name__ == '__main__':
    import sys
    tk, dt, pr = sys.argv[1], sys.argv[2], float(sys.argv[3])
    print(model_exits(tk, dt, pr))
