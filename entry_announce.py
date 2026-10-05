# -*- coding: utf-8 -*-
"""Morning ENTRY announcement (user decision 2026-10-04).

v2 paper entries fill at the regular-session OPEN the morning after a signal. Shortly
after the open this posts the exact entry, target and stop to Telegram and X, so a
follower can place ONE bracket order (sell at the target, stop at the stop) and let their
broker handle the exits in real time.

It ANNOUNCES ONLY. The official record is unchanged: monitor.py still activates the entry
from the completed daily bar after the close and books exits from completed bars. The
opening print is final once it happens, so the morning price and the official entry are
the same number; monitor.py warns if Yahoo ever disagrees.

Outbox discipline (same as closes): each channel's state is saved BEFORE the network call
('inflight'), so a crash can never cause a blind second post. An unknown outcome is never
retried automatically (reconcile it with outbox_cli.py --entry-ticker); a definite
rejection is retried on the next run, at most MAX_ATTEMPTS times. X is checked against
x_posted.log before posting.

Run: python entry_announce.py            (09:45 and 10:20 New York time, tier_a_open.yml)
     python entry_announce.py --dry-run  (practice: prints, sends nothing, saves nothing)
The after-close monitor run also calls announce_entries() to catch up any entry the
morning runs could not announce (for example, no opening print yet).
"""
import argparse
import datetime as dt
import math
import os

import yfinance as yf

import position_tracker as PT
import x_post
from alert import send_telegram_status
from market_time import next_session, now_eastern, is_session
from state_lock import checkpoint, portfolio_lock

FIELD = 'entry_announcement'
MAX_ATTEMPTS = 3
OPEN_SETTLE = dt.time(9, 31)        # the opening print exists from 09:30 New York time
DONE = {'telegram': ('sent', 'gave_up', 'not_configured'),
        'x': ('posted', 'draft', 'gave_up', 'not_configured')}
RETRY = {'telegram': (None, 'pending', 'failed'), 'x': (None, 'pending', 'rejected')}
UNCERTAIN = ('inflight', 'unknown')


def entry_session(pos):
    """Session whose opening print is this position's paper entry (next-open policy only)."""
    if pos.get('entry_policy') != 'next_regular_open':
        return None
    return next_session(pos.get('signal_date', pos['entry_date']))


def opening_price(ticker, session):
    """The session's official opening print from Yahoo's daily bar (raw, unadjusted)."""
    frame = yf.Ticker(ticker).history(start=session.isoformat(),
                                      end=(session + dt.timedelta(days=1)).isoformat(),
                                      interval='1d', auto_adjust=False)
    if frame is None or frame.empty:
        return None
    rows = [row for ix, row in frame.iterrows() if ix.date() == session]
    if len(rows) != 1:
        return None
    value = float(rows[0]['Open'])
    return value if math.isfinite(value) and value > 0 else None


def levels(pos, opening):
    pct = pos['exit_pcts']
    return {'T1': opening * (1 + pct['tp1'] / 100), 'STOP': opening * (1 + pct['stop'] / 100),
            'tp1_pct': pct['tp1'], 'stop_pct': pct['stop']}


# "official" means the entry is already confirmed from the completed daily bar. Only the
# after-close monitor confirms entries, so an official announcement is always a LATE one
# (the morning runs could not post it) and says so. Headers name the session date, never
# "today", so a late post stays true.
LATE = '⏰ Late post: the morning announcement could not go out.'


def format_telegram(tk, session, signal_date, opening, lv, official):
    entry = ('opening print, confirmed from the completed daily bar' if official
             else 'regular-session opening print')
    return '\n'.join(([LATE] if official else []) + [
        f"📥 {tk} — paper entry filled at the open ({session})",
        f"Entry: ${opening:.2f} ({entry})",
        f"🎯 Target: ${lv['T1']:.2f} ({lv['tp1_pct']:+.0f}%)",
        f"🛑 Stop: ${lv['STOP']:.2f} ({lv['stop_pct']:+.0f}%; a gap through it fills at the open)",
        f"Signal published {signal_date} after the close. Results are booked after the close "
        f"from completed daily bars.",
        "Following along? One bracket order at these two prices handles both exits.",
        "⚠️ Paper research only. NOT financial advice.",
    ])


def format_x(tk, session, signal_date, opening, lv, official=False):
    # X rejects posts with 2+ cashtags: exactly one here. The first line is the dedupe key,
    # so it is the same whenever the post goes out.
    return '\n'.join([
        f"📥 ${tk} — paper entry at the open ({session})",
        f"Entry ${opening:.2f} (signal {signal_date})",
        f"🎯 Target ${lv['T1']:.2f} ({lv['tp1_pct']:+.0f}%) · 🛑 Stop ${lv['STOP']:.2f} ({lv['stop_pct']:+.0f}%)",
        "Booked after the close from completed daily bars; a gap through the stop fills at the open.",
    ] + ([LATE] if official else []) + [
        "⚠️ Quant research only. NOT financial advice.",
    ])


