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

# --- stop-width diagnostic (2026-07-31 study) -------------------------------
# The live stop is a FIXED -7%. Measured against ATR(14) it sits a median of only
# 0.77 ATR away on Tier A candidates (89.6% inside 1.5 ATR, p10 = 0.48) — i.e.
# INSIDE normal daily noise, which is why ~40% of trades stop out. Widening to
# ~2x ATR cut stop-outs 41%->25% and tripled RAW expectancy (+3.0%->+8.6%), but
# RISK-ADJUSTED (R) it was a wash (+0.434 -> +0.470, CIs overlapping) and it was
# WORSE on the wide sample. So: NOT changing the stop — logging the diagnostic
# and deciding prospectively, same protocol as the edge metrics.
LIVE_STOP_PCT = 7.0
STOP_ATR_TIGHT = 1.0   # below this the stop is inside a single daily range


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


def atr_profile(ticker: str, scan_date: str, stop_pct: float = LIVE_STOP_PCT, n: int = 14):
    """ATR(14)% as of scan_date + how many ATRs the FIXED live stop sits away.

    Backward-looking only (bars up to and including scan_date). Research logging:
    stop_atr < 1.0 means the -7% stop is inside one average daily range, so a normal
    day's noise can take the trade out before the thesis resolves.
    """
    try:
        import yfinance as yf
        end = dt.date.fromisoformat(str(scan_date)[:10]) + dt.timedelta(days=1)
        start = end - dt.timedelta(days=60)
        df = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat(),
                                       interval='1d', auto_adjust=True)
        if len(df) < n + 1:
            return None
        h = df['High'].to_numpy(dtype=float)
        low = df['Low'].to_numpy(dtype=float)
        cl = df['Close'].to_numpy(dtype=float)
        pc = np.roll(cl, 1)
        pc[0] = cl[0]
        tr = np.maximum(h - low, np.maximum(np.abs(h - pc), np.abs(low - pc)))
        atr = float(np.mean(tr[-n:]))
        last = float(cl[-1])
        if not np.isfinite(atr) or atr <= 0 or last <= 0:
            return None
        atr_pct = atr / last * 100.0
        return {
            'atr_pct': round(atr_pct, 2),
            'stop_atr': round(stop_pct / atr_pct, 2),
            'tight_stop': bool(stop_pct / atr_pct < STOP_ATR_TIGHT),
            'atr2x_stop_pct': round(2.0 * atr_pct, 1),   # what a 2x-ATR stop would be
        }
    except Exception:
        return None


def edge_metrics(c: dict, db_path=None) -> dict:
    """Compute the three validation metrics for a candidate dict (read-only)."""
    sr = c.get('sector_iv_rank')
    sr = None if sr in (None, 0, 0.0) else float(sr)  # 0.0 = missing-coded
    ihr = c.get('iv_hv_ratio')
    ihr = None if ihr in (None, 0, 0.0) else float(ihr)
    ss = skew_slope(c.get('ticker'), c.get('scan_date'), db_path)
    atr = atr_profile(c.get('ticker'), c.get('scan_date'))
    return {
        'sector_iv_rank': sr,
        'skew_slope': None if ss is None else round(ss, 2),
        'iv_hv_ratio': None if ihr is None else round(ihr, 2),
        'combo_pass': bool(sr is not None and ss is not None
                           and sr >= SECTOR_IV_RANK_MIN and ss <= SKEW_SLOPE_MAX),
        'ivr_pass': bool(ihr is not None and ihr >= IV_HV_RATIO_MIN),
        # stop-width diagnostic — logged only, the live stop stays -7%
        'atr_pct': None if not atr else atr['atr_pct'],
        'stop_atr': None if not atr else atr['stop_atr'],
        'tight_stop': None if not atr else atr['tight_stop'],
        'atr2x_stop_pct': None if not atr else atr['atr2x_stop_pct'],
    }
