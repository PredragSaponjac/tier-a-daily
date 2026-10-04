"""Tier A Daily — orchestrator.

Daily run (after Skew Tracker PM scan + v3 AR scanner produces Tier A candidates):
  1. Read Tier A candidates from skew_history.db
  2. Compute UW composite filter score for each
  3. Run vetoes (earnings, liquidity)
  4. Rank survivors by filter score
  5. Send top-pick to Telegram (or just print if --dry-run)

CLI:
  python main.py                        # latest scan, send to Telegram
  python main.py --scan-date YYYY-MM-DD # REPLAY a past date, READ-ONLY (implies --dry-run)
  python main.py --scan-date D --live   # replay that really sends/tracks (not point-in-time!)
  python main.py --dry-run              # don't send, just print
  python main.py --no-dp                # skip dark pool pulls (faster)
  python main.py --min-score 3          # override parameters.json min_filter_score
  python main.py --require-today        # exit silently if no scan for today (production cron)
  python main.py --resend               # deliver even if this scan_date was already decided
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
    max_concurrent. OFF: [top] — the old rule. Either way a ticker already open is never
    taken again, and `taken` may be EMPTY.
    Rank order has no measured skill; it is used only to decide who yields to the cap.

    AUDIT F06 (2026-10-04): room was max(1, cap - open) — "floor 1 so today's top always
    goes". With 6 already open that admitted a 7th, and on later days an 8th: the cap
    the user agreed to (6, each sized at 1/6 of the book) did not hold. Now max(0, ...).
    Re-audit: the top-only switch (OFF) obeys the same cap; it used to bypass it.
    """
    room = max(0, int(sel.get('max_concurrent', 6)) - len(open_tickers))
    if not sel.get('take_all_qualified'):
        fresh = top is not None and top['ticker'] not in open_tickers
        return ([top] if fresh and room > 0 else []), ([top['ticker']] if fresh and room == 0 else [])
    ranked = [c for c in sorted(tradeable, key=rank_key, reverse=True)
              if c['ticker'] not in open_tickers]
    return ranked[:room], [c['ticker'] for c in ranked[room:]]


def effective_dry_run(scan_date, live: bool, dry_run: bool) -> bool:
    """AUDIT F16 (2026-10-04). Pure, so preflight can test it. --scan-date was documented as
    "backtest" but only changed the date: it still sent Telegram/X, opened positions and
    overwrote that day's archive, and its vetoes queried TODAY's earnings calendar, so it
    was neither safe nor point-in-time. A replay is read-only unless --live is explicit."""
    return bool(dry_run or (scan_date and not live))


# ---------------------------------------------------------------- the decision OUTBOX
# RE-AUDIT R2/R6 (2026-10-04). Before: a decision was sent first and archived afterwards,
# so a crash between an accepted Telegram send and the archive (e.g. a failed position
# write) left no record, and the retry sent the same signal again. Failed "no signal"
# messages returned normally and were never retried, and a rejected X entry was never
# retried at all. Now:
#   1. the decision is ARCHIVED FIRST, with the exact texts and position specs (the outbox)
#      and every channel 'pending';
#   2. run_outbox() delivers from that record, in order, storing each channel's result:
#      Telegram -> positions (only after Telegram succeeded) -> X;
#   3. a retry RESUMES the archived decision (only its unfinished channels), never a new
#      decision from a fresh rescan, and X is checked against x_posted.log before posting.
# Residual, stated: a crash in the instant between Telegram accepting a message and its
# status being written leaves it 'pending', and the retry sends it again (Telegram is the
# private channel, so that duplicate is accepted). X is never re-posted on an unknown result.
X_MAX_ATTEMPTS = 3
RETRYABLE = {'telegram': ('pending', 'failed'), 'positions': ('pending', 'failed'),
             'x': ('pending', 'rejected')}


def _x_autopost() -> bool:
    return os.environ.get('ENABLE_X_AUTOPOST', '').strip().lower() in ('1', 'true', 'yes')


