"""Intraday monitor — yfinance-based (FREE, no UW API calls).

For each open position:
  1. Pull intraday OHLC since entry (yfinance, 5-min bars during market hours)
  2. Update MAE/MFE based on intraday highs/lows
  3. If H reaches T1 -> bot auto-closes at TP1, sends close alert
  4. If L reaches stop -> bot exits, sends stop alert
  NOTE: there is NO time-based timeout. Positions exit ONLY on TP1 (win) or STOP (loss).

Runs every 15 min, 13:00-21:45 UTC Mon-Fri: the whole 9:30-16:00 New York session in
both seasons, plus the post-close run (see tier_a_monitor.yml).
"""
import argparse
import datetime as dt
import os
import yfinance as yf
from dotenv import load_dotenv

import parameters as P
import position_tracker as PT
from exits import resolve_bar, held_extremes   # ONE exit rule, shared with path_labels (F02/F03/F20)
import x_post
from alert import format_close, send_telegram


MAX_TG_ATTEMPTS, MAX_X_ATTEMPTS = 8, 3


def publish_pending(dry_run: bool = False) -> int:
    """Telegram + X on every close, WINS AND LOSSES BOTH — from the close OUTBOX.

    RE-AUDIT R4 (2026-10-04). A close is first recorded in closed_trades.json with
    publication {'telegram': 'pending', 'x': 'pending'}. This announces whatever is still
    pending FROM THAT DURABLE RECORD and stores each channel's result, so:
      - a run that crashed after recording a close (position already removed, nothing
        announced) is completed by the next run instead of the close never being published;
      - a retry publishes the RECORDED result, never a recomputed one;
      - X is only retried after a definite rejection, and only if x_posted.log does not
        already hold the post; an unanswered post ('unknown') is left for a human check.
    Closes are published whenever X credentials exist — deliberately NOT behind
    ENABLE_X_AUTOPOST. That switch guards against publishing a bad PICK; a close is the
    outcome of something already public, and gating it would leave public entries with
    no published result — the exact asymmetry we forbid. Returns closes touched.
    """
    todo = PT.pending_publications()
    for rec in todo:
        tk, ed, xd = rec['ticker'], rec['entry_date'], rec.get('exit_date')
        entry, exit_px, reason = float(rec['entry_price']), float(rec['exit_price']), rec['exit_reason']
        pub = rec['publication']
        if dry_run:
            print(f'  [{tk}] (dry-run) close outbox: telegram={pub.get("telegram")} x={pub.get("x")}')
            continue
        if pub.get('telegram') in PT.RETRYABLE['telegram']:
            n = int(pub.get('tg_attempts', 0)) + 1
            ok = send_telegram(format_close(tk, entry, exit_px, reason))
            st = 'sent' if ok else ('failed' if n < MAX_TG_ATTEMPTS else 'gave_up')
            pub = PT.set_publication(tk, ed, telegram=st, tg_attempts=n)['publication']
            print(f'  [{tk}] close -> Telegram: {st}')
        if pub.get('x') in PT.RETRYABLE['x']:
            msg = x_post.format_close_for_x(tk, entry, exit_px, reason, entry_date=ed, exit_date=xd,
                                            mae_pct=rec.get('heat_pct'), mfe_pct=rec.get('peak_pct'))
            n = int(pub.get('x_attempts', 0)) + 1
            tid = x_post.already_posted(msg.splitlines()[0], since=xd)
            status = 'posted' if tid else None
            if status is None:
                status, tid = x_post.post_to_x_status(msg)
            if status == 'posted':
                PT.set_publication(tk, ed, x='posted', x_attempts=n, x_id=tid)
                print(f'  [{tk}] close -> X: posted')
                continue
            os.makedirs('x_drafts', exist_ok=True)
            path = f'x_drafts/{xd}_{tk}_CLOSE.txt'
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(msg)
            final = status != 'rejected' or n >= MAX_X_ATTEMPTS
            st = ('gave_up' if status == 'rejected' else status) if final else 'rejected'
            PT.set_publication(tk, ed, x=st, x_attempts=n, x_draft=path)
            print(f'  [{tk}] close -> X: {st} (attempt {n}); draft {path}')
            # SHOUT (added 2026-09-02). The LCID stop-out on 9/02 failed to post (X API 402
            # "credits depleted") and nobody knew until a manual audit — a public entry sat
            # with no published result, the asymmetry we forbid. Alert on the first failure
            # and when retries end, not on every 15-minute retry.
            if n == 1 or final:
                why = {'rejected': 'X rejected it', 'unknown': 'X did not answer — it MAY be live; check before posting',
                       'not_configured': 'no X credentials'}.get(status, status)
                tail = (' Will retry automatically.' if not final else
                        ' No more automatic retries — post the draft by hand.')
                send_telegram(f'⚠️ {tk} close is NOT confirmed on X ({why}). The public entry has no '
                              f'public result yet. Draft: {path}.{tail}')
    return len(todo)


def _today_iso() -> str:
    return dt.date.today().isoformat()


