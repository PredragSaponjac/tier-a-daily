# -*- coding: utf-8 -*-
"""Prospective tracker for hypotheses registered AFTER the frozen inference-v1 cohort began.

Why a separate file: research-v1-2026-10-05 fingerprints hypotheses.json (its 28 planned
tests), the engine files and the single 2027-07-02 look. Adding a hypothesis there now would
invalidate that lock mid-cohort. New ideas live in prospective_hypotheses.json and count only
on qualifiers that arrive after their registration date.

Each weekly run (after self_audit.py in weekly_audit.yml):
  1. for every Tier A qualifier in tier_a_paths, compute the registered features ONCE from
     skew_daily and freeze them in prospective_ledger.csv (rows are never removed; only the
     outcome columns refresh as paths resolve; a row whose inputs were missing is retried);
  2. split rows into pre_registration (context only) and prospective (the evidence);
  3. write counts and milestones into the registry's "status" block and print a report;
  4. with --send, post a short Telegram line when new prospective qualifiers appeared.
It never reads or writes positions, parameters, hypotheses.json or anything selection uses.

Run: python prospective_tracker.py [--db PATH] [--dry-run] [--send]
"""
import argparse
import csv
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

import market_time as MT

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / 'prospective_hypotheses.json'
LEDGER = HERE / 'prospective_ledger.csv'
WINDOW = 9                      # Tier A's screen window, in exchange sessions
FEATURE_VERSION = 1
COLUMNS = ['ticker', 'scan_date', 'cohort', 'data_ok', 'd0', 'wall_d0', 'wall_now', 'p1_wall_held',
           'oi_d0', 'oi_now', 'put_iv_d0', 'put_iv_now', 'p2_put_sale_footprint', 'feature_version',
           'computed_on', 'outcome', 'hit_t1', 'r_live', 'outcome_asof']


def default_db():
    return Path(os.environ.get('SKEW_DB_PATH', str(HERE / 'skew_history.db')))


def session_before(scan_date, n=WINDOW):
    """The exchange session exactly n sessions before scan_date; None if scan_date is not a session."""
    span = MT.sessions_between(scan_date - dt.timedelta(days=n * 2 + 14), scan_date)
    if len(span) < n + 1 or span[-1] != scan_date:
        return None
    return span[-(n + 1)]


def _positive(v):
    return v is not None and v == v and v > 0


def _row(con, ticker, day):
    r = con.execute('SELECT put_wall_strike, put_wall_oi, put_25d_iv FROM skew_daily WHERE ticker=? AND date=?',
                    (ticker, day.isoformat())).fetchone()
    return None if r is None else {'wall': r[0], 'oi': r[1], 'put_iv': r[2]}


def compute_features(con, ticker, scan_date):
    """Registered feature definitions, version 1. Blank means 'could not be read', never 0."""
    d0 = session_before(scan_date)
    out = {'d0': d0.isoformat() if d0 else '', 'data_ok': 0, 'wall_d0': '', 'wall_now': '', 'p1_wall_held': '',
           'oi_d0': '', 'oi_now': '', 'put_iv_d0': '', 'put_iv_now': '', 'p2_put_sale_footprint': ''}
    if d0 is None:
        return out
    a, b = _row(con, ticker, d0), _row(con, ticker, scan_date)
    if a is None or b is None:
        return out
    out.update(data_ok=1, wall_d0=a['wall'], wall_now=b['wall'], oi_d0=a['oi'], oi_now=b['oi'],
               put_iv_d0=a['put_iv'], put_iv_now=b['put_iv'])
    if _positive(a['wall']) and _positive(b['wall']):
        out['p1_wall_held'] = int(b['wall'] >= a['wall'])
    if a['oi'] is not None and b['oi'] is not None and _positive(a['put_iv']) and _positive(b['put_iv']):
        out['p2_put_sale_footprint'] = int(b['oi'] > a['oi'] and b['put_iv'] < a['put_iv'])
    return out


def load_ledger(path):
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(newline='', encoding='utf-8') as fh:
        return {(r['ticker'], r['scan_date']): dict(r) for r in csv.DictReader(fh)}


