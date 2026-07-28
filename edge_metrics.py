"""Edge-validation metrics — RESEARCH LOGGING ONLY, never used in selection.

Source: 2026-07-27 edge hunt over all 102 historical full-Tier-A candidates
(867 features tested, adversarially verified, Bonferroni-surviving):

  CONFIRMED  sector_iv_rank >= 60          big20 61% vs 14%, loss10 2% vs 29% (p=1.2e-8)
  CONFIRMED  + skew_slope <= -1.6 (combo)  big20 77% vs 20%, loss10 0% vs 22% (p=1.8e-6)
  HYPOTHESIS iv_hv_ratio >= 1.1            disaster rate 8% vs 23% (all 3 real losers < 1.0)

Pre-committed protocol (user-approved 2026-07-27): log these on every new signal,
change NOTHING in selection, interim scorecard at 10 fresh Tier A signals
(wire into rules then ONLY if overwhelming: Fisher p<0.05 on fresh data alone),
formal decision at 15-20. Scorecard: python edge_validation.py

NOTE: sector_iv_rank == 0.0 is missing-coded in the tracker — treated as None here.
"""
import datetime as dt
import sqlite3

import numpy as np

from scanner_reader import SKEW_DB

# thresholds from the verified findings — do not tune these without a new study
SECTOR_IV_RANK_MIN = 60.0
SKEW_SLOPE_MAX = -1.6
IV_HV_RATIO_MIN = 1.1


def skew_slope(ticker: str, scan_date: str, db_path=None,
               n: int = 6, max_gap_days: int = 14):
    """Linear slope of structural skew over the last <=6 readings (gap-guarded).
    Negative = skew still collapsing into entry. Same construction as the study."""
    try:
        con = sqlite3.connect(str(db_path or SKEW_DB))
        rows = con.execute(
            'SELECT date, skew FROM skew_daily WHERE ticker=? AND date<=? '
            'ORDER BY date DESC LIMIT ?', (ticker, scan_date, n)).fetchall()
        con.close()
    except Exception:
        return None
    sd = dt.date.fromisoformat(str(scan_date)[:10])
    pts = sorted((d, s) for d, s in rows if s is not None
                 and (sd - dt.date.fromisoformat(str(d)[:10])).days <= max_gap_days)
    if len(pts) < 3:
        return None
    y = [s for _, s in pts]
    return float(np.polyfit(range(len(y)), y, 1)[0])


def edge_metrics(c: dict, db_path=None) -> dict:
    """Compute the three validation metrics for a candidate dict (read-only)."""
    sr = c.get('sector_iv_rank')
    sr = None if sr in (None, 0, 0.0) else float(sr)  # 0.0 = missing-coded
    ihr = c.get('iv_hv_ratio')
    ihr = None if ihr in (None, 0, 0.0) else float(ihr)
    ss = skew_slope(c.get('ticker'), c.get('scan_date'), db_path)
    return {
        'sector_iv_rank': sr,
        'skew_slope': None if ss is None else round(ss, 2),
        'iv_hv_ratio': None if ihr is None else round(ihr, 2),
        'combo_pass': bool(sr is not None and ss is not None
                           and sr >= SECTOR_IV_RANK_MIN and ss <= SKEW_SLOPE_MAX),
        'ivr_pass': bool(ihr is not None and ihr >= IV_HV_RATIO_MIN),
    }
