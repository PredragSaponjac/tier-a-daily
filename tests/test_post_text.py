"""Published post text (2026-10-13). Each case was visible in a real post."""
import copy
import re
import unittest
from unittest.mock import patch

import alert
import excursions
import x_post

# The archived 2026-10-13 UUUU candidate, trimmed to what the formatters read.
UUUU = {
    'ticker': 'UUUU', 'scan_date': '2026-10-13', 'spot_close': 9.27, 'spot_return_pct': -15.96,
    'skew_change_5d': -7.03, 'near_skew': -7.03, 'skew': -15.23, 'atm_iv': 71.88, 'near_dte': 3,
    'put_wall_strike': 9.0, 'sector': 'Energy',
    'filter': {'score': None, 'conditions': {}, 'raw': {}, 'error': 'no options-volume data'},
    'vetoes': {'pass': True, 'status': 'pass', 'reasons': [],
               'details': {'earnings': {'pass': True, 'status': 'pass', 'next_earnings': '2026-11-02'}}},
    'legs': {'cushion_pct': 3.0, 'vol_cushion': 0.66, 'strong_skew': True, 'strong_cushion': False,
             'tradeable': True, 'reason': 'strong leg: skew -15'},
    'noise': {'skew_std': 5.1, 'noisy': False, 'status': 'pass'},
    'edge': {'sector_iv_rank': 97.0, 'skew_slope': -0.97, 'iv_hv_ratio': 1.34, 'combo_pass': False,
             'ivr_pass': True, 'atr_pct': 5.3, 'stop_atr': 1.32, 'tight_stop': False, 'atr2x_stop_pct': 10.6},
}


def other(ticker, score=None):
    c = copy.deepcopy(UUUU)
    c['ticker'] = ticker
    c['filter'] = {**c['filter'], 'score': score}
    return c


class PostTextTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(excursions, 'load', return_value=[])     # never read the live record
        p.start()
        self.addCleanup(p.stop)

    def test_next_update_names_the_v2_scan_time_and_skips_weekends_and_holidays(self):
        self.assertEqual(alert._next_trading_day_phrase('2026-10-13'),
                         'Next update: tomorrow after the 3:05 PM CT scan.')
        self.assertIn('Monday', alert._next_trading_day_phrase('2026-10-09'))
        self.assertIn('Friday', alert._next_trading_day_phrase('2026-11-25'))   # Thanksgiving closed

    def test_watch_list_prints_a_uw_score_only_when_one_was_measured(self):
        unmeasured = alert.format_watch_list([other('JMIA')])
        self.assertIn('JMIA', unmeasured)
        for fake in ('UW 0/4', '☆', 'None'):
            self.assertNotIn(fake, unmeasured)
        self.assertIn('UW 2/4 ⭐⭐☆☆', alert.format_watch_list([other('JMIA', score=2)]))

    def test_runner_up_never_shows_score_none(self):
        text = alert.format_signal(UUUU, [other('JMIA'), other('REPL')], taken=[])
        self.assertIn("(Today's runner-up: $JMIA)", text)
        self.assertNotIn('None/4', text)
        self.assertIn('score 3/4', alert.format_signal(UUUU, [other('JMIA', 3), other('REPL')], taken=[]))

    def test_x_signal_post_has_no_unavailable_lines_and_one_cashtag(self):
        old = [{'ticker': f'OLD{i}', 'outcome': 'WIN', 'result_pct': 10.0, 'entry_date': '2026-05-01'}
               for i in range(18)]
        measured = [{'ticker': 'NEW', 'outcome': 'WIN', 'result_pct': 10.0, 'entry_date': '2026-09-01',
                     'excursion_bounds': {'mae_pct_min': -2.0, 'mae_pct_max': -2.0,
                                          'mfe_pct_min': 10.0, 'mfe_pct_max': 12.5}}]
        with patch.object(excursions, 'load', return_value=old + measured):
            text = x_post.format_signal_for_x(UUUU, [], taken=[])
        self.assertNotIn('unavailable', text)
        self.assertIn('NEW: worst -2.0%, best +10.0% to +12.5%', text)
        self.assertEqual(sum(line.startswith('Not measured:') for line in text.splitlines()), 1)
        self.assertEqual(re.findall(r'\$[A-Z]{1,5}\b', text), ['$UUUU'])    # X rejects 2+ cashtags
        self.assertLess(len(text), 4000)                             # X Premium post limit

    def test_close_post_prints_one_number_when_the_bounds_agree(self):
        text = x_post.format_close_for_x('UUUU', 9.30, 10.23, 'TP1', '2026-10-14', '2026-10-20',
                                         excursion_bounds={'mae_pct_min': -3.2, 'mae_pct_max': -3.2,
                                                           'mfe_pct_min': 10.0, 'mfe_pct_max': 11.4})
        self.assertIn('Adverse excursion: -3.2% (daily-bar bounds)', text)
        self.assertIn('Favorable excursion: +10.0% to +11.4% (daily-bar bounds)', text)


if __name__ == '__main__':
    unittest.main()
