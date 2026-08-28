# -*- coding: utf-8 -*-
"""Write a small, git-committable marker recording that a scan actually landed.

WHY THIS EXISTS
skew_history.db moved OUT of git on 2026-08-27 (it crossed GitHub's 100MB limit, so
every push was rejected while the workflows still reported success — three days of
scans were computed and thrown away). But two things grep the git log for
"Skew tracker snapshot" commits:

  * heartbeat.yml  — alarms if no such commit in 72h
  * skew_pm guard  — skips a duplicate PM run if today's already exists

With the DB gone from git and *.log gitignored, the snapshot commit would stage
NOTHING, so those commits would stop — the heartbeat would alarm every day on a
perfectly healthy scanner, and the dupe guard would never fire.

This marker is tiny (a few hundred bytes), commits cleanly, keeps both mechanisms
working, and doubles as a health record: if the row counts stop rising, the scan is
running but not writing.

Usage: python scan_marker.py AM|PM
"""
import datetime as dt
import json
import os
import sqlite3
import sys

DB = os.environ.get('SKEW_DB_PATH', 'skew_history.db')


def main():
    tag = (sys.argv[1] if len(sys.argv) > 1 else 'PM').upper()
    out = f'last_scan_{tag.lower()}.json'
    rec = {'scan': tag, 'utc': dt.datetime.utcnow().isoformat() + 'Z'}
    try:
        con = sqlite3.connect(DB)
        q = lambda s: con.execute(s).fetchone()[0]
        rec.update({
            'latest_scan_date': q('SELECT MAX(scan_date) FROM candidate_log'),
            'candidate_log_rows': q('SELECT COUNT(*) FROM candidate_log'),
            'skew_daily_rows': q('SELECT COUNT(*) FROM skew_daily'),
            'fixed_strike_vol_rows': q('SELECT COUNT(*) FROM fixed_strike_vol'),
            'db_bytes': os.path.getsize(DB),
        })
        con.close()
    except Exception as e:                       # never block the commit on this
        rec['error'] = f'{type(e).__name__}: {e}'
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(rec, fh, indent=2)
    print(f'[marker] wrote {out}: {rec.get("latest_scan_date")} '
          f'({rec.get("candidate_log_rows")} candidate rows, '
          f'{round(rec.get("db_bytes", 0) / 1048576, 1)} MB)')


if __name__ == '__main__':
    main()
