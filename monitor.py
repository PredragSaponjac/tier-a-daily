"""Intraday monitor — yfinance-based (FREE, no UW API calls).

For each open position:
  1. Pull intraday OHLC since entry (yfinance, 5-min bars during market hours)
  2. Update MAE/MFE based on intraday highs/lows
  3. If H reaches T1 -> bot auto-closes at TP1, sends close alert
  4. If L reaches stop -> bot exits, sends stop alert
  5. If >= time_stop_days since entry -> timeout close at last price

Run every 15 min during US market hours (9:30am-4pm ET = 14:30-21:00 UTC).
"""
import argparse
import datetime as dt
import os
import yfinance as yf
from dotenv import load_dotenv

import parameters as P
import position_tracker as PT
from alert import format_close, send_telegram


def _today_iso() -> str:
    return dt.date.today().isoformat()


def check_position(pos: dict, dry_run: bool = False) -> dict | None:
    """Check one position for TP1/stop/timeout hits. Returns close info if closed."""
    tk = pos['ticker']
    entry_date = pos['entry_date']
    entry = pos['entry_price']
    T1 = pos['T1']
    STOP = pos['STOP']

    # Pull data from entry+1 onward
    e = dt.datetime.strptime(entry_date, '%Y-%m-%d').date()
    end = e + dt.timedelta(days=P.time_stop_days() + 5)
    try:
        # 1-day bars first to find the relevant window; for today's intraday use 5m
        df = yf.Ticker(tk).history(start=e + dt.timedelta(days=1), end=end, auto_adjust=True, interval='1d')
    except Exception as ex:
        print(f'  [{tk}] yfinance error: {ex}')
        return None

    if df.empty:
        # No price action yet (e.g., entered today after close)
        return None

    # Walk day by day; track MAE/MFE; detect TP1 / stop / timeout
    for idx, row in df.iterrows():
        date_str = idx.date().isoformat()
        high = float(row['High'])
        low = float(row['Low'])
        # Update MAE / MFE
        PT.update_mae_mfe(tk, entry_date, intraday_low=low, intraday_high=high, on_date=date_str)

        # TP1 check
        if high >= T1:
            # Exit at TP1 (assume hit during the day)
            exit_price = T1
            print(f'  [{tk}] TP1 HIT on {date_str} (intraday high {high:.2f} >= T1 {T1:.2f})')
            if dry_run:
                return {'closed': True, 'reason': 'TP1', 'exit_price': exit_price, 'exit_date': date_str}
            closed = PT.close_position(tk, entry_date, exit_price, 'TP1', date_str)
            msg = format_close(tk, entry, exit_price, 'TP1')
            send_telegram(msg)
            return {'closed': True, 'reason': 'TP1', 'exit_price': exit_price, 'exit_date': date_str, 'record': closed}

        # STOP check (only if TP1 not hit first this day)
        if low <= STOP:
            exit_price = STOP
            print(f'  [{tk}] STOP HIT on {date_str} (intraday low {low:.2f} <= STOP {STOP:.2f})')
            if dry_run:
                return {'closed': True, 'reason': 'STOP', 'exit_price': exit_price, 'exit_date': date_str}
            closed = PT.close_position(tk, entry_date, exit_price, 'STOP', date_str)
            msg = format_close(tk, entry, exit_price, 'STOP')
            send_telegram(msg)
            return {'closed': True, 'reason': 'STOP', 'exit_price': exit_price, 'exit_date': date_str, 'record': closed}

    # Neither TP1 nor stop hit yet. Check time-stop.
    today = dt.date.today()
    days_since_entry = (today - e).days
    if days_since_entry >= P.time_stop_days():
        last_close = float(df['Close'].iloc[-1])
        last_date = df.index[-1].date().isoformat()
        print(f'  [{tk}] TIMEOUT at day {days_since_entry} — close at ${last_close:.2f}')
        if dry_run:
            return {'closed': True, 'reason': 'TIMEOUT', 'exit_price': last_close, 'exit_date': last_date}
        closed = PT.close_position(tk, entry_date, last_close, 'TIMEOUT', last_date)
        msg = format_close(tk, entry, last_close, 'TIMEOUT')
        send_telegram(msg)
        return {'closed': True, 'reason': 'TIMEOUT', 'exit_price': last_close, 'exit_date': last_date, 'record': closed}

    # Position still open. MAE/MFE updated above.
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true', help='check positions but do not close or send')
    args = p.parse_args()
    load_dotenv()

    positions = PT.list_open()
    print(f'[{dt.datetime.now().isoformat()}] Monitoring {len(positions)} open position(s)...')

    if not positions:
        print('  (none)')
        return

    closed_count = 0
    for pos in positions:
        result = check_position(pos, dry_run=args.dry_run)
        if result and result.get('closed'):
            closed_count += 1
        else:
            print(f"  [{pos['ticker']}] still open  MAE={pos.get('MAE_pct',0):+.2f}%  MFE={pos.get('MFE_pct',0):+.2f}%")

    print(f'\nClosed this run: {closed_count}  |  Still open: {len(positions) - closed_count}')


if __name__ == '__main__':
    main()
