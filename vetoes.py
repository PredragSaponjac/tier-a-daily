"""Vetoes — reasons to skip an otherwise-strong signal.

- earnings_within_14d: hard block if earnings reported within 14 days of entry
- liquidity_floor: minimum options OI + reasonable bid-ask spread on near-term ATM
- (optional, Phase 2) news scan
"""
import datetime as dt
import yfinance as yf
import parameters as P


def check_earnings(ticker: str, entry_date: str, within_days: int = None) -> dict:
    """Return {'pass': bool, 'reason': str, 'next_earnings': date_str or None}.

    Block if earnings reported within `within_days` trading days of entry.
    Default within_days from parameters.json (vetoes.earnings_within_days).
    """
    if within_days is None:
        within_days = P.earnings_buffer_days()
    try:
        tk = yf.Ticker(ticker)
        cal = tk.calendar
    except Exception as e:
        return {'pass': True, 'reason': f'(earnings check failed: {e})', 'next_earnings': None}

    next_eps = None
    if cal:
        if isinstance(cal, dict):
            # yfinance returns dict with 'Earnings Date' -> list of date(s)
            eps_dates = cal.get('Earnings Date')
            if eps_dates:
                if isinstance(eps_dates, list) and len(eps_dates) > 0:
                    next_eps = eps_dates[0]
                else:
                    next_eps = eps_dates
    if next_eps is None:
        return {'pass': True, 'reason': 'no earnings date found', 'next_earnings': None}

    # Normalize to date
    try:
        if hasattr(next_eps, 'date'):
            next_eps_d = next_eps.date()
        else:
            next_eps_d = next_eps
    except Exception:
        return {'pass': True, 'reason': 'could not parse earnings date', 'next_earnings': str(next_eps)}

    entry_d = dt.datetime.strptime(entry_date, '%Y-%m-%d').date()
    days_to_eps = (next_eps_d - entry_d).days
    if days_to_eps < 0:
        # Past earnings (already reported) — no upcoming risk from this date
        return {'pass': True,
                'reason': f'last earnings {-days_to_eps}d ago (no upcoming reported)',
                'next_earnings': None}
    if 0 <= days_to_eps <= within_days:
        return {'pass': False,
                'reason': f'EARNINGS RISK: reports in {days_to_eps}d ({next_eps_d.isoformat()})',
                'next_earnings': next_eps_d.isoformat()}
    return {'pass': True,
            'reason': f'next earnings in {days_to_eps}d (safe)',
            'next_earnings': next_eps_d.isoformat() if next_eps_d else None}


def check_liquidity(ticker: str, min_oi: int = None) -> dict:
    """Coarse liquidity check: total options OI across next 3 expiries.

    Returns {'pass': bool, 'reason': str, 'total_oi': int}.
    Default min_oi from parameters.json (vetoes.min_total_oi).
    """
    if min_oi is None:
        min_oi = P.min_total_oi()
    try:
        tk = yf.Ticker(ticker)
        expiries = tk.options[:3]  # check first 3 expiries
    except Exception as e:
        return {'pass': True, 'reason': f'(liquidity check failed: {e})', 'total_oi': None}

    total_oi = 0
    for exp in expiries:
        try:
            chain = tk.option_chain(exp)
            total_oi += int(chain.calls['openInterest'].sum())
            total_oi += int(chain.puts['openInterest'].sum())
        except Exception:
            continue

    if total_oi < min_oi:
        return {'pass': False,
                'reason': f'LIQUIDITY: total OI across next 3 expiries = {total_oi} < {min_oi}',
                'total_oi': total_oi}
    return {'pass': True, 'reason': f'OI={total_oi} (ok)', 'total_oi': total_oi}


def run_vetoes(candidate: dict) -> dict:
    """Run all vetoes. Return {'pass': bool, 'reasons': [str], 'details': {}}."""
    tk = candidate['ticker']
    sd = candidate['scan_date']
    details = {
        'earnings': check_earnings(tk, sd),
        'liquidity': check_liquidity(tk),
    }
    failed = [k for k, v in details.items() if not v['pass']]
    reasons = [details[k]['reason'] for k in failed]
    return {'pass': len(failed) == 0, 'reasons': reasons, 'details': details}


if __name__ == '__main__':
    import argparse
    from scanner_reader import read_tier_a
    p = argparse.ArgumentParser()
    p.add_argument('--scan-date', default=None)
    args = p.parse_args()
    cands, sd = read_tier_a(args.scan_date)
    print(f'Checking vetoes for {len(cands)} Tier A candidates on {sd}...\n')
    for c in cands:
        v = run_vetoes(c)
        status = '✅ PASS' if v['pass'] else '❌ VETO'
        print(f"  {c['ticker']:6s} {status}  earnings={v['details']['earnings']['reason']}  liq={v['details']['liquidity']['reason']}")
