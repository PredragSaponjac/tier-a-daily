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
from alert import format_signal, format_no_signal, format_quota_blocked, format_watch_list, send_telegram
import uw_client as uwc
import parameters as P
import position_tracker as PT
import archive
import sheet_sync
import x_post


def select_taken(tradeable, top, open_tickers, sel, rank_key):
    """TAKE-ALL selection (parameters 1.1.0, user decision 2026-09-02). Pure, so preflight
    can test it.

    Returns (taken, skipped_for_cap). With take_all_qualified ON: every gate-passing name
    in rank order, excluding tickers already open, until open + new reaches
    max_concurrent (floor 1 so today's top always goes). OFF: [top] — the old rule.
    Rank order has no measured skill; it is used only to decide who yields to the cap.
    """
    if not sel.get('take_all_qualified'):
        return [top], []
    ranked = [c for c in sorted(tradeable, key=rank_key, reverse=True)
              if c['ticker'] not in open_tickers]
    room = max(1, int(sel.get('max_concurrent', 6)) - len(open_tickers))
    return ranked[:room], [c['ticker'] for c in ranked[room:]]


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
    uwc.reset_quota_flag()   # clear daily-quota tripwire for a fresh run

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

    # 4. Apply the ≥1-STRONG-LEG gate (THE rule earned by the SYM + RGTI losses,
    #    both weak-on-both) + rank. A candidate is TRADEABLE only if it has a deep
    #    structural skew leg OR a real vol-adjusted cushion leg. Weak on BOTH = no
    #    trade, even if it cleared the mechanical Tier A screen. See legs.py.
    import legs as LEGS
    import data_quality as DQ
    sel = P.selection_params()
    for c in enriched:
        c['legs'] = LEGS.assess(c, sel['strong_skew_max'], sel['strong_vol_cushion_min'])
        # DATA-QUALITY gate (earned by SMMT 6/9): reject signals off a noisy/thin
        # options chain — the legs are only as trustworthy as the data they're computed on.
        c['noise'] = DQ.assess_noise(c['ticker'], c['scan_date'],
                                     std_threshold=sel['skew_noise_std_max'])
        # EDGE-VALIDATION logging (2026-07-27 edge hunt) — research only, has NO
        # effect on selection. Scored after 10-20 fresh signals via edge_validation.py.
        try:
            import edge_metrics as EM
            c['edge'] = EM.edge_metrics(c)
        except Exception as _e:
            c['edge'] = None

    survivors = [c for c in enriched if c['vetoes']['pass']]
    vetoed = [c for c in enriched if not c['vetoes']['pass']]
    tradeable = [c for c in survivors
                 if ((not sel['require_strong_leg']) or c['legs']['tradeable'])
                 and not c['noise']['noisy']]

    print(f"\n{'rank':>4s} {'tkr':6s} {'UW':>4s} {'veto':5s} {'legs':10s} note")
    for i, c in enumerate(enriched, 1):
        s = c['filter'].get('score', '—')
        v_status = '—' if c['vetoes']['pass'] else 'X'
        L = c['legs']
        if c['noise']['noisy']:
            leg_tag = 'NOISY'
            note = c['noise']['reason']
        else:
            leg_tag = ('STRONG' if L['tradeable'] else 'NEITHER')
            note = L['reason']
        print(f"  {i:>3d} {c['ticker']:6s} {str(s):>4s} {v_status:>5s} {leg_tag:10s} {note}")

    # 5. Pick top: UW >= min_score is a PRIORITY (picked first); if none of the
    #    tradeable candidates is UW-confirmed, fire the BEST tradeable by leg
    #    strength (number of strong legs, then vol-cushion). Weak-on-both already
    #    excluded above, so a "neither" candidate can never be picked.
    def _rank_key(c):
        sc = c['filter'].get('score') or 0
        L = c['legs']
        n_legs = int(L['strong_skew']) + int(L['strong_cushion'])
        # UW-confirmed (>=min_score) FIRST; then rank by STRUCTURE (number of strong
        # legs, then vol-cushion). The raw sub-threshold UW score is only the final
        # tiebreaker — so a UW-1 falling knife never outranks a UW-0 well-cushioned name.
        return (sc >= min_score, n_legs, L['vol_cushion'] or -999, sc)
    top = max(tradeable, key=_rank_key) if tradeable else None

    # WATCH LIST: survivors the gate blocked, but worth eyeballing (esp. strong UW
    # flow like CAVA — flow can be right even when structure says wait). NOT auto-
    # traded; surfaced in the Telegram alert for a manual call. Ranked by UW, then
    # leg strength; top 5.
    _trade_tks = {c['ticker'] for c in tradeable}
    blocked = [c for c in survivors if c['ticker'] not in _trade_tks]
    def _watch_key(c):
        sc = c['filter'].get('score') or 0
        L = c['legs']
        return (sc, int(L['strong_skew']) + int(L['strong_cushion']), L['vol_cushion'] or -999)
    watch = sorted(blocked, key=_watch_key, reverse=True)[:5]
    watch_block = format_watch_list(watch)

    if top is None:
        # Fail-safe: if UW's daily quota was exhausted, we could NOT see the flow
        # for some/all candidates — so a "no setups" message would be a false
        # negative. Report the quota block honestly and post NO trade instead.
        if uwc.quota_exhausted():
            n_unscored = sum(1 for c in enriched
                             if c['filter'].get('error') == 'uw_quota_exhausted')
            print(f"\nUW DAILY QUOTA EXHAUSTED — {n_unscored} candidate(s) unscored. "
                  f"Posting fail-safe (no trade).")
            msg = format_quota_blocked(scan_date, len(candidates), n_unscored)
            print(f"\n--- ALERT ---\n{msg}\n")
            if not args.dry_run:
                ok = send_telegram(msg)
                print(f"Telegram send: {'OK' if ok else 'FAILED'}")
                archive.archive_daily_run(scan_date, enriched, None, min_score, P.version(),
                                          notes='UW daily quota exhausted — flow scoring unavailable, no trade (fail-safe).')
            return

        print(f"\nNo candidate passed vetoes AND score >= {min_score}. No alert.")
        msg = format_no_signal(scan_date, len(candidates), len(vetoed)) + watch_block
        print(f"\n--- ALERT ---\n{msg}\n")
        if not args.dry_run:
            ok = send_telegram(msg)
            print(f"Telegram send: {'OK' if ok else 'FAILED'}")
            archive.archive_daily_run(scan_date, enriched, None, min_score, P.version(),
                                      notes='Tier A surfaced but no candidate qualified.')
        return

    # Day pool for runner-up context
    day_pool = [c for c in survivors if c['ticker'] != top['ticker']]

    # TAKE-ALL regime (parameters 1.1.0, user decision 2026-09-02). Four independent tests
    # showed the one-per-day pick has NO skill and the names we skipped carried the return
    # (clean universe: PICKED R +0.023 vs SKIPPED +0.518). So every gate-passing name is
    # tracked, in rank order, until open + new reaches max_concurrent. `top` stays the
    # featured name for the alert/X post and the archive's picked_ticker (the self-audit's
    # picked-vs-skipped ledger still needs to know what the OLD rule would have done).
    # A ticker already open is never doubled up. Gates, exits, thresholds: unchanged.
    open_tks = {p['ticker'] for p in PT.list_open()}
    taken, skipped_for_cap = select_taken(tradeable, top, open_tks, sel, _rank_key)
    if sel.get('take_all_qualified'):
        print(f"TAKE-ALL: {len(tradeable)} tradeable, {len(open_tks)} already open, "
              f"cap {sel.get('max_concurrent', 6)} -> tracking {[c['ticker'] for c in taken]}"
              + (f"  (cap skipped {skipped_for_cap})" if skipped_for_cap else ''))
    msg = format_signal(top, day_pool, taken=taken) + watch_block
    print(f"\n--- ALERT ---\n{msg}\n")

    if args.dry_run:
        print("(dry-run: not sending, not adding to tracker)")
    else:
        ok = send_telegram(msg)
        print(f"Telegram send: {'OK' if ok else 'FAILED'}")
        if ok:
            # Compute T1/T2/T3/STOP from entry + parameters, for EVERY tracked name
            tps = P.tp_pcts()
            n_added = 0
            for t in taken:
                entry = t['spot_close']
                T1 = entry * (1 + tps['tp1']/100)
                T2 = entry * (1 + tps['tp2']/100)
                T3 = entry * (1 + tps['tp3']/100)
                STOP = entry * (1 + P.stop_pct()/100)
                added = PT.add_position(t, T1=T1, T2=T2, T3=T3, STOP=STOP, params_version=P.version())
                n_added += int(bool(added))
                print(f"Position tracker: {t['ticker']} {'added' if added else 'already tracking (idempotent skip)'}")
            print(f"Position tracker: {n_added} new position(s) from {len(taken)} tracked name(s)")
            # Archive the full daily record (all candidates + picked + every name taken)
            archive.archive_daily_run(scan_date, enriched, top['ticker'], min_score, P.version(),
                                      taken_tickers=[t['ticker'] for t in taken])
            # X auto-posting is GATED OFF by default. A bad/unstable pick must NEVER
            # auto-publish to a public account again (see the 2026-06-12 RUM incident:
            # a micro-cap whose skew flipped bearish intraday was auto-posted before
            # anyone could look). The bot now PREPARES the post and saves it as a draft
            # for manual review; it only auto-posts if ENABLE_X_AUTOPOST is explicitly set.
            x_msg = x_post.format_signal_for_x(top, [c for c in survivors if c['ticker'] != top['ticker']],
                                               taken=taken)
            if os.environ.get('ENABLE_X_AUTOPOST', '').strip().lower() in ('1', 'true', 'yes'):
                x_ok = x_post.post_to_x(x_msg)
                if x_ok: print('X post: OK')
            else:
                try:
                    os.makedirs('x_drafts', exist_ok=True)
                    draft = f"x_drafts/{scan_date}_{top['ticker']}.txt"
                    with open(draft, 'w', encoding='utf-8') as fh:
                        fh.write(x_msg)
                    print(f"X auto-post DISABLED (safe default) — draft saved to {draft} for manual review.")
                except Exception as e:
                    print(f"X auto-post disabled; draft save failed: {e}")
            # Optional: sync Google Sheet if configured
            sheet_sync.sync_all()


if __name__ == '__main__':
    main()
