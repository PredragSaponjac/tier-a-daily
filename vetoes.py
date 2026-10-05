"""Required admission gates. Unknown data blocks admission and is preserved."""
import datetime as dt
import math

import yfinance as yf
import parameters as P
from market_time import next_session


def _result(status, reason, **data):
    return {'pass': status == 'pass', 'status': status, 'reason': reason, **data}


def check_earnings(ticker, entry_date, within_days=None, sector=None):
    """Require a future earnings date; buffer is explicitly CALENDAR days.

    Funds are the exception (review 2026-10-04): an ETF never reports earnings, so "no
    earnings date" is not missing data for it, and Yahoo returns no calendar at all for
    funds (SMH and IBIT both came back as HTTP errors). Without this every ETF candidate
    was blocked forever as 'unknown'. The scanner tags funds with sector 'ETF'.
    """
    if sector == 'ETF':
        return _result('pass', 'ETF/fund: no earnings to report (not applicable)',
                       next_earnings=None, applicable=False)
    within_days = P.earnings_buffer_days() if within_days is None else within_days
    try:
        cal = yf.Ticker(ticker).calendar
        dates = cal.get('Earnings Date') if isinstance(cal, dict) else None
        values = dates if isinstance(dates, (list, tuple)) else [dates]
        normalized = [x.date() if isinstance(x, dt.datetime) else
                      (x if isinstance(x, dt.date) else dt.date.fromisoformat(str(x)[:10]))
                      for x in values if x is not None]
        entry = dt.date.fromisoformat(str(entry_date)[:10])
        future = sorted(x for x in normalized if x >= entry)
        if not future:
            return _result('unknown', 'No current future earnings date; admission blocked', next_earnings=None)
        earliest = future[0]
        delta = (earliest - entry).days
        return _result('fail' if delta <= within_days else 'pass',
                       f'Earnings in {delta} calendar days (buffer {within_days})',
                       next_earnings=earliest.isoformat())
    except Exception as exc:
        return _result('unknown', f'Earnings unavailable ({type(exc).__name__}); admission blocked',
                       next_earnings=None)


def check_liquidity(ticker, min_oi=None):
    """Require valid OI in every requested available expiry; failures are unknown."""
    min_oi = P.min_total_oi() if min_oi is None else min_oi
    try:
        tk = yf.Ticker(ticker)
        expiries = list(tk.options[:3])
        if not expiries:
            return _result('unknown', 'No option expiries available; admission blocked', total_oi=None)
        total = 0.0
        for exp in expiries:
            chain = tk.option_chain(exp)
            for table in (chain.calls, chain.puts):
                if table.empty or 'openInterest' not in table or table.openInterest.isna().any():
                    return _result('unknown', 'Incomplete open-interest data; admission blocked', total_oi=None)
                vals = table.openInterest.astype(float)
                if any(not math.isfinite(v) or v < 0 for v in vals):
                    return _result('unknown', 'Invalid open-interest data; admission blocked', total_oi=None)
                total += float(vals.sum())
        return _result('pass' if total >= min_oi else 'fail',
                       f'Options OI {int(total)} (minimum {min_oi})', total_oi=int(total))
    except Exception as exc:
        return _result('unknown', f'Liquidity unavailable ({type(exc).__name__}); admission blocked', total_oi=None)


def run_vetoes(candidate):
    entry = next_session(candidate['scan_date']).isoformat()
    details = {'earnings': check_earnings(candidate['ticker'], entry, sector=candidate.get('sector')),
               'liquidity': check_liquidity(candidate['ticker'])}
    failed = [x['reason'] for x in details.values() if not x['pass']]
    return {'pass': not failed, 'status': 'unknown' if any(x['status'] == 'unknown' for x in details.values())
            else ('fail' if failed else 'pass'), 'reasons': failed, 'details': details,
            'expected_entry_session': entry}
