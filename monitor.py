"""Completed-session paper monitoring and recovery of durable close announcements.

No intraday broker orders are placed. Current partial daily bars never resolve an
exit. New signals enter at the next regular-session opening print; existing legacy
positions retain their documented entry convention.
"""
import argparse
import datetime as dt
import math
import os
from contextlib import nullcontext

import yfinance as yf
from dotenv import load_dotenv
import position_tracker as PT
from exits import completed_history, walk_bars
from market_time import now_eastern, next_session, last_completed_session, sessions_between
from state_lock import portfolio_lock, checkpoint
import x_post
from alert import format_close, send_telegram_status

MAX_TG_ATTEMPTS, MAX_X_ATTEMPTS = 8, 3


def publish_pending(dry_run=False, exclude=()):
    """Publish only canonical closes; journal inflight before each network call.

    An interrupted/unknown send is not automatically retried. X receipts can
    reconcile it; other uncertain deliveries require a human check. This trades
    automatic duplication for an explicit unresolved delivery record.
    """
    done_this_run = set(exclude)
    todo = [rec for rec in PT.pending_publications()
            if (rec['ticker'], rec.get('signal_date', rec['entry_date'])) not in done_this_run]
    for rec in todo:
        tk, key = rec['ticker'], rec.get('signal_date', rec['entry_date'])
        ed, xd = rec['entry_date'], rec['exit_date']
        pub = rec['publication']
        if dry_run:
            print(f'[{tk}] close outbox: {pub}')
            continue
        if pub.get('x') in ('inflight', 'unknown'):
            # Never send twice because a receipt acknowledgement was interrupted.
            msg = x_post.format_close_for_x(tk, rec['entry_price'], rec['exit_price'], rec['exit_reason'],
                    entry_date=ed, exit_date=xd, excursion_bounds=rec.get('excursion_bounds'))
            receipt = x_post.already_posted(msg.splitlines()[0], since=xd)
            if receipt:
                pub = PT.set_publication(tk, key, x='posted', x_id=receipt)['publication']
                checkpoint()
        if pub.get('telegram') in ('inflight', 'unknown'):
            print(f'::warning::{tk} Telegram close requires receipt reconciliation; not resent')
        if pub.get('telegram') in PT.RETRYABLE['telegram']:
            msg = format_close(tk, rec['entry_price'], rec['exit_price'], rec['exit_reason'])
            msg += '\nCompleted-session paper outcome. ' + rec.get('note', '')
            if rec.get('split_factor', 1.0) != 1.0:
                msg += '\nPrices normalized for recorded splits; dividends excluded from price return.'
            n = int(pub.get('tg_attempts', 0)) + 1
            PT.begin_publication(tk, key, 'telegram', n)
            checkpoint()
            try:
                status, message_id = send_telegram_status(msg)
            except Exception as exc:
                status, message_id = 'unknown', None
                print(f'::warning::{tk} Telegram delivery uncertain: {type(exc).__name__}')
            final = 'sent' if status == 'sent' else (
                'failed' if status == 'rejected' and n < MAX_TG_ATTEMPTS else
                'gave_up' if status == 'rejected' else status)
            pub = PT.set_publication(tk, key, telegram=final, telegram_message_id=message_id)['publication']
            checkpoint()
        if pub.get('x') in PT.RETRYABLE['x']:
            msg = x_post.format_close_for_x(tk, rec['entry_price'], rec['exit_price'], rec['exit_reason'],
                    entry_date=ed, exit_date=xd, mae_pct=rec.get('heat_pct'), mfe_pct=rec.get('peak_pct'),
                    excursion_bounds=rec.get('excursion_bounds'),
                    split_basis=rec.get('price_basis'))
            if (rec.get('entry_policy') == 'next_regular_open' and
                    os.environ.get('ENABLE_X_AUTOPOST', '').strip().lower() not in ('1', 'true', 'yes')):
                os.makedirs('x_drafts', exist_ok=True)
                path = f'x_drafts/{key}_{tk}_CLOSE.txt'
                from pathlib import Path
                PT._atomic_write(Path(path), msg)
                PT.set_publication(tk, key, x='draft', x_draft=path)
                checkpoint()
                print(f'[{tk}] X manual mode: close draft saved at {path}')
                continue
            n = int(pub.get('x_attempts', 0)) + 1
            receipt = x_post.already_posted(msg.splitlines()[0], since=xd)
            if receipt:
                PT.set_publication(tk, key, x='posted', x_id=receipt)
                checkpoint()
                continue
            PT.begin_publication(tk, key, 'x', n)
            checkpoint()
            try:
                status, receipt = x_post.post_to_x_status(msg)
            except Exception as exc:
                status, receipt = 'unknown', None
                print(f'::warning::{tk} X delivery uncertain: {type(exc).__name__}')
            final = ('gave_up' if n >= MAX_X_ATTEMPTS else 'rejected') if status == 'rejected' else status
            fields = {'x': final, 'x_id': receipt}
            if status != 'posted':
                os.makedirs('x_drafts', exist_ok=True)
                path = f'x_drafts/{key}_{tk}_CLOSE.txt'
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write(msg)
                fields['x_draft'] = path
                print(f'::warning::{tk} X close {final}; draft: {path}. Unknown sends need reconciliation.')
            PT.set_publication(tk, key, **fields)
            checkpoint()
    for rec in PT.unresolved_publications():
        print(f'::warning::{rec["ticker"]} unresolved close delivery: {rec["publication"]}')
    return len(todo)


