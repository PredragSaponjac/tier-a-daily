"""Offline regressions for admission safety and delivery recovery boundaries."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
import alert
import archive
import data_quality
import main
import market_time
import outbox_cli
import position_tracker as PT
import skew_tracker
import vetoes


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(archive, 'ARCHIVE_DIR', self.root / 'signals'))
        self.stack.enter_context(patch.object(PT, 'ROOT', self.root))
        self.stack.enter_context(patch.object(PT, 'OPEN_FILE', self.root / 'open_positions.json'))
        self.stack.enter_context(patch.object(PT, 'RECORD_FILE', self.root / 'track_record.csv'))
        self.stack.enter_context(patch.object(main, 'checkpoint'))
        self.stack.enter_context(patch.object(main.sheet_sync, 'sync_all', return_value=True))
        self.stack.enter_context(patch.dict(os.environ, {'ENABLE_X_AUTOPOST': 'false'}))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.cand = dict(ticker='TEST', scan_date='2026-10-05', spot_close=100.,
                         filter={'score': None, 'raw': {}}, spot_return_pct=-10.,
                         skew_change_5d=-8., near_skew=-8., near_dte=3,
                         put_wall_strike=95., put_wall_oi_change=0,
                         skew=-8., atm_iv=100., noise={'status':'pass','noisy':False})

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def record(self, **kwargs):
        main.record_and_deliver('2026-10-05', [self.cand], 'TEST', 3, '', 'SIGNAL', **kwargs)

    def test_position_failure_after_accepted_send_resumes_without_duplicate(self):
        with patch.object(main, 'send_telegram_status', return_value=('sent', 101)) as send:
            with patch.object(PT, 'add_position', side_effect=OSError('disk full')):
                with self.assertRaises(SystemExit):
                    self.record(taken=[self.cand])
            self.assertEqual(main.prior_state('2026-10-05')[0], 'resume')
            self.assertTrue(main.run_outbox('2026-10-05'))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(PT.list_open()[0]['status'], 'PENDING_ENTRY')

    def test_unknown_telegram_does_not_resend_or_open(self):
        with patch.object(main, 'send_telegram_status', return_value=('unknown', None)) as send:
            with self.assertRaises(SystemExit):
                self.record(taken=[self.cand])
            self.assertFalse(main.run_outbox('2026-10-05'))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(main.prior_state('2026-10-05')[0], 'attention')
            self.assertFalse(PT.list_open())

    def test_checkpoint_failure_prevents_delivery(self):
        with patch.object(main, 'checkpoint', side_effect=RuntimeError('push rejected')):
            with patch.object(main, 'send_telegram_status') as send:
                with self.assertRaises(RuntimeError):
                    self.record()
                send.assert_not_called()

    def test_failure_persisting_inflight_prevents_send(self):
        with patch.object(main, 'checkpoint', side_effect=[None, RuntimeError('push rejected')]):
            with patch.object(main, 'send_telegram_status') as send:
                with self.assertRaises(RuntimeError):
                    self.record()
                send.assert_not_called()
        self.assertEqual(archive.load_archive('2026-10-05')['delivery']['telegram'], 'inflight')

    def test_failed_no_signal_notification_is_retryable(self):
        with patch.object(main, 'send_telegram_status', side_effect=[('rejected', None), ('sent', 102)]) as send:
            with self.assertRaises(SystemExit):
                self.record()
            self.assertTrue(main.run_outbox('2026-10-05'))
            self.assertEqual(send.call_count, 2)

    def test_sheet_failure_keeps_completion_false_and_resumes(self):
        with patch.object(main, 'send_telegram_status', return_value=('sent', 103)) as send:
            with patch.object(main.sheet_sync, 'sync_all', return_value=False):
                with self.assertRaises(SystemExit):
                    self.record(taken=[self.cand])
            self.assertFalse(archive.load_archive('2026-10-05')['delivery']['complete'])
            self.assertTrue(main.run_outbox('2026-10-05'))
            self.assertEqual(send.call_count, 1)

    def test_x_unknown_is_never_blindly_repeated(self):
        with patch.dict(os.environ, {'ENABLE_X_AUTOPOST': 'true'}):
            with patch.object(main, 'send_telegram_status', return_value=('sent', 104)):
                with patch.object(main.x_post, 'already_posted', return_value=None):
                    with patch.object(main.x_post, 'post_to_x_status', return_value=('unknown', None)) as post:
                        with self.assertRaises(SystemExit):
                            self.record(x_text='TEST signal', featured='TEST')
                        self.assertFalse(main.run_outbox('2026-10-05'))
                        self.assertEqual(post.call_count, 1)

    def test_archives_preserve_immutable_inputs_and_typed_features(self):
        self.cand['legs'] = {'tradeable': np.bool_(True)}
        with patch.object(main, 'send_telegram_status', return_value=('sent', 105)):
            self.record()
        original = next((self.root/'signals'/'runs').glob('*.json')).read_bytes()
        archive.update_delivery('2026-10-05', telegram='sent', telegram_id=106)
        self.assertEqual(next((self.root/'signals'/'runs').glob('*.json')).read_bytes(), original)
        rec = archive.load_archive('2026-10-05')
        self.assertIs(rec['candidate_inputs'][0]['legs']['tradeable'], True)
        self.assertEqual(rec['parameters_snapshot']['version'], '2.0.0')
        self.assertGreater(len(list((self.root/'signals'/'events').glob('*.json'))), 0)

    def test_uncertain_untracked_admissions_reserve_next_day_capacity(self):
        with patch.object(main, 'send_telegram_status', return_value=('unknown', None)):
            with self.assertRaises(SystemExit):
                self.record(taken=[self.cand])
        self.assertFalse(PT.list_open())
        self.assertEqual(main.reserved_tickers(), {'TEST'})
        fresh = {'ticker': 'NEW'}
        taken, skipped = main.select_taken([fresh], fresh, main.reserved_tickers(),
            {'take_all_qualified': True, 'max_concurrent': 1}, lambda _: 1)
        self.assertEqual((taken, skipped), ([], ['NEW']))

    def test_verified_receipt_resumes_frozen_decision_without_second_send(self):
        with patch.object(main, 'send_telegram_status', return_value=('unknown', None)) as send:
            with self.assertRaises(SystemExit):
                self.record(taken=[self.cand])
            with patch.object(outbox_cli, 'checkpoint'), patch.object(outbox_cli, 'portfolio_lock', contextlib.nullcontext):
                outbox_cli.reconcile('2026-10-05', 'telegram', receipt='108', evidence='Checked channel')
                with patch('dotenv.load_dotenv'):
                    outbox_cli.resume('2026-10-05')
            self.assertEqual(send.call_count, 1)
        self.assertEqual(PT.list_open()[0]['status'], 'PENDING_ENTRY')
        self.assertTrue(archive.load_archive('2026-10-05')['delivery']['complete'])

    def test_dated_preview_reads_archive_without_provider_queries(self):
        with patch.object(main, 'send_telegram_status', return_value=('sent', 109)):
            self.record()
        before = (self.root/'signals'/'2026-10-05.json').read_bytes()
        with patch('sys.argv', ['main.py', '--scan-date', '2026-10-05']):
            with patch.object(main, 'read_tier_a') as read, patch.object(main, 'load_dotenv') as creds:
                main._main()
                read.assert_not_called()
                creds.assert_not_called()
        self.assertEqual(before, (self.root/'signals'/'2026-10-05.json').read_bytes())

    def test_canonical_close_receipt_uses_monitor_field_without_sending(self):
        PT.add_position(self.cand, 110, 120, 130, 93, 'legacy')
        PT.close_position('TEST', '2026-10-05', 110, 'T1', '2026-10-06')
        PT.set_publication('TEST', '2026-10-05', telegram='unknown')
        with patch.object(outbox_cli, 'checkpoint'), patch.object(outbox_cli, 'portfolio_lock', contextlib.nullcontext):
            outbox_cli.reconcile('2026-10-05', 'telegram', receipt='110', evidence='Checked close', close_ticker='TEST')
        pub = PT.closed_record('TEST', '2026-10-05')['publication']
        self.assertEqual(pub['telegram'], 'sent')
        self.assertEqual(pub['telegram_message_id'], '110')


class GateAndCalendarTests(unittest.TestCase):
    def test_unknown_earnings_blocks(self):
        with patch.object(vetoes.yf, 'Ticker', return_value=SimpleNamespace(calendar={})):
            result = vetoes.check_earnings('TEST', '2026-10-06')
        self.assertFalse(result['pass'])
        self.assertEqual(result['status'], 'unknown')

    def test_partial_options_data_does_not_pass_liquidity(self):
        frame = pd.DataFrame({'openInterest':[1000., None]})
        ticker = SimpleNamespace(options=['2026-10-09'], option_chain=lambda _:SimpleNamespace(calls=frame,puts=frame))
        with patch.object(vetoes.yf, 'Ticker', return_value=ticker):
            result = vetoes.check_liquidity('TEST')
        self.assertFalse(result['pass'])
        self.assertEqual(result['status'], 'unknown')

    def test_missing_noise_database_blocks_without_creating_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'missing.db'
            result = data_quality.assess_noise('TEST', '2026-10-05', str(path))
            self.assertFalse(path.exists())
        self.assertTrue(result['noisy'])
        self.assertEqual(result['status'], 'unknown')

    def test_telegram_timeout_does_not_log_credential_url(self):
        output = io.StringIO()
        with patch.object(alert.requests, 'post', side_effect=RuntimeError('botSECRET URL')):
            with contextlib.redirect_stdout(output):
                result = alert.send_telegram_status('test', 'chat', 'SECRET')
        self.assertEqual(result[0], 'unknown')
        self.assertNotIn('SECRET', output.getvalue())

    def test_holidays_early_close_and_dst(self):
        utc = dt.timezone.utc
        self.assertEqual(market_time.next_session('2026-11-25'), dt.date(2026,11,27))
        self.assertFalse(market_time.session_completed('2026-11-27',dt.datetime(2026,11,27,17,59,tzinfo=utc)))
        self.assertTrue(market_time.session_completed('2026-11-27',dt.datetime(2026,11,27,18,1,tzinfo=utc)))
        self.assertEqual(market_time.now_eastern(dt.datetime(2026,10,30,20,tzinfo=utc)).hour,16)
        self.assertEqual(market_time.now_eastern(dt.datetime(2026,11,2,21,tzinfo=utc)).hour,16)

    def test_nine_session_screen_uses_session_sigma_and_supports_separate_five(self):
        dates = market_time.sessions_between('2026-09-14','2026-09-25')
        history = pd.DataFrame({'date':pd.to_datetime(dates),'spot_close':[120]+[100]*9,
                                'skew':[0]+[-10]*9,'atm_iv':100.,'hv_10d':100.})
        actual = skew_tracker.compute_divergence(history)
        self.assertEqual(actual['window_sessions'],9)
        expected = (-100/6)/(100/np.sqrt(252)*np.sqrt(9))
        self.assertEqual(actual['sigma_iv'],round(expected,2))
        five = skew_tracker.compute_divergence(history,lookback_sessions=5)
        self.assertEqual(five['spot_return_pct'],0)
        self.assertEqual(five['skew_change'],0)
        self.assertIsNone(skew_tracker.compute_divergence(history.drop(index=3)))
        history.loc[0, 'skew'] = float('nan')
        self.assertIsNone(skew_tracker.compute_divergence(history))

    def test_x_success_without_receipt_remains_uncertain(self):
        import x_post
        env = {k:'placeholder' for k in ('X_API_KEY','X_API_SECRET','X_ACCESS_TOKEN','X_ACCESS_SECRET')}
        response = SimpleNamespace(status_code=201, json=lambda: {'data': {}})
        with patch.dict(os.environ, env), patch.object(x_post, '_session') as session:
            session.return_value.post.return_value = response
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(x_post.post_to_x_status('TEST'), ('unknown', None))

    def test_top_only_also_obeys_full_capacity(self):
        c={'ticker':'NEW'}
        taken,skipped=main.select_taken([c],c,{str(x) for x in range(6)},
                                      {'take_all_qualified':False,'max_concurrent':6},lambda _:1)
        self.assertEqual(taken,[])
        self.assertEqual(skipped,['NEW'])


if __name__ == '__main__':
    unittest.main()
