"""Data-quality gate — reject signals built on a NOISY / thin options chain.

The SMMT 6/9 case: it PASSED the leg gate (cushion 3.4x) but its structural skew
was oscillating wildly day-to-day (+18 / −5 / +63 / +30 / −46 / +17 / +48 / +37 /
+18) — a thin, illiquid chain whose skew & IV readings can't be trusted. The
"cushion 3.4x" was riding a single low IV print; at the IV it printed days earlier
it was <2x. Garbage in.

Metric: standard deviation of STRUCTURAL skew over the last `lookback` trading days.
Validated across all 8 trades — every REAL trade (wins AND losses) had skew_std
≤ 12; the SMMT-noise case = 30. Threshold 20 cleanly separates with margin both ways.

A noisy candidate is NOT tradeable, regardless of how good its legs look — because
the legs themselves are computed off untrustworthy data.
"""
import datetime as _dt
import sqlite3
import statistics as st


def assess_noise(ticker: str, scan_date: str, db_path: str = 'skew_history.db',
                 lookback: int = 6, std_threshold: float = 20.0,
                 max_gap_days: int = 14) -> dict:
    """Return {skew_std, skew_range, noisy, reason} for a ticker as of scan_date."""
    try:
        con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
        rows = con.execute(
            'SELECT date, skew FROM skew_daily WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT ?',
            (ticker, scan_date, lookback)).fetchall()
        con.close()
    except Exception as e:
        return {'skew_std': None, 'skew_range': None, 'noisy': False,
                'reason': f'noise check skipped ({e})'}

    # RECENCY GUARD (added 2026-07-20). The lookback takes the last N ROWS, not the
    # last N calendar days — so a collection gap (the scanner was dead 6/27-7/17)
    # would silently mix today's skew with weeks-old skew and produce a meaningless
    # std. Only trust rows within max_gap_days of scan_date.
    try:
        _sd = _dt.date.fromisoformat(str(scan_date)[:10])
        recent = [r for r in rows if r['skew'] is not None
                  and (_sd - _dt.date.fromisoformat(str(r['date'])[:10])).days <= max_gap_days]
    except Exception:
        recent = [r for r in rows if r['skew'] is not None]

    sk = [r['skew'] for r in recent]
    if len(sk) < 3:
        return {'skew_std': None, 'skew_range': None, 'noisy': False,
                'reason': (f'⚠️ noise gate UNAVAILABLE — only {len(sk)} day(s) of history '
                           f'within {max_gap_days}d (collection gap). Chain quality NOT verified.')}

    sstd = st.pstdev(sk)
    srng = max(sk) - min(sk)
    noisy = sstd > std_threshold
    reason = (f"NOISY chain: skew std {sstd:.0f} > {std_threshold:.0f} "
              f"(range {srng:.0f}) — signal unreliable, NO TRADE"
              if noisy else f"chain ok (skew std {sstd:.0f})")
    return {'skew_std': sstd, 'skew_range': srng, 'noisy': noisy, 'reason': reason}
