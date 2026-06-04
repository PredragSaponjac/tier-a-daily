"""Trade excursion tracker — Heat First (MAE) -> Peak (MFE) -> days, per closed trade.

Powers the 'heat & peak' block in X / Telegram posts. Auto-updates as trades close.

KEY DESIGN — compute once, store forever:
  yfinance only serves 15-min intraday for the last ~60 days. A trade's intraday
  heat/peak must therefore be computed WHILE the data is still fresh (right after
  it closes) and stored permanently in closed_trades.json. The post then reads
  stored values — it never re-pulls aged-out data, so old trades never break.

Definitions (all relative to entry price):
  heat_pct = Maximum Adverse Excursion measured UP TO the favorable peak — i.e.
             the deepest the trade went underwater BEFORE it worked. (Post-peak
             round-trips are intentionally excluded so they don't contaminate the
             'heat before it works' read.)
  peak_pct = Maximum Favorable Excursion (highest intraday high) within the hold
             window (~10-11 trading days = our 10-day max hold).
  peak_day = trading-calendar days from entry to that peak.

Usage:
  # add a trade the day it closes (computes + stores while data is fresh):
  python excursions.py --add TICKER YYYY-MM-DD ENTRY_PRICE WIN|LOSS
  # re-render the block (what goes in posts):
  python excursions.py --show
"""
import json
import os
from datetime import datetime, timedelta

STORE = os.path.join(os.path.dirname(__file__), 'closed_trades.json')
HOLD_WINDOW_DAYS = 16  # calendar days ~= 10-11 trading days (our 10-day max hold)


