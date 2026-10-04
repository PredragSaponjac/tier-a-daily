"""Independent target shadows using the shared completed-session paper engine.

The baseline outcome always uses TP1; smaller/larger targets are separate shadow
strategies. A finite observation window is right-censored, never an expiry sale.
"""
import datetime as dt
from exits import completed_history, walk_bars, split_factor
from market_time import now_eastern, next_session

TP1, TP2, TP3, CONSERV, STOP = 10.0, 11.0, 20.0, 7.5, -7.0
WINDOW_DAYS = None


def model_exits(ticker, entry_date, entry_price, window_days=None, asof=None, entry_policy='legacy_close'):
    import pandas as pd
    import yfinance as yf
    entry_date = dt.date.fromisoformat(entry_date[:10])
    clock, start = now_eastern(asof), next_session(entry_date)
    end = clock.date() + dt.timedelta(days=1)
    frame = yf.download(ticker, start=start, end=end,
                        interval='1d', auto_adjust=False, actions=True, progress=False)
    if frame is not None and isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [c[0] for c in frame.columns]
    completed = completed_history(frame, start, clock)
    if completed is None or completed.empty:
        return None
    factor = split_factor(frame, entry_date)
    frame = completed
    if window_days is not None:
        if not isinstance(window_days, int) or window_days < 1:
            raise ValueError('window_days must be a positive calendar observation window')
        frame = frame.loc[[ix.date() < entry_date + dt.timedelta(days=window_days) for ix in frame.index]]
    entry = float(completed.iloc[0]['Open']) if entry_policy == 'next_regular_open' else entry_price / factor
    shadows = {label: walk_bars(frame, entry, pct, STOP)
               for label, pct in {'conserv': CONSERV, 'tp1': TP1, 'tp2': TP2, 'tp3': TP3}.items()}
    base = shadows['tp1']
    days = {f'{label}_day': r['days_in'] if r['outcome'] == 'TP1' else None
            for label, r in shadows.items()}
    return {**days, 'stop_day': base['days_in'] if base['outcome'] == 'STOP' else None,
            'also_reached': 'TP3 +20%' if days['tp3_day'] else 'TP2 +11%' if days['tp2_day'] else '—',
            'outcome': {'TP1': 'WIN', 'STOP': 'LOSS', 'OPEN': 'OPEN'}[base['outcome']],
            'complete': base['complete'], 'return_pct': base['return_pct'],
            'exit_price': base['exit_price'], 'exit_date': base['exit_date'],
            'excursion_bounds': base['excursion_bounds'], 'exit_note': base['exit_note'],
            'shadows': shadows, 'model_policy': base['execution_method'],
            'entry_price': entry, 'entry_policy': entry_policy,
            'actual_entry_date': completed.index[0].date().isoformat() if entry_policy == 'next_regular_open' else entry_date.isoformat(),
            'price_basis': 'split_normalized_price_only'}


if __name__ == '__main__':
    import sys
    print(model_exits(sys.argv[1], sys.argv[2], float(sys.argv[3])))