def _basis(pos, frame):
    """Normalize frozen entry/levels to Yahoo's current split-rebased OHLC basis."""
    if not {'Stock Splits', 'Dividends'}.issubset(frame.columns):
        raise ValueError('Corporate-action columns missing; split basis cannot be established')
    factor = 1.0
    actions = []
    entry_date = dt.date.fromisoformat(pos['entry_date'])
    for ix, row in frame.iterrows():
        split = float(row['Stock Splits'])
        dividend = float(row['Dividends'])
        if not math.isfinite(split) or not math.isfinite(dividend):
            raise ValueError('nonfinite corporate action')
        if split and ix.date() > entry_date:
            if split <= 0:
                raise ValueError('invalid split factor')
            factor *= split
            actions.append({'date': ix.date().isoformat(), 'split': split})
        if dividend:
            actions.append({'date': ix.date().isoformat(), 'dividend': dividend,
                            'return_treatment': 'excluded_price_only'})
    original = pos.get('original_entry_price', pos['entry_price'])
    levels = pos.get('original_levels', {k: pos[k] for k in ('T1', 'T2', 'T3', 'STOP')})
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError('Invalid cumulative split factor')
    if any(not math.isfinite(float(v)) or float(v) <= 0 for v in (original, *levels.values())):
        raise ValueError('Invalid frozen original price basis')
    return {**pos, 'original_entry_price': original, 'original_levels': levels,
            'entry_price': original / factor, **{k: v / factor for k, v in levels.items()},
            'split_factor': factor, 'quantity_factor': factor, 'corporate_actions': actions,
            'price_basis': 'split_normalized_price_only',
            'price_basis_asof': dt.datetime.now(dt.timezone.utc).isoformat()}