def _autopost():
    return os.environ.get('ENABLE_X_AUTOPOST', '').strip().lower() in ('1', 'true', 'yes')


def _save(tk, key, ann):
    PT.update_position(tk, key, **{FIELD: ann})
    checkpoint()


def announce_entries(dry_run=False, asof=None):
    """Announce every open next-open entry that has filled and is not yet announced.
    Returns the number of positions announced or previewed this call."""
    clock = now_eastern(asof)
    today = clock.date()
    touched = 0
    for pos in PT.list_open():
        session = entry_session(pos)
        if session is None or session > today:
            continue
        ann = dict(pos.get(FIELD) or {})
        if all(ann.get(ch) in DONE[ch] for ch in DONE):
            continue
        tk, key = pos['ticker'], pos.get('signal_date', pos['entry_date'])
        signal_date = pos.get('signal_date', pos['entry_date'])
        if pos.get('status') == 'OPEN' and pos.get('entry_date') == session.isoformat():
            opening, official = float(pos.get('original_entry_price', pos['entry_price'])), True
        else:
            # Still pending: only today's entry can be read live, and only after the open.
            if session != today or not is_session(today) or clock.time() < OPEN_SETTLE:
                continue
            opening, official = ann.get('open') or opening_price(tk, session), False
            if not opening:
                print(f'[{tk}] no opening print yet for {session}; a later run announces it')
                continue
        lv = levels(pos, opening)
        ann.update({'session': session.isoformat(), 'open': opening, 'T1': lv['T1'],
                    'STOP': lv['STOP'], 'source': 'official' if official else 'opening_print'})
        tg_text = format_telegram(tk, session.isoformat(), signal_date, opening, lv, official)
        x_text = format_x(tk, session.isoformat(), signal_date, opening, lv, official)
        touched += 1
        if dry_run:
            print(f'--- [{tk}] would announce (practice run, nothing sent) ---\n{tg_text}\n')
            continue
        for ch in ('telegram', 'x'):
            if ann.get(ch) in UNCERTAIN:
                print(f'::warning::{tk} entry {ch} outcome is uncertain; not resent '
                      f'(reconcile: outbox_cli.py {key} {ch} --entry-ticker {tk} ...)')
        if ann.get('telegram') in RETRY['telegram']:
            n = int(ann.get('telegram_attempts', 0)) + 1
            ann.update(telegram='inflight', telegram_attempts=n)
            _save(tk, key, ann)
            try:
                status, receipt = send_telegram_status(tg_text)
            except Exception as exc:
                status, receipt = 'unknown', None
                print(f'::warning::{tk} entry Telegram uncertain: {type(exc).__name__}')
            ann['telegram'] = ('sent' if status == 'sent' else
                               ('failed' if n < MAX_ATTEMPTS else 'gave_up') if status == 'rejected'
                               else status)
            ann['telegram_message_id'] = receipt
            _save(tk, key, ann)
        # X
        if ann.get('x') in RETRY['x']:
            if not _autopost():
                (PT.ROOT / 'x_drafts').mkdir(exist_ok=True)
                draft = f'x_drafts/{session.isoformat()}_{tk}_ENTRY.txt'
                PT._atomic_write(PT.ROOT / draft, x_text)
                ann.update(x='draft', x_draft=draft)
                _save(tk, key, ann)
            else:
                receipt = x_post.already_posted(x_text.splitlines()[0], since=session.isoformat())
                if receipt:
                    ann.update(x='posted', x_id=receipt)
                    _save(tk, key, ann)
                else:
                    n = int(ann.get('x_attempts', 0)) + 1
                    ann.update(x='inflight', x_attempts=n)
                    _save(tk, key, ann)
                    try:
                        status, receipt = x_post.post_to_x_status(x_text)
                    except Exception as exc:
                        status, receipt = 'unknown', None
                        print(f'::warning::{tk} entry X uncertain: {type(exc).__name__}')
                    ann['x'] = (('gave_up' if n >= MAX_ATTEMPTS else 'rejected')
                                if status == 'rejected' else status)
                    ann['x_id'] = receipt
                    _save(tk, key, ann)
        print(f"[{tk}] entry announcement: telegram={ann.get('telegram')} x={ann.get('x')}")
    return touched


def main():
    p = argparse.ArgumentParser(description='Announce filled paper entries (morning)')
    p.add_argument('--dry-run', action='store_true', help='practice: print only, send and save nothing')
    a = p.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    today = now_eastern().date()
    if not is_session(today):
        print(f'{today} is not a regular session; nothing to announce.')
        return
    if a.dry_run:
        n = announce_entries(dry_run=True)
    else:
        with portfolio_lock():
            n = announce_entries()
    print(f'{n} entry announcement(s) {"previewed" if a.dry_run else "processed"}.')


if __name__ == '__main__':
    main()
