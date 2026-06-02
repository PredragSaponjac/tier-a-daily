"""Tier A Daily — orchestrator.

Daily run (after Skew Tracker PM scan + v3 AR scanner produces Tier A candidates):
  1. Read Tier A candidates from skew_history.db
  2. Compute UW composite filter score for each
  3. Run vetoes (earnings, liquidity)
  4. Rank survivors by filter score
  5. Send top-pick to Telegram (or just print if --dry-run)

CLI:
  python main.py                        # latest scan, send to Telegram
  python main.py --scan-date YYYY-MM-DD # backtest a specific date
  python main.py --dry-run              # don't send, just print
  python main.py --no-dp                # skip dark pool pulls (faster)
  python main.py --min-score 3          # override parameters.json min_filter_score
  python main.py --require-today        # exit silently if no scan for today (production cron)
"""
import argparse
import os
from dotenv import load_dotenv

from scanner_reader import read_tier_a
from uw_filter import enrich_candidates
from vetoes import run_vetoes
from alert import format_signal, format_no_signal, send_telegram
import parameters as P
import position_tracker as PT
import archive
import sheet_sync
import x_post


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--scan-date', default=None, help='YYYY-MM-DD (default: latest)')
    p.add_argument('--dry-run', action='store_true', help='format but do not send')
    p.add_argument('--no-dp', action='store_true', help='skip dark pool (faster)')
    p.add_argument('--min-score', type=int, default=None,
                   help='override parameters.json min_filter_score for this run')
    p.add_argument('--require-today', action='store_true',
                   help='exit if MAX(scan_date) != today (use in production cron)')
    p.add_argument('--vol-days', type=int, default=None,
                   help='override options-volume pull size (for backtesting old dates)')
    args = p.parse_args()
    min_score = args.min_score if args.min_score is not None else P.min_filter_score()

    load_dotenv()

    # 1. Read Tier A candidates
    candidates, scan_date = read_tier_a(args.scan_date)
    print(f"\n{'='*60}\nTier A Daily v{P.version()} — scan {scan_date}\n{'='*60}")
    print(f"Tier A candidates: {len(candidates)}")
    print(f"Using min_filter_score = {min_score}")

    # Production cron guard: only act on TODAY's scan
    if args.require_today:
        import datetime as _dt
        today_str = _dt.date.today().isoformat()
        if scan_date != today_str:
            print(f"--require-today: latest scan {scan_date} != today {today_str}. Exit (probably holiday/weekend).")
            return

    if len(candidates) == 0:
        msg = format_no_signal(scan_date, 0, 0)
        print(f"\n--- ALERT ---\n{msg}\n")
        if not args.dry_run:
            ok = send_telegram(msg)
            print(f"Telegram send: {'OK' if ok else 'FAILED'}")
            # Archive empty day too (so we know the bot ran)
            archive.archive_daily_run(scan_date, [], None, min_score, P.version(),
                                      notes='No Tier A candidates today.')
        return

    # 2. Compute UW filter scores
    print(f"Computing UW scores (pull_dp={not args.no_dp}, vol_days={args.vol_days or P.options_volume_days()})...")
    enriched = enrich_candidates(candidates, pull_dp=not args.no_dp, vol_days=args.vol_days)

    # 3. Run vetoes
    print("Running vetoes...")
    for c in enriched:
        c['vetoes'] = run_vetoes(c)

    # 4. Rank survivors
    survivors = [c for c in enriched if c['vetoes']['pass']]
    vetoed = [c for c in enriched if not c['vetoes']['pass']]
    print(f"\n{'rank':>4s} {'tkr':6s} {'score':>5s} {'veto':6s} status")
    for i, c in enumerate(enriched, 1):
        s = c['filter'].get('score', '—')
        v_status = '—' if c['vetoes']['pass'] else 'X'
        v_reason = '; '.join(c['vetoes']['reasons']) if c['vetoes']['reasons'] else ''
        print(f"  {i:>3d} {c['ticker']:6s} {str(s):>5s} {v_status:>6s} {v_reason}")

    # 5. Pick top
    # None-safe: a candidate whose UW score could not be computed (no
    # options-volume data / insufficient pre-window) has score None. Coerce
    # None -> 0 so it is simply filtered out (can't confirm flow = don't fire)
    # instead of crashing the comparison. Same guard for a missing veto pass.
    top = next((c for c in enriched
                if c['vetoes'].get('pass')
                and (c['filter'].get('score') or 0) >= min_score), None)

    if top is None:
        print(f"\nNo candidate passed vetoes AND score >= {min_score}. No alert.")
        msg = format_no_signal(scan_date, len(candidates), len(vetoed))
        print(f"\n--- ALERT ---\n{msg}\n")
        if not args.dry_run:
            ok = send_telegram(msg)
            print(f"Telegram send: {'OK' if ok else 'FAILED'}")
            archive.archive_daily_run(scan_date, enriched, None, min_score, P.version(),
                                      notes='Tier A surfaced but no candidate qualified.')
        return

    # Day pool for runner-up context
    day_pool = [c for c in survivors if c['ticker'] != top['ticker']]
    msg = format_signal(top, day_pool)
    print(f"\n--- ALERT ---\n{msg}\n")

    if args.dry_run:
        print("(dry-run: not sending, not adding to tracker)")
    else:
        ok = send_telegram(msg)
        print(f"Telegram send: {'OK' if ok else 'FAILED'}")
        if ok:
            # Compute T1/T2/T3/STOP from entry + parameters
            entry = top['spot_close']
            tps = P.tp_pcts()
            T1 = entry * (1 + tps['tp1']/100)
            T2 = entry * (1 + tps['tp2']/100)
            T3 = entry * (1 + tps['tp3']/100)
            STOP = entry * (1 + P.stop_pct()/100)
            added = PT.add_position(top, T1=T1, T2=T2, T3=T3, STOP=STOP, params_version=P.version())
            print(f"Position tracker: {'added' if added else 'already tracking (idempotent skip)'}")
            # Archive the full daily record (all candidates + picked)
            archive.archive_daily_run(scan_date, enriched, top['ticker'], min_score, P.version())
            # Optional: post to X if creds present
            x_msg = x_post.format_signal_for_x(top, [c for c in survivors if c['ticker'] != top['ticker']])
            x_ok = x_post.post_to_x(x_msg)
            if x_ok: print('X post: OK')
            # Optional: sync Google Sheet if configured
            sheet_sync.sync_all()


if __name__ == '__main__':
    main()
