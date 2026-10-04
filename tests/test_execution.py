"""Execution/recovery regressions. No real providers, publications, or portfolio files."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import backtest
import exit_model
import exits
import excursions
import manual_trades
import monitor
import position_tracker as PT

ET = ZoneInfo('America/New_York')
AFTER_CLOSE = dt.datetime(2026, 9, 14, 17, tzinfo=ET)


def bars(rows, dates=None):
    dates = ['2026-09-14'] if dates is None else dates
    return pd.DataFrame(rows, index=pd.to_datetime(dates),
                        columns=['Open', 'High', 'Low', 'Close', 'Stock Splits', 'Dividends'])


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(PT, 'ROOT', self.root),
                        patch.object(PT, 'OPEN_FILE', self.root / 'open_positions.json'),
                        patch.object(PT, 'RECORD_FILE', self.root / 'track_record.csv'),
                        patch.object(monitor, 'checkpoint', return_value=None)]
        for p in self.patches:
            p.start()
        self.addCleanup(self.temp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

    def position(self, policy='legacy_close', ticker='TEST', day='2026-09-11'):
        PT.add_position({'ticker': ticker, 'scan_date': day, 'spot_close': 100},
                        110, 111, 120, 93, 'test', entry_policy=policy)
        return PT.list_open()[0]

    def provider(self, frame):
        return patch.object(monitor.yf.Ticker, 'history', return_value=frame)

    def test_evolving_daily_bar_never_closes_before_session_completion(self):
        p = self.position()
        prefix = bars([[100, 112, 100, 111, 0, 0]])
        before = PT.OPEN_FILE.read_bytes()
        with self.provider(prefix):
            result = monitor.check_position(p, asof=dt.datetime(2026, 9, 14, 10, tzinfo=ET))
        self.assertIsNone(result)
        self.assertEqual(before, PT.OPEN_FILE.read_bytes())
        final = bars([[100, 112, 90, 95, 0, 0]])
        with self.provider(final):
            result = monitor.check_position(p, asof=AFTER_CLOSE)
        self.assertEqual(result['reason'], 'STOP')
        self.assertIn('AMBIGUOUS', result['record']['note'])
        self.assertEqual(exits.walk_bars(final, 100)['outcome'], 'STOP')

    def test_gap_fill_and_all_legacy_engines_use_same_completed_bar(self):
        gap = bars([[80, 90, 75, 85, 0, 0]])
        with self.provider(gap), patch.object(exit_model.yf if hasattr(exit_model, 'yf') else monitor.yf,
                                              'download', return_value=gap):
            replay = backtest.simulate_trade('TEST', '2026-09-11', 100, 10, -7, asof=AFTER_CLOSE)
            sheet = exit_model.model_exits('TEST', '2026-09-11', 100, asof=AFTER_CLOSE)
        self.assertAlmostEqual(replay['return_pct'], -20)
        self.assertAlmostEqual(sheet['return_pct'], -20)
        dual = bars([[100, 112, 90, 95, 0, 0]])
        with self.provider(dual), patch.object(monitor.yf, 'download', return_value=dual):
            self.assertEqual(backtest.simulate_trade('TEST', '2026-09-11', 100, 10, -7,
                                                    asof=AFTER_CLOSE)['outcome'], 'STOP')
            self.assertEqual(exit_model.model_exits('TEST', '2026-09-11', 100,
                                                   asof=AFTER_CLOSE)['outcome'], 'LOSS')

    def test_no_time_expiry_and_open_marks_are_not_realized(self):
        dates = pd.bdate_range('2026-08-03', periods=26)
        data = bars([[100, 101, 99, 100, 0, 0]] * 25 + [[100, 111, 99, 110, 0, 0]], dates)
        complete = exits.walk_bars(data, 100)
        self.assertEqual(complete['days_in'], 26)
        self.assertEqual(complete['outcome'], 'TP1')
        censored = exits.walk_bars(data.iloc[:25], 100)
        self.assertFalse(censored['complete'])
        self.assertIsNone(censored['return_pct'])
        self.assertEqual(censored['mark_return_pct'], 0)

    def test_opposite_extrema_remain_bounds_and_ambiguous_mfe_is_not_open(self):
        target = exits.walk_bars(bars([[100, 112, 96, 97, 0, 0]]), 100)['excursion_bounds']
        self.assertAlmostEqual(target['mae_pct_min'], -4)
        self.assertAlmostEqual(target['mae_pct_max'], 0)
        self.assertFalse(target['exact'])
        stop = exits.walk_bars(bars([[100, 108, 90, 106, 0, 0]]), 100)['excursion_bounds']
        self.assertAlmostEqual(stop['mfe_pct_min'], 0)
        self.assertAlmostEqual(stop['mfe_pct_max'], 8)
        dual = exits.walk_bars(bars([[100, 112, 90, 95, 0, 0]]), 100)['excursion_bounds']
        self.assertAlmostEqual(dual['mfe_pct_max'], 10)
        self.assertAlmostEqual(dual['mfe_pct_min'], 0)

    def test_research_observation_limit_fetches_later_actions_before_censoring(self):
        data = bars([[50, 51, 49, 50, 0, 0], [50, 51, 49, 50, 0, 0], [50, 51, 49, 50, 2, 0]],
                    ['2026-09-14', '2026-09-15', '2026-09-16'])
        asof = dt.datetime(2026, 9, 16, 17, tzinfo=ET)
        with self.provider(data), patch.object(monitor.yf, 'download', return_value=data) as provider:
            replay = backtest.simulate_trade('TEST', '2026-09-11', 100, 10, -7, max_days=1, asof=asof)
            sheet = exit_model.model_exits('TEST', '2026-09-11', 100, window_days=4, asof=asof)
        self.assertEqual(provider.call_args.kwargs['end'], dt.date(2026, 9, 17))
        self.assertEqual(replay['entry'], 50)
        self.assertEqual(sheet['entry_price'], 50)
        self.assertFalse(replay['complete'])
        self.assertFalse(sheet['complete'])
        self.assertIsNone(replay['return_pct'])
        self.assertIsNone(sheet['return_pct'])

    def test_decimal_barrier_equality_does_not_miss_exit(self):
        self.assertEqual(exits.walk_bars(bars([[100, 110, 99, 109, 0, 0]]), 100)['outcome'], 'TP1')
        self.assertEqual(exits.walk_bars(bars([[100, 101, 93, 94, 0, 0]]), 100)['outcome'], 'STOP')

    def test_new_entry_uses_next_open_and_stable_signal_identity(self):
        p = self.position('next_regular_open')
        first = bars([[110, 123, 108, 120, 0, 0]])
        with self.provider(first):
            result = monitor.check_position(p, asof=AFTER_CLOSE)
        record = result['record']
        self.assertEqual(record['entry_price'], 110)
        self.assertEqual(record['signal_date'], '2026-09-11')
        self.assertEqual(record['entry_date'], '2026-09-14')
        self.assertEqual(record['trade_id'], 'TEST:2026-09-11')
        self.assertAlmostEqual(record['exit_price'], 121)
        self.assertFalse(PT.add_position({'ticker': 'TEST', 'scan_date': '2026-09-11', 'spot_close': 100},
                                        110, 111, 120, 93, 'test', entry_policy='next_regular_open'))

    def test_pending_and_open_preview_do_not_write(self):
        p = self.position('next_regular_open')
        before = PT.OPEN_FILE.read_bytes()
        with self.provider(bars([[110, 123, 108, 120, 0, 0]])):
            self.assertTrue(monitor.check_position(p, dry_run=True, asof=AFTER_CLOSE)['closed'])
        self.assertEqual(before, PT.OPEN_FILE.read_bytes())
        self.assertFalse((self.root / 'closed_trades.json').exists())

    def test_split_basis_is_recomputed_from_original_once(self):
        p = self.position()
        data = bars([[50, 51, 49, 50, 2, 0]])
        with self.provider(data):
            self.assertIsNone(monitor.check_position(p, asof=AFTER_CLOSE))
            self.assertIsNone(monitor.check_position(PT.list_open()[0], asof=AFTER_CLOSE))
        result = PT.list_open()[0]
        self.assertEqual(result['original_entry_price'], 100)
        self.assertEqual(result['entry_price'], 50)
        self.assertEqual(result['T1'], 55)
        self.assertEqual(result['split_factor'], 2)

    def test_split_on_unfinished_today_rebases_completed_prior_history(self):
        p = self.position()
        data = bars([[50, 51, 49, 50, 0, 0], [50, 51, 49, 50, 2, 0]],
                    ['2026-09-14', '2026-09-15'])
        with self.provider(data):
            result = monitor.check_position(p, asof=dt.datetime(2026, 9, 15, 10, tzinfo=ET))
        self.assertIsNone(result)
        self.assertEqual(PT.list_open()[0]['entry_price'], 50)
        self.assertEqual(PT.list_open()[0]['split_factor'], 2)

    def test_missing_entry_session_or_action_columns_never_mutates_book(self):
        p = self.position('next_regular_open')
        before = PT.OPEN_FILE.read_bytes()
        late = bars([[110, 123, 108, 120, 0, 0]], ['2026-09-15'])
        with self.provider(late), self.assertRaisesRegex(ValueError, 'incomplete'):
            monitor.check_position(p, asof=dt.datetime(2026, 9, 15, 17, tzinfo=ET))
        self.assertEqual(before, PT.OPEN_FILE.read_bytes())
        no_actions = bars([[100, 101, 99, 100, 0, 0]]).drop(columns=['Stock Splits'])
        with self.provider(no_actions), self.assertRaisesRegex(ValueError, 'corporate actions'):
            monitor.check_position(p, asof=AFTER_CLOSE)
        self.assertEqual(before, PT.OPEN_FILE.read_bytes())

    def test_missing_middle_session_cannot_skip_hidden_exit(self):
        p = self.position()
        before = PT.OPEN_FILE.read_bytes()
        data = bars([[100, 101, 99, 100, 0, 0], [100, 111, 99, 110, 0, 0]],
                    ['2026-09-14', '2026-09-16'])
        with self.provider(data), self.assertRaisesRegex(ValueError, 'incomplete'):
            monitor.check_position(p, asof=dt.datetime(2026, 9, 16, 17, tzinfo=ET))
        self.assertEqual(before, PT.OPEN_FILE.read_bytes())

    def test_corrupt_record_keeps_position_open_and_conflicting_retry_returns_canonical(self):
        self.position()
        closed = self.root / 'closed_trades.json'
        closed.write_text('{bad')
        with self.assertRaises(PT.PortfolioStateError):
            PT.close_position('TEST', '2026-09-11', 110, 'TP1', '2026-09-14')
        self.assertEqual(len(PT.list_open()), 1)
        closed.unlink()
        with patch.object(PT, '_save_open', side_effect=OSError('interrupted removal')):
            with self.assertRaises(OSError):
                PT.close_position('TEST', '2026-09-11', 110, 'TP1', '2026-09-14')
        canon = PT.close_position('TEST', '2026-09-11', 93, 'STOP', '2026-09-15')
        self.assertEqual(canon['exit_reason'], 'TP1')
        self.assertEqual(canon['exit_price'], 110)
        self.assertEqual(len(json.loads(closed.read_text())), 1)
        self.assertEqual(PT.list_open(), [])

    def close(self):
        self.position()
        return PT.close_position('TEST', '2026-09-11', 110, 'TP1', '2026-09-14')

    def test_close_send_is_checkpointed_inflight_and_ack_persisted(self):
        self.close()
        def telegram(msg):
            self.assertEqual(PT.closed_record('TEST', '2026-09-11')['publication']['telegram'], 'inflight')
            return 'sent', 'tg-id'
        def twitter(msg):
            self.assertEqual(PT.closed_record('TEST', '2026-09-11')['publication']['x'], 'inflight')
            return 'posted', 'x-id'
        with patch.object(monitor, 'send_telegram_status', side_effect=telegram), \
             patch.object(monitor.x_post, 'already_posted', return_value=None), \
             patch.object(monitor.x_post, 'post_to_x_status', side_effect=twitter):
            monitor.publish_pending()
        pub = PT.closed_record('TEST', '2026-09-11')['publication']
        self.assertEqual(pub['telegram'], 'sent')
        self.assertEqual(pub['x'], 'posted')
        self.assertEqual(PT.pending_publications(), [])

    def test_interrupted_ack_never_resends_uncertain_telegram(self):
        self.close()
        original = PT.set_publication
        def failed_ack(ticker, key, **fields):
            if fields.get('telegram') == 'sent':
                raise OSError('lost acknowledgement')
            return original(ticker, key, **fields)
        with patch.object(PT, 'set_publication', side_effect=failed_ack), \
             patch.object(monitor, 'send_telegram_status', return_value=('sent', 'tg-id')) as sent:
            with self.assertRaises(OSError):
                monitor.publish_pending()
            self.assertEqual(sent.call_count, 1)
        with patch.object(monitor, 'send_telegram_status') as sent, \
             patch.object(monitor.x_post, 'already_posted', return_value='receipt'):
            monitor.publish_pending()
            sent.assert_not_called()
        self.assertEqual(PT.closed_record('TEST', '2026-09-11')['publication']['telegram'], 'inflight')

    def test_checkpoint_failure_prevents_external_send(self):
        self.close()
        with patch.object(monitor, 'checkpoint', side_effect=OSError('push failed')), \
             patch.object(monitor, 'send_telegram_status') as sent:
            with self.assertRaises(OSError):
                monitor.publish_pending()
            sent.assert_not_called()

    def test_close_recovery_precedes_unrelated_price_feed_failure(self):
        self.close()
        self.position(ticker='OTHER')
        with patch('sys.argv', ['monitor.py']), \
             patch.object(monitor, 'portfolio_lock', return_value=nullcontext()), \
             patch.object(monitor, 'load_dotenv'), \
             patch.object(monitor, 'send_telegram_status', return_value=('sent', 'tg-id')) as telegram, \
             patch.object(monitor.x_post, 'already_posted', return_value=None), \
             patch.object(monitor.x_post, 'post_to_x_status', return_value=('posted', 'x-id')) as twitter, \
             patch.object(monitor, 'check_position', side_effect=ValueError('unrelated feed incomplete')):
            with self.assertRaises(ValueError):
                monitor.main()
        self.assertEqual(telegram.call_count, 1)
        self.assertEqual(twitter.call_count, 1)
        self.assertEqual(PT.closed_record('TEST', '2026-09-11')['publication']['telegram'], 'sent')

    def test_manual_x_mode_saves_paper_close_draft_without_send(self):
        self.position('next_regular_open')
        PT.activate_position('TEST', '2026-09-11', '2026-09-14', 110)
        PT.close_position('TEST', '2026-09-11', 121, 'TP1', '2026-09-14')
        with patch.dict(monitor.os.environ, {'ENABLE_X_AUTOPOST': 'false'}), \
             patch.object(monitor, 'send_telegram_status', return_value=('sent', 'tg-id')), \
             patch.object(monitor.os, 'makedirs'), patch.object(PT, '_atomic_write', wraps=PT._atomic_write) as write, \
             patch.object(monitor.x_post, 'post_to_x_status') as sent:
            # Draft filesystem effects stay inside this test's temporary directory.
            original = Path.cwd()
            try:
                monitor.os.chdir(self.root)
                (self.root / 'x_drafts').mkdir()
                monitor.publish_pending()
            finally:
                monitor.os.chdir(original)
        sent.assert_not_called()
        self.assertEqual(PT.closed_record('TEST', '2026-09-11')['publication']['x'], 'draft')
        self.assertEqual(PT.unresolved_publications(), [])

    def test_manual_excursions_exclude_pre_entry_and_post_exit_dates(self):
        data = bars([[100, 130, 90, 100, 0, 0], [100, 103, 99, 102, 0, 0],
                     [102, 112, 98, 110, 0, 0], [110, 500, 5, 100, 0, 0]],
                    ['2026-09-11', '2026-09-14', '2026-09-15', '2026-09-16'])
        with patch.object(monitor.yf, 'download', return_value=data) as provider:
            result = excursions.compute_excursion('TEST', '2026-09-11', 100, '2026-09-15', 105,
                                                  asof=dt.datetime(2026, 9, 15, 17, tzinfo=ET))
        self.assertEqual(provider.call_args.kwargs['start'], dt.date(2026, 9, 12))
        self.assertEqual(provider.call_args.kwargs['end'], dt.date(2026, 9, 16))
        b = result['excursion_bounds']
        self.assertAlmostEqual(b['mfe_pct_min'], 5)
        self.assertAlmostEqual(b['mfe_pct_max'], 12)
        self.assertIsNone(excursions.compute_excursion('TEST', '2026-09-11', 100))
        self.assertIn('legacy measurement', excursions.format_excursion_block([{'ticker': 'OLD'}]))

    def test_manual_historical_fills_and_exit_bounds_share_later_split_basis(self):
        data = bars([[50, 52, 49, 51, 0, 0], [51, 56, 49, 55, 0, 0], [55, 60, 50, 57, 2, 0]],
                    ['2026-09-14', '2026-09-15', '2026-09-16'])
        with patch.object(monitor.yf, 'download', return_value=data):
            result = excursions.compute_excursion('TEST', '2026-09-11', 100, '2026-09-15', 110,
                                                  asof=dt.datetime(2026, 9, 16, 17, tzinfo=ET))
        self.assertEqual(result['normalized_entry_price'], 50)
        self.assertEqual(result['normalized_exit_price'], 55)
        self.assertAlmostEqual(result['excursion_bounds']['mfe_pct_max'], 12)

    def test_manual_interrupted_removal_uses_canonical_record_without_provider(self):
        opened, closed = self.root / 'open_trades.json', self.root / 'closed_trades.json'
        opened.write_text(json.dumps([{'ticker': 'TEST', 'entry_date': '2026-09-11',
                                       'entry_price': 100, 'status': 'OPEN'}]))
        canonical = {'ticker': 'TEST', 'entry_date': '2026-09-11', 'exit_price': 110, 'outcome': 'WIN'}
        closed.write_text(json.dumps([canonical]))
        with patch.object(manual_trades, 'OPEN_FILE', str(opened)), \
             patch.object(manual_trades, 'CLOSED_FILE', str(closed)), \
             patch.object(excursions, 'compute_excursion') as provider:
            result = manual_trades._close_trade('TEST', 'bad new date', -1, 'LOSS')
        self.assertEqual(result, canonical)
        self.assertEqual(json.loads(opened.read_text()), [])
        provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