def check_position(pos, dry_run=False, asof=None):
    tk, key = pos['ticker'], pos.get('signal_date', pos['entry_date'])
    existing = PT.closed_record(tk, key)
    if existing is not None:
        if not dry_run:
            PT.close_position(tk, key, existing['exit_price'], existing['exit_reason'], existing['exit_date'])
            checkpoint()
        return {'closed': True, 'reason': existing['exit_reason'], 'exit_price': existing['exit_price'],
                'exit_date': existing['exit_date'], 'record': existing, 'resumed': True}
    signal_date = dt.date.fromisoformat(pos.get('signal_date', pos['entry_date']))
    pending = pos.get('status') == 'PENDING_ENTRY'
    include_entry = pos.get('entry_policy') == 'next_regular_open'
    clock = now_eastern(asof)
    start = next_session(signal_date) if pending else (
        dt.date.fromisoformat(pos['entry_date']) if include_entry else next_session(pos['entry_date']))
    expected = sessions_between(start, last_completed_session(clock))
    if not expected:
        return None
    end = clock.date() + dt.timedelta(days=1)
    frame = yf.Ticker(tk).history(start=start, end=end, interval='1d', auto_adjust=False, actions=True)
    actions_frame = frame
    # Validate before activating a queued entry or rewriting any existing state.
    frame = completed_history(frame, start, clock)
    if pending:
        first = frame.index[0].date().isoformat()
        # Yahoo rebases the historical opening by subsequent splits. Preserve the
        # original opening basis once, then normalize from it on every retry.
        temp = {**pos, 'entry_date': first}
        factor = _basis(temp, actions_frame)['split_factor']
        opening = float(frame.iloc[0]['Open']) * factor
        if dry_run:
            pct = pos['exit_pcts']
            levels = {field: opening * (1 + pct[name] / 100) for field, name in
                      {'T1': 'tp1', 'T2': 'tp2', 'T3': 'tp3', 'STOP': 'stop'}.items()}
            pos = {**pos, 'entry_date': first, 'entry_price': opening,
                   'original_entry_price': opening, 'original_levels': levels, **levels}
        else:
            pos = PT.activate_position(tk, key, first, opening)
            # The morning post announced this same opening print (entry_announce.py). The
            # official entry is this completed-bar value; say so loudly if Yahoo ever differs.
            announced = (pos.get('entry_announcement') or {}).get('open')
            if announced and abs(float(announced) / opening - 1) > 0.001:
                note = f'announced open {float(announced):.4f} differs from official open {opening:.4f}'
                print(f'::warning::[{tk}] {note}')
                pos = PT.update_position(tk, key, entry_note=note)
    pos = _basis(pos, actions_frame)
    if not dry_run:
        pos = PT.update_position(tk, key, **{k: v for k, v in pos.items() if k not in ('ticker', 'signal_date')})
    result = walk_bars(frame, pos['entry_price'],
                       (pos['T1'] / pos['entry_price'] - 1) * 100,
                       (pos['STOP'] / pos['entry_price'] - 1) * 100)
    if not dry_run:
        PT.update_excursions(tk, key, result)
    if not result['complete']:
        return None
    if dry_run:
        return {'closed': True, 'reason': result['outcome'], **result}
    canon = PT.close_position(tk, key, result['exit_price'], result['outcome'], result['exit_date'],
                              exit_note=result['exit_note'])
    checkpoint()
    return {'closed': True, 'reason': canon['exit_reason'], 'exit_price': canon['exit_price'],
            'exit_date': canon['exit_date'], 'record': canon}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='read-only execution preview')
    args = parser.parse_args()
    load_dotenv()
    with nullcontext() if args.dry_run else portfolio_lock():
        # Delivery recovery is independent of the market feed. A missing bar for
        # another holding must not block an outcome already recorded durably.
        resumed = {(rec['ticker'], rec.get('signal_date', rec['entry_date']))
                   for rec in PT.pending_publications()}
        publish_pending(dry_run=args.dry_run)
        errors = []
        for pos in PT.list_open():
            # One holding's bad or incomplete price feed must not stop the others from being
            # checked (review 2026-10-04). The run still FAILS at the end, naming it.
            try:
                result = check_position(pos, dry_run=args.dry_run)
                print(f"[{pos['ticker']}] {'closed' if result else 'open or pending entry'}")
            except Exception as exc:
                errors.append(f"{pos['ticker']}: {type(exc).__name__}: {exc}")
                print(f"::error::[{pos['ticker']}] not checked this run: {type(exc).__name__}: {exc}")
        if not args.dry_run:
            checkpoint()
        # Catch up any entry the morning announcement runs could not post (no opening print
        # yet, a GitHub outage): now announced from the official completed-bar open. Runs
        # BEFORE new closes are published, so an entry is never announced after its close.
        try:
            import entry_announce
            entry_announce.announce_entries(dry_run=args.dry_run)
        except Exception as exc:
            errors.append(f'entry announcements: {type(exc).__name__}: {exc}')
            print(f'::error::entry announcement catch-up failed: {type(exc).__name__}: {exc}')
        # Definite rejections get at most one retry per record in this run.
        publish_pending(dry_run=args.dry_run, exclude=resumed)
        if not args.dry_run:
            import sheet_sync
            if sheet_sync.sync_all() is False:
                raise SystemExit('Sheet synchronization incomplete; durable portfolio remains available')
            if PT.unresolved_publications():
                raise SystemExit('Close delivery incomplete; reconcile the durable outbox')
        if errors:
            raise SystemExit('Position check failed for: ' + '; '.join(errors))


if __name__ == '__main__':
    main()
