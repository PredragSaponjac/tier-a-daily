"""Completed-session paper execution shared by monitoring and research.

Daily OHLC cannot recover intraday ordering or broker fills. Dual touches use a
flagged stop-first assumption; opening gaps use a paper opening fill. Unresolved
paths are right-censored, never converted into an expiry sale.
"""
import math

EXECUTION_METHOD = 'completed_daily_next_open_v1'


def completed_daily_bars(frame, asof=None):
    """Discard unfinished/non-session bars using the exchange calendar."""
    from market_time import session_completed
    eligible = [session_completed(ix.date(), now=asof) for ix in frame.index]
    return frame.loc[eligible].copy()


def completed_history(frame, start, asof=None):
    """Require a complete ordered raw/action daily feed before resolving a path.

    Missing sessions can hide the first barrier touch. Missing action columns
    cannot establish a common price basis. An incomplete current session may
    supply a split action but is never included in the returned execution bars.
    """
    from market_time import now_eastern, last_completed_session, sessions_between
    clock = now_eastern(asof)
    expected = sessions_between(start, last_completed_session(clock))
    if frame is None or frame.empty:
        if expected:
            raise ValueError('Completed-session feed is empty')
        return frame
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError('Daily feed dates are unordered or duplicated')
    if any(ix.date() < start or ix.date() > clock.date() for ix in frame.index):
        raise ValueError('Daily feed contains dates outside the requested interval')
    required = {'Open', 'High', 'Low', 'Close', 'Stock Splits', 'Dividends'}
    if not required.issubset(frame.columns):
        raise ValueError('Daily feed lacks OHLC or corporate actions')
    completed = completed_daily_bars(frame, clock)
    if [ix.date() for ix in completed.index] != expected:
        raise ValueError(f'Completed-session feed is incomplete; expected {start} through {expected[-1]}')
    for _, row in frame.iterrows():
        split, dividend = float(row['Stock Splits']), float(row['Dividends'])
        if not math.isfinite(split) or not math.isfinite(dividend) or split < 0:
            raise ValueError('Invalid corporate action')
    for _, row in completed.iterrows():
        opn, high, low, close = (float(row[k]) for k in ('Open', 'High', 'Low', 'Close'))
        if (any(not math.isfinite(v) or v <= 0 for v in (opn, high, low, close)) or
                low > min(opn, close) or high < max(opn, close) or low > high):
            raise ValueError('Invalid daily OHLC')
    return completed


def split_factor(frame, after_date):
    """Cumulative recorded splits after a frozen fill's date (dividends excluded)."""
    if not {'Stock Splits', 'Dividends'}.issubset(frame.columns):
        raise ValueError('Corporate-action columns missing; split basis cannot be established')
    factor = 1.0
    for ix, row in frame.iterrows():
        split = float(row['Stock Splits'])
        if not math.isfinite(split) or split < 0:
            raise ValueError('Invalid split factor')
        if split and ix.date() > after_date:
            factor *= split
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError('Invalid cumulative split factor')
    return factor


def _ge(a, b):
    return a >= b or math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-10)


def resolve_bar(opn, high, low, T1, STOP):
    """Return (reason, assumed fill, assumption note), or None for no exit."""
    if any(not math.isfinite(float(v)) or float(v) <= 0 for v in (opn, high, low, T1, STOP)):
        raise ValueError('OHLC and exit levels must be finite and positive')
    if low > high or opn < low or opn > high or STOP >= T1:
        raise ValueError('invalid OHLC or exit levels')
    if _ge(STOP, opn):
        return 'STOP', opn, f'gapped through stop: paper opening fill {opn:.2f}'
    if _ge(opn, T1):
        return 'TP1', T1, f'gapped through target: conservative paper fill {T1:.2f}'
    target, stop = _ge(high, T1), _ge(STOP, low)
    if target and stop:
        return 'STOP', STOP, 'AMBIGUOUS daily bar; assumed STOP first (conservative)'
    if target:
        return 'TP1', T1, ''
    if stop:
        return 'STOP', STOP, ''
    return None


