"""Prospective tracker (registered 2026-10-22): features frozen, outcomes refresh, nothing implemented."""
import datetime as dt
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import prospective_tracker as PT

ROOT = Path(__file__).resolve().parents[1]
PRE = '2026-10-13'      # before registration -> context only   (d0 = 2026-09-30)
PRO = '2026-11-02'      # after registration  -> evidence        (d0 = 2026-10-20)


def make_db(path, rows, paths):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE skew_daily(ticker TEXT, date TEXT, spot_close REAL, put_wall_strike REAL, put_wall_oi INTEGER, put_25d_iv REAL)')
    con.execute('CREATE TABLE tier_a_paths(ticker TEXT, scan_date TEXT, outcome TEXT, r_live REAL)')
    con.executemany('INSERT INTO skew_daily VALUES (?,?,?,?,?,?)', rows)
    con.executemany('INSERT INTO tier_a_paths VALUES (?,?,?,?)', paths)
    con.commit()
    con.close()


class ProspectiveTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reg = self.tmp / 'prospective_hypotheses.json'
        shutil.copy(ROOT / 'prospective_hypotheses.json', self.reg)
        self.ledger = self.tmp / 'ledger.csv'
        self.db = str(self.tmp / 'skew.db')

    def run_update(self, today='2026-11-06', dry_run=False):
        with redirect_stdout(io.StringIO()):
            return PT.update(self.db, self.reg, self.ledger, today=dt.date.fromisoformat(today), dry_run=dry_run)

    def test_session_before_counts_exchange_sessions_only(self):
        self.assertEqual(PT.session_before(dt.date(2026, 11, 2)), dt.date(2026, 10, 20))
        self.assertEqual(PT.session_before(dt.date(2026, 12, 1)), dt.date(2026, 11, 17))   # skips Thanksgiving
        self.assertIsNone(PT.session_before(dt.date(2026, 11, 1)))                          # a Sunday

    def test_features_follow_the_registered_definitions(self):
        make_db(self.db, [
            ('HELD', '2026-10-20', 10, 9.0, 100, 80.0), ('HELD', PRO, 8.5, 9.0, 150, 70.0),    # wall held, OI up, put IV down
            ('ROLL', '2026-10-20', 10, 9.0, 100, 80.0), ('ROLL', PRO, 8.5, 8.0, 90, 70.0),     # wall rolled, OI down
            ('IVUP', '2026-10-20', 10, 9.0, 100, 80.0), ('IVUP', PRO, 8.5, 9.5, 150, 90.0),    # OI up but puts got dearer
            ('FLAT', '2026-10-20', 10, 9.0, 100, 80.0), ('FLAT', PRO, 8.5, 9.0, 100, 70.0),    # OI unchanged: no footprint
            ('GAP', PRO, 8.5, 9.0, 150, 70.0),                                                  # no reading 9 sessions earlier
        ], [('HELD', PRO, 'OPEN', None), ('ROLL', PRO, 'OPEN', None), ('IVUP', PRO, 'OPEN', None),
            ('FLAT', PRO, 'OPEN', None), ('GAP', PRO, 'OPEN', None)])
        self.run_update()
        led = PT.load_ledger(self.ledger)
        self.assertEqual((led[('HELD', PRO)]['p1_wall_held'], led[('HELD', PRO)]['p2_put_sale_footprint']), ('1', '1'))
        self.assertEqual((led[('ROLL', PRO)]['p1_wall_held'], led[('ROLL', PRO)]['p2_put_sale_footprint']), ('0', '0'))
        self.assertEqual((led[('IVUP', PRO)]['p1_wall_held'], led[('IVUP', PRO)]['p2_put_sale_footprint']), ('1', '0'))
        self.assertEqual(led[('FLAT', PRO)]['p2_put_sale_footprint'], '0')
        self.assertEqual((led[('GAP', PRO)]['data_ok'], led[('GAP', PRO)]['p1_wall_held']), ('0', ''))
        self.assertEqual(led[('HELD', PRO)]['d0'], '2026-10-20')

    def test_a_signal_on_the_registration_day_counts_as_prospective(self):
        make_db(self.db, [('DAY', '2026-10-09', 10, 9.0, 100, 80.0), ('DAY', '2026-10-22', 8.5, 9.0, 150, 70.0)],
                [('DAY', '2026-10-22', 'OPEN', None)])
        _, new_rows, _ = self.run_update(today='2026-10-23')
        self.assertEqual((new_rows, PT.load_ledger(self.ledger)[('DAY', '2026-10-22')]['cohort']), (1, 'prospective'))

    def test_rows_before_registration_are_context_not_evidence(self):
        make_db(self.db, [('OLD', '2026-09-30', 10, 9.0, 100, 80.0), ('OLD', PRE, 8.5, 9.0, 150, 70.0),
                          ('NEW', '2026-10-20', 10, 9.0, 100, 80.0), ('NEW', PRO, 8.5, 9.0, 150, 70.0)],
                [('OLD', PRE, 'T1', 1.43), ('NEW', PRO, 'T1', 1.43)])
        text, new_rows, summary = self.run_update()
        led = PT.load_ledger(self.ledger)
        self.assertEqual((led[('OLD', PRE)]['cohort'], led[('NEW', PRO)]['cohort']), ('pre_registration', 'prospective'))
        self.assertEqual(new_rows, 1)
        s = summary['P1_put_wall_held_through_washout']
        self.assertEqual((s['prospective']['n_total'], s['pre_registration_context']['n_total']), (1, 1))

    def test_weekly_message_leaves_out_legacy_context(self):
        make_db(self.db, [('A', '2026-09-30', 10, 9.0, 100, 80.0), ('A', PRE, 8.5, 9.0, 150, 70.0),   # footprint
                          ('B', '2026-09-30', 10, 9.0, 100, 80.0), ('B', PRE, 8.5, 8.0, 90, 70.0)],   # none
                [('A', PRE, 'T1', 1.43), ('B', PRE, 'STOP', -1.0)])
        text, _, summary = self.run_update()
        self.assertIn('diff_pts', summary['P2_opening_put_sale_footprint']['pre_registration_context'])
        self.assertNotIn('Pre-registration', text)
        status = json.loads(self.reg.read_text(encoding='utf-8'))['status']
        self.assertIn('legacy labels', status['pre_registration_context_note'])

    def test_features_are_frozen_while_outcomes_refresh(self):
        make_db(self.db, [('T', '2026-10-20', 10, 9.0, 100, 80.0), ('T', PRO, 8.5, 9.0, 150, 70.0)], [('T', PRO, 'OPEN', None)])
        self.run_update(today='2026-11-06')
        con = sqlite3.connect(self.db)
        con.execute("UPDATE skew_daily SET put_wall_strike=5 WHERE ticker='T' AND date=?", (PRO,))   # a later revision
        con.execute("UPDATE tier_a_paths SET outcome='T1', r_live=1.43")
        con.commit(); con.close()
        self.run_update(today='2026-11-13')
        row = PT.load_ledger(self.ledger)[('T', PRO)]
        self.assertEqual((row['wall_now'], row['p1_wall_held'], row['computed_on']), ('9.0', '1', '2026-11-06'))
        self.assertEqual((row['outcome'], row['hit_t1'], row['outcome_asof']), ('T1', '1', '2026-11-13'))

    def test_missing_inputs_are_retried_later_not_frozen_blank(self):
        make_db(self.db, [('T', PRO, 8.5, 9.0, 150, 70.0)], [('T', PRO, 'OPEN', None)])
        self.run_update()
        self.assertEqual(PT.load_ledger(self.ledger)[('T', PRO)]['data_ok'], '0')
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO skew_daily VALUES ('T','2026-10-20',10,9.0,100,80.0)")
        con.commit(); con.close()
        self.run_update(today='2026-11-13')
        row = PT.load_ledger(self.ledger)[('T', PRO)]
        self.assertEqual((row['data_ok'], row['p1_wall_held'], row['computed_on']), ('1', '1', '2026-11-13'))

    def test_dry_run_writes_nothing(self):
        make_db(self.db, [('T', '2026-10-20', 10, 9.0, 100, 80.0), ('T', PRO, 8.5, 9.0, 150, 70.0)], [('T', PRO, 'T1', 1.43)])
        before = self.reg.read_bytes()
        text, new_rows, _ = self.run_update(dry_run=True)
        self.assertEqual(new_rows, 1)
        self.assertFalse(self.ledger.exists())
        self.assertEqual(self.reg.read_bytes(), before)

    def test_report_hides_rates_before_the_first_look_and_never_says_ready(self):
        rows, paths = [], []
        for i in range(6):
            tk = f'S{i}'
            rows += [(tk, '2026-10-20', 10, 9.0, 100, 80.0), (tk, PRO, 8.5, 9.0 if i % 2 else 8.0, 150, 70.0)]
            paths.append((tk, PRO, 'T1' if i % 2 else 'STOP', 1.43 if i % 2 else -1.0))
        make_db(self.db, rows, paths)
        text, _, summary = self.run_update()
        s = summary['P1_put_wall_held_through_washout']['prospective']
        self.assertEqual((s['n_resolved'], s['n_yes_resolved']), (6, 3))
        self.assertNotIn('hit_yes', s)                                   # 6 resolved < first look at 40
        self.assertIn('rates hidden until 40 resolved', text)
        self.assertNotIn('READY', text)

    def test_a_run_never_changes_the_registered_hypotheses(self):
        make_db(self.db, [('T', '2026-10-20', 10, 9.0, 100, 80.0), ('T', PRO, 8.5, 9.0, 150, 70.0)], [('T', PRO, 'T1', 1.43)])
        before = json.loads(self.reg.read_text(encoding='utf-8'))
        self.run_update()
        after = json.loads(self.reg.read_text(encoding='utf-8'))
        self.assertEqual(after['hypotheses'], before['hypotheses'])
        self.assertEqual(after['read_plan'], before['read_plan'])
        self.assertEqual(after['status']['new_prospective_rows_last_run'], 1)
        for h in after['hypotheses']:
            self.assertEqual((h['status'], h['implements']), ('tracking', False))


if __name__ == '__main__':
    unittest.main()
