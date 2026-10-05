"""Morning entry announcements (2026-10-04). Fake Telegram/X/Yahoo; temp state only."""
import datetime as dt
import io
import os
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import entry_announce as EA
import monitor
import outbox_cli
import position_tracker as PT

ET = ZoneInfo('America/New_York')
SIGNAL = '2026-09-11'                                  # Friday signal
SESSION = dt.date(2026, 9, 14)                         # entry fills at Monday's open
MORNING = dt.datetime(2026, 9, 14, 9, 46, tzinfo=ET)
AFTER_CLOSE = dt.datetime(2026, 9, 14, 17, 0, tzinfo=ET)


class EntryAnnounceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tg, self.xs = [], []
        self.tg_result, self.x_result = [('sent', 7)], [('posted', 'x1')]
        patches = [patch.object(PT, 'ROOT', self.root),
                   patch.object(PT, 'OPEN_FILE', self.root / 'open_positions.json'),
                   patch.object(PT, 'RECORD_FILE', self.root / 'track_record.csv'),
                   patch.object(EA, 'checkpoint', return_value=None),
                   patch.object(monitor, 'checkpoint', return_value=None),
                   patch.object(EA, 'send_telegram_status',
                                side_effect=lambda m: (self.tg.append(m), self.tg_result[0])[1]),
                   patch.object(EA.x_post, 'post_to_x_status',
                                side_effect=lambda m: (self.xs.append(m), self.x_result[0])[1]),
                   patch.object(EA.x_post, 'already_posted', return_value=None),
                   patch.dict(os.environ, {'ENABLE_X_AUTOPOST': '1'})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)

    def position(self, policy='next_regular_open', ticker='TEST'):
        PT.add_position({'ticker': ticker, 'scan_date': SIGNAL, 'spot_close': 100},
                        110, 111, 120, 93, 'test', entry_policy=policy)

    def ann(self, ticker='TEST'):
        return next(p for p in PT.list_open() if p['ticker'] == ticker).get(EA.FIELD) or {}

    def run_at(self, when, opening=50.0, dry_run=False):
        with patch.object(EA, 'opening_price', return_value=opening), redirect_stdout(io.StringIO()):
            return EA.announce_entries(dry_run=dry_run, asof=when)

    def test_announces_once_after_the_open_with_bracket_prices(self):
        self.position()
        self.assertEqual(self.run_at(MORNING), 1)
        self.assertEqual((len(self.tg), len(self.xs)), (1, 1))
        msg = self.tg[0]
        self.assertIn('$50.00', msg)                     # entry = the opening print
        self.assertIn('$55.00', msg)                     # +10% target from the OPEN
        self.assertIn('$46.50', msg)                     # -7% stop from the OPEN
        self.assertEqual(self.xs[0].count('$TEST'), 1)   # one cashtag (X rejects two)
        for text in (msg, self.xs[0]):                   # the morning post claims nothing it cannot know
            self.assertNotIn('Late post', text)
            self.assertNotIn('confirmed', text)
        a = self.ann()
        self.assertEqual((a['telegram'], a['x'], a['open']), ('sent', 'posted', 50.0))
        self.assertEqual(self.run_at(MORNING + dt.timedelta(minutes=35)), 0)   # the 10:20 backup run
        self.assertEqual((len(self.tg), len(self.xs)), (1, 1))   # nothing posted twice

    def test_nothing_before_the_open_or_before_the_entry_session(self):
        self.position()
        self.assertEqual(self.run_at(dt.datetime(2026, 9, 14, 9, 20, tzinfo=ET)), 0)
        self.assertEqual(self.run_at(dt.datetime(2026, 9, 11, 17, 0, tzinfo=ET)), 0)   # signal evening
        self.assertEqual((len(self.tg), self.ann()), (0, {}))

    def test_missing_opening_print_waits_for_a_later_run(self):
        self.position()
        self.assertEqual(self.run_at(MORNING, opening=None), 0)
        self.assertEqual((len(self.tg), self.ann()), (0, {}))
        self.run_at(MORNING + dt.timedelta(minutes=35), opening=50.0)
        self.assertEqual(self.ann()['telegram'], 'sent')

    def test_after_close_catch_up_uses_the_official_open(self):
        self.position()
        PT.activate_position('TEST', SIGNAL, SESSION.isoformat(), 51.0)
        self.run_at(AFTER_CLOSE, opening=99.0)           # the live read must NOT be used
        self.assertIn('$51.00', self.tg[0])
        self.assertIn('confirmed from the completed daily bar', self.tg[0])
        for text in (self.tg[0], self.xs[0]):            # a catch-up post says it is late
            self.assertIn('Late post', text)
        self.assertEqual(self.ann()['source'], 'official')

    def test_rejected_telegram_is_retried_but_unknown_is_never_resent(self):
        self.position()
        self.tg_result[0] = ('rejected', None)
        self.run_at(MORNING)
        self.assertEqual(self.ann()['telegram'], 'failed')
        self.tg_result[0] = ('sent', 9)
        self.run_at(MORNING + dt.timedelta(minutes=35))
        self.assertEqual((self.ann()['telegram'], len(self.tg)), ('sent', 2))
        self.position(ticker='UNK')
        self.tg_result[0] = ('unknown', None)
        self.run_at(MORNING)
        self.run_at(MORNING + dt.timedelta(minutes=35))
        self.assertEqual(self.ann('UNK')['telegram'], 'unknown')
        self.assertEqual(sum('UNK' in m for m in self.tg), 1)

    def test_post_already_in_x_log_is_not_posted_again(self):
        self.position()
        with patch.object(EA.x_post, 'already_posted', return_value='x9'):
            self.run_at(MORNING)
        self.assertEqual((len(self.xs), self.ann()['x'], self.ann()['x_id']), (0, 'posted', 'x9'))

    def test_autopost_off_saves_a_draft_instead_of_posting(self):
        self.position()
        with patch.dict(os.environ, {'ENABLE_X_AUTOPOST': '0'}):
            self.run_at(MORNING)
        self.assertEqual((len(self.xs), self.ann()['x']), (0, 'draft'))
        self.assertTrue((self.root / self.ann()['x_draft']).exists())

    def test_legacy_positions_are_never_announced(self):
        self.position(policy='legacy_close')
        self.assertEqual(self.run_at(MORNING), 0)
        self.assertEqual(len(self.tg), 0)

    def test_practice_run_sends_and_saves_nothing(self):
        self.position()
        before = PT.OPEN_FILE.read_bytes()
        self.assertEqual(self.run_at(MORNING, dry_run=True), 1)
        self.assertEqual((len(self.tg), len(self.xs)), (0, 0))
        self.assertEqual(before, PT.OPEN_FILE.read_bytes())

    def test_monitor_flags_an_announced_open_that_differs_from_the_official_one(self):
        self.position()
        self.run_at(MORNING, opening=50.0)
        frame = pd.DataFrame([[52.0, 53.0, 51.0, 52.5, 0.0, 0.0]], index=pd.to_datetime([SESSION]),
                             columns=['Open', 'High', 'Low', 'Close', 'Stock Splits', 'Dividends'])
        pos = next(p for p in PT.list_open() if p['ticker'] == 'TEST')
        with patch.object(monitor.yf.Ticker, 'history', return_value=frame), redirect_stdout(io.StringIO()):
            monitor.check_position(pos, asof=AFTER_CLOSE)
        self.assertIn('differs', next(p for p in PT.list_open() if p['ticker'] == 'TEST')['entry_note'])

    def test_uncertain_entry_post_can_be_reconciled_without_sending(self):
        self.position()
        self.tg_result[0] = ('unknown', None)
        self.run_at(MORNING)
        with patch.object(outbox_cli, 'portfolio_lock', return_value=nullcontext()), \
                patch.object(outbox_cli, 'checkpoint', return_value=None):
            outbox_cli.reconcile(SIGNAL, 'telegram', receipt='77', evidence='seen in the channel',
                                 entry_ticker='TEST')
        self.assertEqual((self.ann()['telegram'], self.ann()['telegram_message_id']), ('sent', '77'))
        self.assertEqual(len(self.tg), 1)

    def test_heartbeat_flags_a_filled_entry_that_was_never_announced(self):
        import db_state
        filled = {'status': 'OPEN', 'entry_policy': 'next_regular_open'}
        self.assertFalse(db_state._entry_unannounced({'status': None, 'entry_policy': None}))   # old record
        self.assertFalse(db_state._entry_unannounced({'status': 'OPEN', 'entry_policy': 'legacy_close'}))
        self.assertFalse(db_state._entry_unannounced({**filled, 'status': 'PENDING_ENTRY'}))   # not yet open
        self.assertTrue(db_state._entry_unannounced(filled))                                  # every run failed
        for tg, x, flagged in (('sent', 'posted', False), ('sent', 'draft', False),
                               ('unknown', 'posted', True), ('sent', 'gave_up', True)):
            self.assertEqual(db_state._entry_unannounced(
                {**filled, 'entry_announcement': {'telegram': tg, 'x': x}}), flagged)


if __name__ == '__main__':
    unittest.main()