def check_position(pos: dict, dry_run: bool = False) -> dict | None:
    """Check one position for TP1/stop hits. Returns close info if closed. NO timeout."""
    tk = pos['ticker']
    entry_date = pos['entry_date']
    entry = pos['entry_price']
    T1 = pos['T1']
    STOP = pos['STOP']

    # RE-AUDIT R4: a close already on record (an earlier run recorded it, then failed before
    # removing the position) is FINISHED from that record. The bars are not walked again:
    # a later bar can resolve differently and would announce a second, contradicting result.
    rec = PT.closed_record(tk, entry_date)
    if rec is not None:
        print(f'  [{tk}] close already recorded ({rec["exit_reason"]} {rec["exit_date"]}); finishing it')
        if not dry_run:
            PT.close_position(tk, entry_date, float(rec['exit_price']), rec['exit_reason'], rec['exit_date'])
            publish_pending()
        return {'closed': True, 'reason': rec['exit_reason'], 'exit_price': rec['exit_price'],
                'exit_date': rec['exit_date'], 'record': rec, 'resumed': True}

    # Pull data from entry+1 onward
    e = dt.datetime.strptime(entry_date, '%Y-%m-%d').date()
    end = dt.date.today() + dt.timedelta(days=1)  # full window entry->today (no time cap)
    try:
        # 1-day bars first to find the relevant window; for today's intraday use 5m
        df = yf.Ticker(tk).history(start=e + dt.timedelta(days=1), end=end, auto_adjust=True, interval='1d')
    except Exception as ex:
        print(f'  [{tk}] yfinance error: {ex}')
        return None

    if df.empty:
        # No price action yet (e.g., entered today after close)
        return None

    # Walk day by day; track MAE/MFE; resolve each bar with the SAME rule research uses
    for idx, row in df.iterrows():
        date_str = idx.date().isoformat()
        high = float(row['High'])
        low = float(row['Low'])
        opn = float(row['Open'])
        res = resolve_bar(opn, high, low, T1, STOP)
        # MAE / MFE from the part of the bar actually HELD (audit F20): on an exit bar,
        # prices beyond the fill came after it and must not count.
        lo_h, hi_h = held_extremes(opn, high, low, res)
        upd = PT.update_mae_mfe(tk, entry_date, intraday_low=lo_h, intraday_high=hi_h, on_date=date_str)
        if upd:
            pos = {**pos, 'MAE_pct': upd.get('MAE_pct'), 'MFE_pct': upd.get('MFE_pct')}
        if res is None:
            continue
        reason, exit_price, note = res
        print(f'  [{tk}] {reason} on {date_str}: O {opn:.2f} H {high:.2f} L {low:.2f} '
              f'-> exit {exit_price:.2f}' + (f'  [{note}]' if note else ''))
        if note:
            print(f'::warning::{tk} {date_str}: {note}')
        if dry_run:
            return {'closed': True, 'reason': reason, 'exit_price': exit_price,
                    'exit_date': date_str, 'note': note}
        canon = PT.close_position(tk, entry_date, exit_price, reason, date_str, exit_note=note)
        publish_pending()            # announce from the durable record (the close outbox)
        return {'closed': True, 'reason': canon['exit_reason'], 'exit_price': canon['exit_price'],
                'exit_date': canon['exit_date'], 'note': note, 'record': canon}

    # NO time-based timeout. A position exits ONLY on TP1 (win) or STOP (loss),
    # both handled in the day-walk above. If neither fired, it stays open and
    # keeps tracking MAE/MFE indefinitely until a target or the stop is hit.
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true', help='check positions but do not close or send')
    args = p.parse_args()
    load_dotenv()

    # Resume first: announcements a previous run recorded but did not finish (R4).
    resumed = publish_pending(dry_run=args.dry_run)
    if resumed:
        print(f'Close outbox: {resumed} pending announcement(s) processed.')

    positions = PT.list_open()
    print(f'[{dt.datetime.now().isoformat()}] Monitoring {len(positions)} open position(s)...')

    if not positions:
        print('  (none)')
        if resumed and not args.dry_run:
            try:
                import sheet_sync
                sheet_sync.sync_all()
            except Exception as e:
                print(f'Sheet sync skipped: {e}')
        return

    closed_count = 0
    for pos in positions:
        result = check_position(pos, dry_run=args.dry_run)
        if result and result.get('closed'):
            closed_count += 1
        else:
            print(f"  [{pos['ticker']}] still open  MAE={pos.get('MAE_pct',0):+.2f}%  MFE={pos.get('MFE_pct',0):+.2f}%")

    # Push updated MAE/MFE (and any closes) to the Google Sheet. The monitor was
    # updating open_positions.json every run but NEVER syncing the sheet, so the
    # sheet's MAE/MFE columns sat at 0 forever (the 2026-06-15 MDB "not tracking"
    # issue — it WAS tracking, the sheet just never got refreshed).
    if not args.dry_run:
        try:
            import sheet_sync
            sheet_sync.sync_all()
            print('Sheet synced (MAE/MFE + open/closed positions).')
        except Exception as e:
            print(f'Sheet sync skipped: {e}')

    print(f'\nClosed this run: {closed_count}  |  Still open: {len(positions) - closed_count}')


if __name__ == '__main__':
    main()