def outbox_pending(delivery: dict) -> bool:
    return any((delivery or {}).get(ch) in st for ch, st in RETRYABLE.items())


def prior_state(scan_date: str):
    """('none', None)            nothing decided for this date yet
       ('done', rec)             decided, nothing left to deliver
       ('resume', rec)           an archived decision with undelivered channels
       ('legacy_retry', rec)     pre-outbox archive that recorded a delivery failure
    Pure apart from reading the archive, so preflight can test it."""
    rec = archive.load_archive(scan_date)
    if rec is None:
        return 'none', None
    if 'delivery' not in rec:                  # archives written before 2026-10-04
        return ('legacy_retry', rec) if 'DELIVERY FAILED' in (rec.get('notes') or '') else ('done', rec)
    return ('resume', rec) if outbox_pending(rec['delivery']) else ('done', rec)


def run_outbox(scan_date: str) -> bool:
    """Deliver whatever the archived decision for scan_date still has pending. True when no
    retryable channel remains (the run may complete), False when a retry is needed."""
    rec = archive.load_archive(scan_date)
    d, box = rec.get('delivery') or {}, rec.get('outbox') or {}
    # 1. Telegram, required for every decision. A failure stops here: nothing is opened and
    #    nothing is published before the user has been told.
    if d.get('telegram') in RETRYABLE['telegram']:
        n = int(d.get('telegram_attempts', 0)) + 1
        ok = send_telegram(box['telegram'])
        d = archive.update_delivery(scan_date, telegram='sent' if ok else 'failed', telegram_attempts=n)['delivery']
        print(f"Telegram send: {'OK' if ok else 'FAILED'} (attempt {n})")
        if not ok:
            print('::error::Telegram delivery failed; nothing opened or posted; the retry resends it')
            return False
    # 2. Positions, idempotent on (ticker, entry_date), at the ARCHIVED entry levels.
    if d.get('positions') in RETRYABLE['positions']:
        try:
            for spec in box.get('positions', []):
                added = PT.add_position(spec['candidate'], T1=spec['T1'], T2=spec['T2'], T3=spec['T3'],
                                        STOP=spec['STOP'], params_version=spec['params_version'])
                print(f"Position tracker: {spec['candidate']['ticker']} "
                      f"{'added' if added else 'already tracking (idempotent skip)'}")
        except Exception as e:
            archive.update_delivery(scan_date, positions='failed', positions_error=f'{type(e).__name__}: {e}'[:300])
            print(f'::error::position tracking failed ({e}); the retry resumes it')
            return False
        d = archive.update_delivery(scan_date, positions='tracked')['delivery']
    # 3. X, the public entry. Retried only after a DEFINITE rejection and never if
    #    x_posted.log already holds it; an unanswered post is left for a human check.
    if d.get('x') in RETRYABLE['x']:
        text = box['x']
        n = int(d.get('x_attempts', 0)) + 1
        tid = x_post.already_posted(text.splitlines()[0], since=scan_date)
        status = 'posted' if tid else None
        if status is None:
            status, tid = x_post.post_to_x_status(text)
        if status == 'posted':
            archive.update_delivery(scan_date, x='posted', x_attempts=n, x_id=tid)
            print('X post: OK')
        else:
            os.makedirs('x_drafts', exist_ok=True)
            draft = f"x_drafts/{scan_date}_{box.get('featured', 'signal')}.txt"
            with open(draft, 'w', encoding='utf-8') as fh:
                fh.write(text)
            final = status != 'rejected' or n >= X_MAX_ATTEMPTS
            st = ('gave_up' if status == 'rejected' else status) if final else 'rejected'
            archive.update_delivery(scan_date, x=st, x_attempts=n, x_draft=draft)
            print(f'::warning::X entry post {st} (attempt {n}); draft saved to {draft}')
            if n == 1 or final:
                why = {'rejected': 'X rejected it', 'unknown': 'X did not answer — it MAY be live; check first',
                       'not_configured': 'no X credentials'}.get(status, status)
                send_telegram(f"⚠️ {box.get('featured')} ENTRY is NOT confirmed on X ({why}). The position "
                              f"is tracked; the public entry is missing. Draft: {draft}."
                              + (' The next run retries.' if not final else ' Post it by hand.'))
            if not final:
                return False
    if box.get('positions'):
        try:
            sheet_sync.sync_all()
        except Exception as e:
            print(f'Sheet sync skipped: {e}')
    return True