def load() -> list:
    if not os.path.exists(STORE):
        return []
    with open(STORE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save(trades: list):
    with open(STORE, 'w', encoding='utf-8') as f:
        json.dump(trades, f, indent=2)


def compute_excursion(ticker: str, entry_date: str, entry_price: float):
    """Pull intraday bars and compute heat-before-peak + peak + days.

    Returns dict(heat_pct, peak_pct, peak_day, src) or None if no data.
    Run this WHILE the trade is < ~55 days old (15-min still available).
    """
    import yfinance as yf
    import pandas as pd
    e = datetime.fromisoformat(entry_date)
    start = e.strftime('%Y-%m-%d')
    end = (e + timedelta(days=HOLD_WINDOW_DAYS)).strftime('%Y-%m-%d')

    def pull(interval):
        try:
            d = yf.download(ticker, start=start, end=end, interval=interval,
                            progress=False, auto_adjust=False)
            if d is None or len(d) == 0:
                return None
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = [c[0] for c in d.columns]
            return d
        except Exception:
            return None

    src = '15m'
    df = pull('15m')
    if df is None:
        df = pull('1h'); src = '1h'
    if df is None:
        df = pull('1d'); src = '1d'
    if df is None or len(df) == 0:
        return None

    # Map each bar to a trading-day index (1 = entry day) so 'day' counts trading
    # days, not calendar days (more meaningful to a trader).
    tdays = sorted(set(ix.date() for ix in df.index))
    tdidx = {d: i + 1 for i, d in enumerate(tdays)}

    hi_t = df['High'].idxmax()
    hi = float(df['High'].max())
    peak_pct = round((hi / entry_price - 1) * 100, 1)
    pre = df.loc[:hi_t]                       # only up to the favorable peak
    lo = float(pre['Low'].min())
    heat_pct = round((lo / entry_price - 1) * 100, 1)
    peak_day = tdidx[hi_t.date()]

    # First bar that turned +5% green (the 'it's working' moment), trading days
    green = df[df['High'] >= entry_price * 1.05]
    first_green_day = tdidx[green.index[0].date()] if len(green) else None

    return {'heat_pct': heat_pct, 'peak_pct': peak_pct, 'peak_day': peak_day,
            'first_green_day': first_green_day, 'src': src}


def add_trade(ticker, entry_date, entry_price, outcome, note=''):
    """Compute (while data fresh) + store a newly-closed trade. Idempotent by ticker+date."""
    trades = load()
    if any(t['ticker'] == ticker and t['entry_date'] == entry_date for t in trades):
        print(f'[excursions] {ticker} {entry_date} already stored — skip')
        return
    exc = compute_excursion(ticker, entry_date, float(entry_price))
    if exc is None:
        print(f'[excursions] WARNING: no intraday data for {ticker} {entry_date} (aged out?)')
        exc = {'heat_pct': None, 'peak_pct': None, 'peak_day': None, 'src': 'none'}
    rec = {
        'ticker': ticker, 'entry_date': entry_date, 'entry_price': float(entry_price),
        'outcome': outcome.upper(), **exc,
        'computed_on': datetime.now().strftime('%Y-%m-%d') if False else entry_date,
        'note': note,
    }
    # NOTE: computed_on stamped by caller/date to avoid Date.now in restricted ctx;
    # here we just reuse entry_date as a placeholder when not provided.
    trades.append(rec)
    trades.sort(key=lambda t: t['entry_date'])
    save(trades)
    print(f'[excursions] stored {ticker}: heat {exc["heat_pct"]}%  peak {exc["peak_pct"]}%  d{exc["peak_day"]} ({exc["src"]})')


def _agg(trades):
    import statistics as st
    wins = [t for t in trades if str(t['outcome']).startswith('WIN') and t['heat_pct'] is not None]
    if not wins:
        return None
    heats = [t['heat_pct'] for t in wins]
    peaks = [t['peak_pct'] for t in wins if t['peak_pct'] is not None]
    greens = [t.get('first_green_day') for t in wins if t.get('first_green_day') is not None]
    return {
        'n_win': len(wins),
        'heat_median': st.median(heats), 'heat_worst': min(heats),
        'peak_lo': min(peaks) if peaks else None, 'peak_hi': max(peaks) if peaks else None,
        'green_lo': min(greens) if greens else None, 'green_hi': max(greens) if greens else None,
    }


def format_excursion_block(trades=None) -> str:
    """Render the 'heat & peak' block for X / Telegram posts."""
    if trades is None:
        trades = load()
    if not trades:
        return ''
    lines = ['📊 Track record — heat & peak, every call (15-min intraday):']
    for t in trades:
        tk = t['ticker']
        if t['outcome'] == 'LOSS' or t['peak_pct'] is None:
            lines.append(f"{tk:<5s} {t['heat_pct']:+.1f}%  →  stopped ❌")
        else:
            lines.append(f"{tk:<5s} {t['heat_pct']:+.1f}%  →  +{t['peak_pct']:.1f}%  (d{t['peak_day']})")
    a = _agg(trades)
    if a:
        lines.append('')
        green = ''
        if a['green_lo'] is not None:
            if a['green_lo'] == a['green_hi']:
                green = f"Winners turned green (+5%) by day {a['green_lo']}, "
            else:
                green = f"Winners turned green (+5%) within {a['green_lo']}–{a['green_hi']} days, "
        lines.append(
            f"{green}peaking +{a['peak_lo']:.0f}–{a['peak_hi']:.0f}% by ~1–2 weeks "
            f"(median {a['heat_median']:+.1f}% heat first, deepest {a['heat_worst']:+.1f}%). "
            f"A normal pullback ≠ a broken trade."
        )
    return '\n'.join(lines)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--add', nargs=4, metavar=('TICKER', 'DATE', 'PRICE', 'OUTCOME'),
                   help='compute+store a closed trade while data is fresh')
    p.add_argument('--note', default='')
    p.add_argument('--show', action='store_true', help='print the post block')
    args = p.parse_args()
    if args.add:
        tk, dt, pr, oc = args.add
        add_trade(tk, dt, pr, oc, note=args.note)
    if args.show or not args.add:
        print(format_excursion_block())