def held_extreme_bounds(opn, high, low, res, T1=None, STOP=None):
    """Bounds on held low/high under assumed fills, not recovered timestamps."""
    if res is None:
        return {'low_min': low, 'low_max': low, 'high_min': high, 'high_max': high,
                'exact': True, 'note': ''}
    reason, px, note = res
    if note.startswith('gapped'):
        return {'low_min': opn, 'low_max': opn, 'high_min': opn, 'high_max': opn,
                'exact': True, 'note': 'opening mark; assumed opening exit'}
    if reason == 'STOP':
        upper = min(high, T1) if T1 is not None else high
        lower = max(opn, px)
        return {'low_min': px, 'low_max': px, 'high_min': lower, 'high_max': upper,
                'exact': math.isclose(lower, upper), 'note': 'favorable extreme timing unknown'}
    lower, upper = low, min(opn, px)
    return {'low_min': lower, 'low_max': upper, 'high_min': px, 'high_max': px,
            'exact': math.isclose(lower, upper), 'note': 'adverse extreme timing unknown'}


def held_extremes(opn, high, low, res):
    """Compatibility view of outer bounds; these are not necessarily exact extrema."""
    bounds = held_extreme_bounds(opn, high, low, res)
    return bounds['low_min'], bounds['high_max']


def walk_bars(frame, entry, tp_pct=10.0, stop_pct=-7.0):
    """Walk supplied completed bars; no timeout. OPEN returns no realized P&L."""
    if not math.isfinite(entry) or entry <= 0:
        raise ValueError('entry must be positive and finite')
    if not all(math.isfinite(v) for v in (tp_pct, stop_pct)) or tp_pct <= 0 or not -100 < stop_pct < 0:
        raise ValueError('target must be finite/positive and stop between -100 and 0')
    target, stop = entry * (1 + tp_pct / 100), entry * (1 + stop_pct / 100)
    agg = {'low_min': entry, 'low_max': entry, 'high_min': entry, 'high_max': entry}
    outcome, fill, hit_date, note, hit_day = 'OPEN', None, None, '', None
    mae_date = mfe_date = None
    for i, (index, row) in enumerate(frame.iterrows(), 1):
        opn, high, low = (float(row[k]) for k in ('Open', 'High', 'Low'))
        res = resolve_bar(opn, high, low, target, stop)
        b = held_extreme_bounds(opn, high, low, res, target, stop)
        if b['low_min'] < agg['low_min']:
            mae_date = index.date().isoformat()
        if b['high_max'] > agg['high_max']:
            mfe_date = index.date().isoformat()
        for key in ('low_min', 'low_max'):
            agg[key] = min(agg[key], b[key])
        for key in ('high_min', 'high_max'):
            agg[key] = max(agg[key], b[key])
        if res:
            outcome, fill, note = res
            hit_date, hit_day = index.date().isoformat(), i
            break
    pct = lambda x: (x / entry - 1) * 100
    bounds = {'mae_pct_min': pct(agg['low_min']), 'mae_pct_max': pct(agg['low_max']),
              'mfe_pct_min': pct(agg['high_min']), 'mfe_pct_max': pct(agg['high_max']),
              'exact': math.isclose(agg['low_min'], agg['low_max']) and math.isclose(agg['high_min'], agg['high_max']),
              'method': EXECUTION_METHOD}
    return {'outcome': outcome, 'exit_price': fill, 'exit_date': hit_date,
            'days_in': hit_day if hit_day else len(frame), 'exit_note': note,
            'complete': outcome != 'OPEN', 'return_pct': pct(fill) if fill is not None else None,
            'mark_return_pct': pct(float(frame.iloc[-1]['Close'])) if len(frame) else None,
            'mae_pct': bounds['mae_pct_min'], 'mfe_pct': bounds['mfe_pct_max'],
            'mae_date': mae_date, 'mfe_date': mfe_date, 'excursion_bounds': bounds,
            'execution_method': EXECUTION_METHOD}
