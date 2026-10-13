"""Manual held-period daily excursion bounds and immutable record presentation.

Entry is the reported close-date price. Only sessions after that date through
the explicit manual exit are eligible. The exit day's ordering is unknown, so
both extrema remain ranges. Old full-window measurements are labeled unavailable.
No maximum holding period or exact intraday timestamps are inferred.
"""
import json
import math
import os
from datetime import datetime, timedelta

STORE = os.path.join(os.path.dirname(__file__), 'closed_trades.json')


def load() -> list:
    if not os.path.exists(STORE):
        return []
    with open(STORE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save(trades: list):
    from pathlib import Path
    from position_tracker import _atomic_write
    # Same serialization as position_tracker's writer, so a save changes only real data.
    _atomic_write(Path(STORE), json.dumps(trades, indent=2, ensure_ascii=False))


def compute_excursion(ticker, entry_date, entry_price, exit_date=None, exit_price=None, asof=None):
    """Held-period daily bounds for an explicit manual exit; no unbounded window."""
    if exit_date is None or exit_price is None:
        return None
    import yfinance as yf
    import pandas as pd
    from market_time import now_eastern, next_session, sessions_between, session_completed
    e, x = datetime.fromisoformat(entry_date).date(), datetime.fromisoformat(exit_date).date()
    if x < e or any(not math.isfinite(v) or v <= 0 for v in (entry_price, exit_price)):
        raise ValueError('invalid manual entry/exit')
    clock = now_eastern(asof)
    if not session_completed(x, clock):
        return None
    # Yahoo also rebases old OHLC for splits after the manual exit. Fetch those
    # actions too, then normalize both reported original fills to one basis.
    frame = yf.download(ticker, start=e + timedelta(days=1), end=clock.date() + timedelta(days=1),
                        interval='1d', auto_adjust=False, actions=True, progress=False)
    if frame is None or frame.empty:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [c[0] for c in frame.columns]
    if not {'Open', 'High', 'Low', 'Close', 'Stock Splits', 'Dividends'}.issubset(frame.columns):
        raise ValueError('manual OHLC/action history incomplete')
    factor = exit_factor = 1.0
    actions = []
    for ix, row in frame.iterrows():
        split, dividend = float(row['Stock Splits']), float(row['Dividends'])
        if not math.isfinite(split) or not math.isfinite(dividend) or split < 0:
            raise ValueError('invalid manual corporate action')
        if split and e < ix.date() <= clock.date():
            factor *= split
            if ix.date() > x:
                exit_factor *= split
            actions.append({'date': ix.date().isoformat(), 'split': split})
        if dividend and e < ix.date() <= clock.date():
            actions.append({'date': ix.date().isoformat(), 'dividend': dividend,
                            'return_treatment': 'excluded_price_only'})
    frame = frame.loc[[e < ix.date() <= x for ix in frame.index]]
    if frame.empty or [ix.date() for ix in frame.index] != sessions_between(next_session(e), x):
        return None
    if not math.isfinite(factor) or not math.isfinite(exit_factor) or factor <= 0 or exit_factor <= 0:
        raise ValueError('invalid cumulative manual split factor')
    for _, row in frame.iterrows():
        opn, high, low, close = (float(row[k]) for k in ('Open', 'High', 'Low', 'Close'))
        if (any(not math.isfinite(v) or v <= 0 for v in (opn, high, low, close)) or
                low > min(opn, close) or high < max(opn, close)):
            raise ValueError('invalid manual daily OHLC')
    entry = entry_price / factor
    fill = exit_price / exit_factor
    before = frame.loc[[ix.date() < x for ix in frame.index]]
    last = frame.loc[[ix.date() == x for ix in frame.index]]
    low_min = low_max = high_min = high_max = entry
    if len(before):
        low_min = low_max = min(entry, float(before['Low'].min()))
        high_min = high_max = max(entry, float(before['High'].max()))
    if len(last):
        r = last.iloc[-1]
        low_min = min(low_min, float(r['Low']))
        if not float(r['Low']) <= fill <= float(r['High']):
            raise ValueError('reported manual exit outside the normalized daily range')
        low_max = min(low_max, float(r['Open']), fill)
        high_min = max(high_min, float(r['Open']), fill)
        high_max = max(high_max, float(r['High']))
    pct = lambda px: (px / entry - 1) * 100
    bounds = {'mae_pct_min': pct(low_min), 'mae_pct_max': pct(low_max),
              'mfe_pct_min': pct(high_min), 'mfe_pct_max': pct(high_max),
              'exact': low_min == low_max and high_min == high_max,
              'method': 'manual_exit_daily_bounds'}
    return {'heat_pct': bounds['mae_pct_min'], 'peak_pct': bounds['mfe_pct_max'],
            'peak_day': None, 'first_green_day': None, 'src': 'daily_bounds',
            'excursion_bounds': bounds, 'price_basis': 'split_normalized_price_only',
            'normalized_entry_price': entry, 'normalized_exit_price': fill,
            'original_exit_price': exit_price, 'quantity_factor': factor,
            'corporate_actions': actions}


def _add_trade(ticker, entry_date, entry_price, outcome, note='', exit_date=None, exit_price=None):
    """Store explicit manual close bounds. Idempotent by ticker+date."""
    trades = load()
    if any(t['ticker'] == ticker and t['entry_date'] == entry_date for t in trades):
        print(f'[excursions] {ticker} {entry_date} already stored — skip')
        return
    exc = compute_excursion(ticker, entry_date, float(entry_price), exit_date, exit_price)
    if exc is None:
        print(f'[excursions] WARNING: held-period daily bounds unavailable for {ticker} {entry_date}')
        exc = {'heat_pct': None, 'peak_pct': None, 'peak_day': None, 'src': 'none'}
    rec = {
        'ticker': ticker, 'entry_date': entry_date, 'entry_price': exc.get('normalized_entry_price', float(entry_price)),
        'original_entry_price': float(entry_price), 'exit_date': exit_date,
        'exit_price': exc.get('normalized_exit_price', exit_price),
        'outcome': outcome.upper(), **exc,
        'computed_on': datetime.now().date().isoformat(),
        'note': note,
    }
    trades.append(rec)
    trades.sort(key=lambda t: t['entry_date'])
    save(trades)
    print(f'[excursions] stored {ticker}: heat {exc["heat_pct"]}%  peak {exc["peak_pct"]}%  d{exc["peak_day"]} ({exc["src"]})')


def add_trade(*args, **kwargs):
    from state_lock import portfolio_lock, checkpoint
    with portfolio_lock():
        result = _add_trade(*args, **kwargs)
        checkpoint(paths=('closed_trades.json',))
        return result


BACKTEST_LINE = "Paper/manual reported calls; execution costs and actual fills are unverified."


def format_results_line(trades=None) -> str:
    """Auto-generated live track record: per-trade outcome (✅/❌ +result%) +
    W/L summary + backtest line. Never stale — pulls from closed_trades.json."""
    if trades is None:
        trades = load()
    if not trades:
        return ''
    import datetime, statistics as st
    parts, win_res, wins, losses = [], [], 0, 0
    for t in trades:
        oc = str(t.get('outcome', ''))
        res = t.get('result_pct')
        if oc.startswith('WIN'):
            wins += 1
            if res is not None:
                win_res.append(res)
            sign = '✅'
        else:
            losses += 1
            sign = '❌'
        rs = f" {res:+.0f}%" if res is not None else ''
        parts.append(f"{t['ticker']} {sign}{rs}")
    try:
        d = datetime.date.fromisoformat(min(t['entry_date'] for t in trades)[:10])
        since = f"{d.month}/{d.day}"
    except Exception:
        since = '4/20'
    total = wins + losses
    wr = round(100 * wins / total) if total else 0
    avg = round(st.mean(win_res)) if win_res else 0
    return '\n'.join([
        f"📊 Reported calls (since {since}; paper/manual): " + " | ".join(parts),
        f"{wins} of {total} winners ({wr}% win) · avg winner +{avg}%",
        BACKTEST_LINE,
    ])


BOUND_KEYS = ('mae_pct_min', 'mae_pct_max', 'mfe_pct_min', 'mfe_pct_max')
BACKFILL_NOTE = ('held-period daily bounds measured {date} from Yahoo daily bars; the old '
                 'full-window heat_pct/peak_pct are kept as recorded')


def _valid_bounds(b) -> bool:
    return (isinstance(b, dict) and all(isinstance(b.get(k), (int, float)) and math.isfinite(b[k])
                                        for k in BOUND_KEYS))


def backfill_bounds(trades, compute=None, today=None):
    """Measure held-period daily bounds for closed trades that never had them.

    Review 2026-10-13: the 18 trades recorded before the 2026-10-04 audit carried
    full-window heat/peak values that ran past the exit (RDDT +19.1 on a +10 exit), so
    the post printed 'unavailable' for every one of them. This adds `excursion_bounds`
    only — outcome, prices and the old heat/peak fields stay exactly as recorded. A trade
    whose daily bars cannot be validated stays unmeasured and is returned in `failed`.
    """
    compute = compute or compute_excursion
    today = today or datetime.now().date().isoformat()
    added, failed = [], []
    for t in trades:
        if _valid_bounds(t.get('excursion_bounds')) or not t.get('exit_date') or t.get('exit_price') is None:
            continue
        try:
            exc = compute(t['ticker'], t['entry_date'], float(t.get('original_entry_price') or t['entry_price']),
                          t['exit_date'], float(t.get('original_exit_price') or t['exit_price']))
        except Exception as exc_error:
            failed.append(f"{t['ticker']}: {type(exc_error).__name__}: {exc_error}")
            continue
        if not exc or not _valid_bounds(exc.get('excursion_bounds')):
            failed.append(f"{t['ticker']}: daily bars incomplete")
            continue
        t['excursion_bounds'] = exc['excursion_bounds']
        t['excursion_bounds_note'] = BACKFILL_NOTE.format(date=today)
        added.append(t['ticker'])
    return added, failed


def format_excursion_block(trades=None):
    """Worst and best move while each trade was open, from completed daily bars.

    A range means the exit day's intraday order is unknown (its high or low may have come
    after the exit). Trades without a valid measurement share ONE line, so a missing feed
    can never again fill a post with an 'unavailable' line per trade (review 2026-10-13);
    if no trade is measured the block is left out entirely.
    """
    trades = load() if trades is None else trades
    measured = [t for t in trades if _valid_bounds(t.get('excursion_bounds'))]
    if not measured:
        return ''

    def value(b, prefix):
        lo, hi = b[prefix + '_pct_min'], b[prefix + '_pct_max']
        return f'{lo:+.1f}%' if abs(lo - hi) < 0.05 else f'{lo:+.1f}% to {hi:+.1f}%'

    lines = ['Paper record — worst / best move while each trade was open '
             '(daily bars; a range = exit-day order unknown):']
    for t in measured:
        b = t['excursion_bounds']
        lines.append(f"{t['ticker']}: worst {value(b, 'mae')}, best {value(b, 'mfe')}")
    missing = [t['ticker'] for t in trades if not _valid_bounds(t.get('excursion_bounds'))]
    if missing:
        lines.append('Not measured: ' + ', '.join(missing))
    return '\n'.join(lines)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--add', nargs=4, metavar=('TICKER', 'DATE', 'PRICE', 'OUTCOME'),
                   help='compute+store a closed trade while data is fresh')
    p.add_argument('--note', default='')
    p.add_argument('--exit-date')
    p.add_argument('--exit-price', type=float)
    p.add_argument('--show', action='store_true', help='print the post block')
    p.add_argument('--backfill', action='store_true',
                   help='measure held-period bounds for closed trades that lack them')
    args = p.parse_args()
    if args.backfill:
        from state_lock import portfolio_lock, checkpoint
        with portfolio_lock():
            trades = load()
            added, failed = backfill_bounds(trades)
            if added:
                save(trades)
                checkpoint(paths=('closed_trades.json',))
        print(f'[excursions] measured {len(added)}: {", ".join(added) or "-"}')
        for line in failed:
            print(f'::warning::[excursions] not measured — {line}')
    if args.add:
        if args.exit_date is None or args.exit_price is None:
            p.error('--add requires --exit-date and --exit-price for a held-period measurement')
        tk, dt, pr, oc = args.add
        add_trade(tk, dt, pr, oc, note=args.note, exit_date=args.exit_date, exit_price=args.exit_price)
    if args.show or not args.add:
        print(format_excursion_block())