def save_ledger(path, ledger):
    rows = sorted(ledger.values(), key=lambda r: (r['scan_date'], r['ticker']))
    tmp = Path(str(path) + '.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator='\n')
        w.writeheader()
        for r in rows:
            w.writerow({k: ('' if r.get(k) is None else r.get(k)) for k in COLUMNS})
    os.replace(tmp, path)


def _flag(v):
    return None if v in ('', None) else int(float(v))


def _block(rows, feature, show_rates):
    resolved = [r for r in rows if _flag(r.get('hit_t1')) is not None]
    yes = [r for r in resolved if _flag(r[feature]) == 1]
    no = [r for r in resolved if _flag(r[feature]) == 0]
    b = {'n_total': len(rows), 'n_yes_total': sum(_flag(r[feature]) == 1 for r in rows),
         'n_resolved': len(resolved), 'n_yes_resolved': len(yes), 'n_no_resolved': len(no)}
    if show_rates and yes and no:
        b['hit_yes'] = round(sum(_flag(r['hit_t1']) for r in yes) / len(yes), 3)
        b['hit_no'] = round(sum(_flag(r['hit_t1']) for r in no) / len(no), 3)
        b['diff_pts'] = round((b['hit_yes'] - b['hit_no']) * 100, 1)
    return b


def summarize(registry, ledger):
    plan = registry['read_plan']
    looks, decision = plan['interim_descriptive_looks_at_n_resolved'], plan['decision_read_at_n_resolved']
    out = {}
    for h in registry['hypotheses']:
        f = h['feature']
        rows = [r for r in ledger.values() if str(r.get('data_ok')) == '1' and _flag(r.get(f)) is not None]
        pro = [r for r in rows if r['scan_date'] >= h['registered']]
        pre = [r for r in rows if r['scan_date'] < h['registered']]
        n_res = sum(_flag(r.get('hit_t1')) is not None for r in pro)
        show = n_res >= looks[0]
        out[h['id']] = {'prospective': _block(pro, f, show), 'pre_registration_context': _block(pre, f, True),
                        'rates_visible': show, 'next_milestone_n_resolved': next((m for m in looks + [decision] if n_res < m), None),
                        'decision_read_at_n_resolved': decision,
                        'verdict': 'tracking; no verdict is possible before the decision read'}
    return out


def report(registry, summary, new_rows):
    lines = [f"🔭 Prospective hypotheses ({registry['registry_version']}; outside inference v1; tracking only, never implements)"]
    for h in registry['hypotheses']:
        s = summary[h['id']]
        p = s['prospective']
        line = (f"• {h['id']}: prospective {p['n_total']} qualifiers ({p['n_yes_total']} with the feature), "
                f"{p['n_resolved']} resolved")
        if 'diff_pts' in p:
            line += f" — descriptive: {p['hit_yes']:.0%} vs {p['hit_no']:.0%} ({p['diff_pts']:+.0f} pts)"
        else:
            line += f" — rates hidden until {s['next_milestone_n_resolved']} resolved"
        line += f"; decision read at {s['decision_read_at_n_resolved']}."
        lines.append(line)
    lines.append(f"New prospective qualifiers this run: {new_rows}.")
    context = []
    for h in registry['hypotheses']:
        c = summary[h['id']]['pre_registration_context']
        if 'diff_pts' in c:
            context.append(f"{h['feature']}: yes {c['n_yes_resolved']} → {c['hit_yes']:.0%}, no {c['n_no_resolved']} → {c['hit_no']:.0%}")
    if context:
        lines.append('Pre-registration context (in-sample, NOT evidence): ' + ' | '.join(context))
    return '\n'.join(lines)


def update(db_path, registry_path=REGISTRY, ledger_path=LEDGER, today=None, dry_run=False):
    today = today or MT.now_eastern().date()
    registry = json.loads(Path(registry_path).read_text(encoding='utf-8'))
    first_registered = min(h['registered'] for h in registry['hypotheses'])
    ledger = load_ledger(ledger_path)
    new_rows = 0
    con = sqlite3.connect(f'file:{Path(db_path).resolve().as_posix()}?mode=ro', uri=True)
    try:
        paths = con.execute('SELECT ticker, scan_date, outcome, r_live FROM tier_a_paths').fetchall()
        for ticker, scan_date, outcome, r_live in paths:
            key = (ticker, scan_date)
            row = ledger.get(key)
            if row is None:
                row = {'ticker': ticker, 'scan_date': scan_date,
                       'cohort': 'prospective' if scan_date >= first_registered else 'pre_registration'}
                new_rows += row['cohort'] == 'prospective'
                ledger[key] = row
            if str(row.get('data_ok')) != '1':            # first sight, or inputs were missing last time
                feats = compute_features(con, ticker, dt.date.fromisoformat(scan_date))
                row.update(feats, feature_version=FEATURE_VERSION,
                           computed_on=today.isoformat() if feats['data_ok'] else row.get('computed_on', ''))
            resolved = outcome in ('T1', 'STOP')
            row.update(outcome=outcome, hit_t1=int(outcome == 'T1') if resolved else '',
                       r_live='' if r_live is None else r_live, outcome_asof=today.isoformat())
    finally:
        con.close()
    summary = summarize(registry, ledger)
    text = report(registry, summary, new_rows)
    if not dry_run:
        save_ledger(ledger_path, ledger)
        registry['status'] = {'last_run': today.isoformat(), 'database': Path(db_path).name,
                              'new_prospective_rows_last_run': new_rows, 'by_hypothesis': summary}
        tmp = Path(str(registry_path) + '.tmp')
        tmp.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(tmp, registry_path)
    return text, new_rows, summary


def main():
    p = argparse.ArgumentParser(description='Track prospective hypotheses (records and reports; never implements)')
    p.add_argument('--db', help='skew_history.db path (default: SKEW_DB_PATH or ./skew_history.db)')
    p.add_argument('--dry-run', action='store_true', help='compute and print; write nothing')
    p.add_argument('--send', action='store_true', help='Telegram the report when new prospective qualifiers appeared')
    a = p.parse_args()
    db = Path(a.db) if a.db else default_db()
    if not db.exists():
        raise SystemExit(f'[prospective] database not found: {db}')
    text, new_rows, _ = update(db, dry_run=a.dry_run)
    print(text)
    if a.send and not a.dry_run:
        if new_rows:
            from alert import send_telegram_status
            status, _ = send_telegram_status(text)
            print(f'[prospective] telegram: {status}')
        else:
            print('[prospective] nothing new since the last run; not sent')


if __name__ == '__main__':
    main()
