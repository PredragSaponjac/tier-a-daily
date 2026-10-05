"""Reconcile an uncertain delivery or resume an already archived entry decision.

Reconciliation records evidence without sending. Explicit `DATE resume` resumes
pending channels from the archived decision, without recalculating admission.
A deliberate --not-sent requires verified non-delivery. Invoke in the checkout
whose state will be committed/deployed.
"""
import argparse
import archive
import position_tracker as PT
from state_lock import checkpoint, portfolio_lock


def reconcile(scan_date, channel, *, receipt=None, not_sent=False, evidence, close_ticker=None,
              entry_ticker=None):
    if channel not in ('telegram', 'x') or not evidence.strip():
        raise ValueError('Channel and reconciliation evidence are required')
    if bool(receipt) == bool(not_sent):
        raise ValueError('Provide exactly one receipt or verified not-sent decision')
    if close_ticker and entry_ticker:
        raise ValueError('Reconcile a close OR an entry announcement, not both')
    with portfolio_lock():
        if entry_ticker:
            # Morning entry announcement (entry_announce.py), keyed by the signal date.
            pos = next((p for p in PT.list_open() if PT._matches(p, entry_ticker, scan_date)), None)
            ann = dict((pos or {}).get('entry_announcement') or {})
            if pos is None or ann.get(channel) not in ('inflight', 'unknown', 'gave_up', 'not_configured'):
                raise ValueError('No uncertain entry announcement to reconcile')
            ann[channel] = ('sent' if channel == 'telegram' else 'posted') if receipt else 'pending'
            ann[channel + '_reconciliation'] = evidence
            ann['telegram_message_id' if channel == 'telegram' else 'x_id'] = receipt
            if not receipt:                       # verified NOT sent: allow one fresh attempt
                ann[channel + '_attempts'] = 0
            PT.update_position(entry_ticker, scan_date, entry_announcement=ann)
            checkpoint()
            return
        if close_ticker:
            rec = PT.closed_record(close_ticker, scan_date)
            if rec is None or rec.get('publication', {}).get(channel) not in (
                    'inflight', 'unknown', 'manual_required', 'not_configured', 'gave_up'):
                raise ValueError('No uncertain canonical close delivery to reconcile')
            status = ('sent' if channel == 'telegram' else 'posted') if receipt else 'pending'
            receipt_field = 'telegram_message_id' if channel == 'telegram' else 'x_id'
            PT.set_publication(close_ticker, scan_date, **{channel: status,
                channel + '_reconciliation': evidence, receipt_field: receipt})
            checkpoint()
            return
        rec = archive.load_archive(scan_date)
        if rec is None or rec.get('delivery', {}).get(channel) not in (
                'inflight', 'unknown', 'manual_required', 'not_configured', 'gave_up'):
            raise ValueError('No uncertain delivery to reconcile')
        status = ('sent' if channel == 'telegram' else 'posted') if receipt else 'pending'
        archive.update_delivery(scan_date, **{channel: status, channel + '_id': receipt,
                                channel + '_reconciliation': evidence, 'complete': False})
        checkpoint()


def resume(scan_date):
    import main as bot
    from dotenv import load_dotenv
    load_dotenv()
    with portfolio_lock():
        if not bot.run_outbox(scan_date):
            raise SystemExit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('scan_date')
    p.add_argument('channel', choices=['telegram', 'x', 'resume'])
    group = p.add_mutually_exclusive_group()
    group.add_argument('--receipt')
    group.add_argument('--not-sent', action='store_true')
    p.add_argument('--evidence', default='')
    p.add_argument('--close-ticker', help='Reconcile a canonical close keyed by signal date')
    p.add_argument('--entry-ticker', help='Reconcile a morning entry announcement keyed by signal date')
    a = p.parse_args()
    if a.channel == 'resume':
        if a.receipt or a.not_sent or a.evidence or a.close_ticker or a.entry_ticker:
            p.error('resume accepts only the archived decision date')
        resume(a.scan_date)
        print('Archived outbox completed.')
        return
    if not (a.receipt or a.not_sent) or not a.evidence:
        p.error('reconciliation requires --receipt or --not-sent and --evidence')
    reconcile(a.scan_date, a.channel, receipt=a.receipt, not_sent=a.not_sent,
              evidence=a.evidence, close_ticker=a.close_ticker, entry_ticker=a.entry_ticker)
    print('Reconciliation saved. No message was sent; resume the pending outbox normally.')


if __name__ == '__main__':
    main()