def record_and_deliver(scan_date, enriched, picked, min_score, notes, telegram_text, *,
                       taken=(), x_text=None, featured=None, context=None):
    """Archive the decision FIRST (with its outbox), then deliver it. Exits non-zero when a
    channel still needs a retry, so the PM completion marker is not written and the backup
    run resumes it."""
    import parameters as _P
    specs = []
    tps = _P.tp_pcts()
    for t in taken:
        entry = t['spot_close']
        f = t.get('filter') or {}
        specs.append({'candidate': {'ticker': t['ticker'], 'scan_date': t['scan_date'], 'spot_close': entry,
                                    'filter': {'score': f.get('score'), 'raw': f.get('raw') or {}}},
                      'T1': entry * (1 + tps['tp1'] / 100), 'T2': entry * (1 + tps['tp2'] / 100),
                      'T3': entry * (1 + tps['tp3'] / 100), 'STOP': entry * (1 + _P.stop_pct() / 100),
                      'params_version': _P.version()})
    if x_text is not None and not _x_autopost():
        # X auto-posting gated off: the post is PREPARED for manual review, never sent.
        os.makedirs('x_drafts', exist_ok=True)
        draft = f'x_drafts/{scan_date}_{featured}.txt'
        with open(draft, 'w', encoding='utf-8') as fh:
            fh.write(x_text)
        print(f'X auto-post DISABLED (safe default) — draft saved to {draft} for manual review.')
        x_state = 'draft'
    else:
        x_state = 'pending' if x_text is not None else 'n/a'
    delivery = {'telegram': 'pending', 'positions': 'pending' if specs else 'n/a', 'x': x_state}
    outbox = {'telegram': telegram_text, 'x': x_text, 'positions': specs, 'featured': featured}
    archive.archive_daily_run(scan_date, enriched, picked, min_score, P.version(), notes=notes,
                              taken_tickers=[t['ticker'] for t in taken], delivery=delivery,
                              outbox=outbox, context=context)
    if not run_outbox(scan_date):
        raise SystemExit(1)


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
    p.add_argument('--live', action='store_true',
                   help='with --scan-date: really send and track. A replay uses TODAY\'s earnings '
                        'calendar and option chains, not what was known on that date')
    p.add_argument('--resend', action='store_true',
                   help='deliver even if this scan_date was already decided (normally refused)')
    args = p.parse_args()
    if effective_dry_run(args.scan_date, args.live, args.dry_run) and not args.dry_run:
        print(f'--scan-date {args.scan_date}: REPLAY is READ-ONLY (implies --dry-run). Vetoes use '
              f'today\'s earnings calendar and option chains, so it is not point-in-time. '
              f'Pass --live to really send/track.')
        args.dry_run = True
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

    # Retry safety (audit F11 + re-audit R2): a date is decided ONCE. A retry either finds
    # it fully delivered, or RESUMES the archived decision's unfinished channels; it never
    # decides again from a fresh rescan.
    if not args.dry_run and not args.resend:
        state, prior = prior_state(scan_date)
        if state == 'done':
            print(f"Already decided for {scan_date} (archived {prior.get('run_at')}, tracked "
                  f"{prior.get('taken_tickers')}): NOT delivering again. --resend overrides.")
            return
        if state == 'resume':
            print(f"Resuming the archived decision for {scan_date} (run {prior.get('run_id')}): "
                  f"{prior.get('delivery')}")
            if not run_outbox(scan_date):
                raise SystemExit(1)
            return

    if len(candidates) == 0:
        msg = format_no_signal(scan_date, 0, 0)
        print(f"\n--- ALERT ---\n{msg}\n")
        if not args.dry_run:
            # Archived even on an empty day (so we know the bot ran); the Telegram note is a
            # required delivery now: a failed send is retried, not silently dropped (R6).
            record_and_deliver(scan_date, [], None, min_score, 'No Tier A candidates today.', msg)
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
                record_and_deliver(scan_date, enriched, None, min_score,
                                   'UW daily quota exhausted — flow scoring unavailable, no trade (fail-safe).',
                                   msg, context={'portfolio_before': sorted(p['ticker'] for p in PT.list_open())})
            return

        print(f"\nNo candidate passed vetoes AND score >= {min_score}. No alert.")
        msg = format_no_signal(scan_date, len(candidates), len(vetoed)) + watch_block
        print(f"\n--- ALERT ---\n{msg}\n")
        if not args.dry_run:
            record_and_deliver(scan_date, enriched, None, min_score,
                               'Tier A surfaced but no candidate qualified.', msg,
                               context={'portfolio_before': sorted(p['ticker'] for p in PT.list_open())})
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

    # AUDIT F06/F18: names qualified but NONE admitted (the book is at the cap, or every
    # qualifier is already open). Never announce a "NEW Tier A Signal" that opens nothing.
    if not taken:
        why = (f"book at cap ({len(open_tks)}/{sel.get('max_concurrent', 6)})"
               if skipped_for_cap else 'every qualifier is already an open position')
        note = (f"Tier A Daily — {scan_date}\n{len(tradeable)} name(s) qualified but none admitted: "
                f"{why}. Skipped: {', '.join(skipped_for_cap or [c['ticker'] for c in tradeable])}. "
                f"No new position, no X post.")
        print(f"\n--- NO ENTRY ---\n{note}\n")
        if not args.dry_run:
            record_and_deliver(scan_date, enriched, top['ticker'], min_score,
                               f'Qualified but none admitted: {why}', note,
                               context={'portfolio_before': sorted(open_tks),
                                        'max_concurrent': sel.get('max_concurrent', 6),
                                        'cap_skipped': skipped_for_cap})
        return

    # AUDIT F18: feature a name that was ACTUALLY admitted. `top` is ranked before open
    # tickers are excluded, so it could be a position already held — announced as new
    # while nothing was opened. `top` stays the archive's picked_ticker, because the
    # self-audit's picked-vs-skipped ledger scores what the OLD one-pick rule would do.
    featured = taken[0]
    day_pool = [c for c in survivors if c['ticker'] != featured['ticker']]
    msg = format_signal(featured, day_pool, taken=taken) + watch_block
    print(f"\n--- ALERT ---\n{msg}\n")

    if args.dry_run:
        print("(dry-run: not sending, not adding to tracker)")
        return
    # X auto-posting is gated by ENABLE_X_AUTOPOST (see the 2026-06-12 RUM incident: a
    # micro-cap whose skew flipped bearish intraday was auto-posted before anyone could
    # look). Gated off, the post is prepared as a draft for manual review, never sent.
    # The decision (texts, positions at the archived entry levels) is archived FIRST, then
    # delivered: Telegram -> positions (only once Telegram succeeded: no position is ever
    # opened unannounced) -> X. A failure leaves the run red and the backup run resumes
    # exactly this decision (audit F19, re-audit R2/R6).
    record_and_deliver(scan_date, enriched, top['ticker'], min_score, '', msg, taken=taken,
                       x_text=x_post.format_signal_for_x(featured, day_pool, taken=taken),
                       featured=featured['ticker'],
                       context={'portfolio_before': sorted(open_tks),
                                'max_concurrent': sel.get('max_concurrent', 6),
                                'cap_skipped': skipped_for_cap})


if __name__ == '__main__':
    main()
