# -*- coding: utf-8 -*-
"""Daily data-integrity check — proves a scan actually LANDED, not just that the
workflow went green. Usage:  python verify_daily.py [YYYY-MM-DD]

A green workflow is not evidence: the run can succeed while writing nothing, writing
partial rows, or silently zero-coding a column (that is exactly how sector_iv_rank
died unnoticed for two months, 5/28 -> 7/27). This checks the DATA.

Exit 0 = all checks pass. Exit 1 = something needs a look.
"""
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
DB = REPO / 'skew_history.db'

# expected universe size (666 tickers scanned daily); allow slack for delistings/failures
MIN_ROWS = 600
MIN_SECTOR_RANK = 400      # ~562 typical; 0 means the 7/28 fix regressed
MIN_SKEW_CHG = 600         # skew_change_5d must be computed post-warmup


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    if not DB.exists():
        print(f'FAIL {date}: skew_history.db missing')
        return 1

    con = sqlite3.connect(str(DB))
    q = lambda s, p=(): con.execute(s, p).fetchone()[0]
    problems, notes = [], []

    sd = q('SELECT COUNT(*) FROM skew_daily WHERE date=?', (date,))
    cl = q('SELECT COUNT(*) FROM candidate_log WHERE scan_date=?', (date,))
    fsv = q('SELECT COUNT(*) FROM fixed_strike_vol WHERE date=?', (date,))
    sir = q('SELECT COUNT(*) FROM candidate_log WHERE scan_date=? AND '
            'sector_iv_rank IS NOT NULL AND sector_iv_rank!=0.0', (date,))
    skc = q('SELECT COUNT(*) FROM candidate_log WHERE scan_date=? AND '
            'skew_change_5d IS NOT NULL', (date,))
    pwc = q('SELECT COUNT(*) FROM candidate_log WHERE scan_date=? AND '
            'put_wall_oi_change IS NOT NULL', (date,))

    if sd == 0 and cl == 0:
        print(f'FAIL {date}: NO DATA AT ALL — scan did not land')
        return 1
    if sd < MIN_ROWS:
        problems.append(f'skew_daily only {sd} rows (<{MIN_ROWS})')
    if cl < MIN_ROWS:
        problems.append(f'candidate_log only {cl} rows (<{MIN_ROWS})')
    if fsv < 1000:
        problems.append(f'fixed_strike_vol only {fsv} rows')
    if sir < MIN_SECTOR_RANK:
        problems.append(f'sector_iv_rank populated on only {sir} rows '
                        f'(<{MIN_SECTOR_RANK}) — the 5/28 regression may be back')
    if skc < MIN_SKEW_CHG:
        problems.append(f'skew_change_5d computed on only {skc} rows')
    if pwc < MIN_SKEW_CHG:
        notes.append(f'put_wall_oi_change on {pwc} rows')

    # Tier A funnel (informational — 0 is a normal, valid outcome)
    tier = q("""SELECT COUNT(*) FROM candidate_log WHERE scan_date=?
                AND current_signal='BULLISH_REVERSAL' AND near_dte<=6
                AND skew_change_5d<=-7 AND near_skew<=-7 AND spot_return_pct<=-8
                AND put_wall_oi_change IS NOT NULL AND put_wall_oi_change<=0""", (date,))
    washouts = q('SELECT COUNT(*) FROM candidate_log WHERE scan_date=? AND spot_return_pct<=-8', (date,))

    # PM only: the daily archive must exist and be committed
    arch = REPO / 'signals' / f'{date}.json'
    arch_ok = arch.exists()
    edge_logged = None
    if arch_ok:
        try:
            a = json.loads(arch.read_text())
            cands = a.get('candidates', [])
            edge_logged = sum(1 for c in cands if c.get('edge')) if cands else 0
        except Exception as e:
            problems.append(f'archive unreadable: {e}')

    con.close()
    status = 'FAIL' if problems else 'OK'
    print(f'{status} {date}: skew_daily={sd} candidate_log={cl} fsv={fsv} '
          f'sector_iv_rank={sir} skewD5={skc} | washouts={washouts} tierA={tier} '
          f'| archive={"yes" if arch_ok else "NO"}'
          + (f' edge_logged={edge_logged}' if edge_logged is not None else ''))
    for p in problems:
        print(f'   PROBLEM: {p}')
    for n in notes:
        print(f'   note: {n}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
