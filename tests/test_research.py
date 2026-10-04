"""Offline regression tests for prospective labels, failure visibility and inference gates."""
import copy
import datetime as dt
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import label_candidates as LC
import market_time as MT
import path_labels as PL
import self_audit as SA

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = json.loads((ROOT / 'hypotheses.json').read_text(encoding='utf-8'))


def bars(n=4, open_price=100):
    dates = MT.sessions_between(dt.date(2026, 10, 6), dt.date(2026, 12, 31))[:n]
    return pd.DataFrame({'date': [s.isoformat() for s in dates], 'Open': [open_price]*n,
        'High': [open_price*1.01]*n, 'Low': [open_price*.99]*n, 'Close': [open_price]*n})


def insert_path(con, ticker, date, rec):
    rec = {**rec, 'ticker': ticker, 'scan_date': date}
    if rec.get('label_version') == PL.LABEL_VERSION:
        rec.setdefault('signal_features_json', json.dumps({'ticker': ticker, 'scan_date': date}))
    con.execute('INSERT INTO tier_a_paths (' + ','.join(rec) + ') VALUES ('
                + ','.join('?' for _ in rec) + ')', list(rec.values()))


class ResearchTestCase(unittest.TestCase):
    def setUp(self):
        self.globals = {key:getattr(SA,key) for key in
            ('ARCHIVES','_FRAME','_COVERAGE','_INFERENCE_STATUS','_CLUSTER_EVIDENCE')}

    def tearDown(self):
        for key,value in self.globals.items():setattr(SA,key,value)


class DecisionTests(ResearchTestCase):
    def test_raw_precision_and_numpy_boolean(self):
        bar = .05/29
        self.assertEqual(SA._verdict(.4, .0017477554091212665, True, 80, 10, bar), 'ACCUMULATING')
        self.assertEqual(SA._verdict(.4, 1e-8, np.bool_(True), 80, 10, bar), 'READY FOR DECISION')
        self.assertEqual(SA._verdict(.4, float('nan'), True, 80, 10, bar), 'INSUFFICIENT')

    def test_opposite_speed_halves_and_constant_feature(self):
        x = pd.DataFrame({'scan_date': np.repeat(['2026-11-02','2026-11-03','2026-11-04','2026-11-05'],10),
            'hit_t1': 1., 'atm_iv': np.r_[100+np.tile(np.arange(1,11),2),100-np.tile(np.arange(11,21),2)],
            'days_to_t1': np.r_[np.tile(np.arange(1,11),2),np.tile(np.arange(11,21),2)]})
        h = {'id':'speed','type':'speed','feature':'atm_iv','direction':'any','registered':'2026-10-04'}
        result = SA.score_one(h,x,10,.05/28)
        self.assertFalse(result['halves_agree'])
        self.assertNotEqual(result['verdict'],'READY FOR DECISION')
        x.atm_iv = 100.
        self.assertEqual(SA.score_one(h,x,10,.05/28)['verdict'],'INSUFFICIENT')

    def test_landmark_censoring_separate_from_outcome(self):
        x = pd.DataFrame({'days_to_t1':[np.nan,np.nan,2,np.nan], 'days_to_stop':[np.nan,np.nan,np.nan,np.nan],
            'outcome':['EXPIRED','OPEN','T1','OPEN'],'bars_seen':[20,9,5,1]})
        self.assertEqual(list(SA._open_at(x,3).index),[0,1])
        x['scan_date']='2026-11-02'; x['first_green_day']=np.nan; x['hit_t1']=[np.nan,np.nan,1.,np.nan]
        h={'id':'green','type':'post_entry','feature':'first_green_day','landmark_day':3,
           'registered':'2026-10-04','op':'<=','threshold':3}
        r=SA.score_one(h,x,1,.05)
        self.assertEqual(r['n_at_risk'],2); self.assertEqual(r['n_censored'],2)
        self.assertEqual(r['n_pass']+r['n_fail'],0)

    def test_missing_oi_never_becomes_pass(self):
        x=pd.DataFrame({'scan_date':['2026-11-02']*3,'days_to_t1':[5,5,np.nan],
          'days_to_stop':[np.nan,np.nan,5],'bars_seen':[5]*3,'hit_t1':[1.,1.,0.],
          'call_wall_oi_d2':[np.nan,400.,100.]})
        h={'id':'oi','type':'post_entry','feature':'call_wall_oi_d2','landmark_day':2,
          'registered':'2026-10-04','op':'>=','threshold':238}
        r=SA.score_one(h,x,1,.05)
        self.assertEqual((r['n_pass'],r['n_fail'],r['n_missing_feature']),(1,1,1))

    def test_weekly_cannot_promote_and_coverage_fails(self):
        reg=copy.deepcopy(REGISTRY); reg['hypotheses']=[{'id':'h','type':'entry_filter','status':'active',
            'registered':'2026-10-04','feature':'f','op':'>=','threshold':1}]
        reg['inference_protocol']['planned_tests']=1
        frame=pd.DataFrame({'scan_date':['2026-11-02'],'ticker':['X'],'f':[2.]})
        ready={'id':'h','type':'entry_filter','registered':'2026-10-04','verdict':'READY FOR DECISION','detail':'nominal'}
        with patch.object(SA,'load_frame',return_value=frame), patch.object(SA,'cluster_evidence',return_value={}), \
             patch.object(SA,'inference_status',return_value={'ok':True,'checkpoint_due':False}), \
             patch.object(SA,'feature_coverage',return_value=[]), patch.object(SA,'score_one',return_value=copy.deepcopy(ready)), \
             patch.object(SA,'label_coverage',return_value={'ok':True,'detail':'covered'}):
            result,_,_=SA.score_registry(None,reg)
            self.assertEqual(result[0]['verdict'],'DESCRIPTIVE')
            self.assertEqual(result[0]['nominal_iid_verdict'],'READY FOR DECISION')
        with patch.object(SA,'load_frame',return_value=frame), patch.object(SA,'cluster_evidence',return_value={}), \
             patch.object(SA,'inference_status',return_value={'ok':True,'checkpoint_due':False}), \
             patch.object(SA,'feature_coverage',return_value=[]), \
             patch.object(SA,'label_coverage',return_value={'ok':False,'detail':'one expected row missing'}):
            result,_,_=SA.score_registry(None,reg)
            self.assertEqual(result[0]['verdict'],'ERROR')


class PathTests(ResearchTestCase):
    def test_real_qualifier_query_preserves_empty_schema_and_uses_skew_column(self):
        con=sqlite3.connect(':memory:')
        con.execute('CREATE TABLE candidate_log(ticker TEXT,scan_date TEXT,current_signal TEXT,near_dte REAL,'
          'skew_change_5d REAL,near_skew REAL,spot_return_pct REAL,put_wall_oi_change REAL,sector TEXT,'
          'spot_close REAL,put_wall_strike REAL,atm_iv REAL,skew REAL,screen_version TEXT,window_sessions INTEGER)')
        with patch.object(MT,'last_completed_session',return_value=dt.date(2026,10,8)):
            empty=PL.expected_qualifiers(con,dt.date(2026,10,9))
            self.assertTrue(empty.empty);self.assertIn('ticker',empty.columns)
            con.execute('INSERT INTO candidate_log VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              ('X','2026-10-05','BULLISH_REVERSAL',4,-9,-9,-15,-1,'Tech',100,90,80,-12,'nine-session-v2',9))
            q=PL.expected_qualifiers(con,dt.date(2026,10,9))
        self.assertEqual(q.ticker.tolist(),['X']);self.assertGreaterEqual(q.n_legs.iloc[0],1)
        con.execute('UPDATE candidate_log SET screen_version=NULL')
        with self.assertRaises(PL.LabelDataError):PL.qualifiers(con,dt.date(2026,10,9))
        con.close()

    def test_next_open_same_price_basis_and_unknown_realized_return(self):
        g=bars(4,10)
        rec=PL.label_one(g,'2026-10-05',100.)
        self.assertEqual(rec['entry'],10.)
        self.assertEqual(rec['entry_date'],'2026-10-06')
        self.assertEqual(rec['outcome'],'OPEN')
        self.assertIsNone(rec['pnl_pct']); self.assertIsNone(rec['r_live'])
        self.assertEqual(rec['bars_seen'],4)

    def test_unlimited_live_and_complete_shadow_coverage(self):
        g=bars(25); g.loc[24,'High']=111.
        rec=PL.label_one(g,'2026-10-05')
        self.assertEqual((rec['outcome'],rec['days_to_t1'],rec['complete']),('T1',25,1))
        self.assertIsNone(rec['r_t12'])
        self.assertAlmostEqual(rec['r_stop5'],10./7)
        self.assertAlmostEqual(rec['r_stop6'],10./7)
        self.assertEqual(rec['pnl_stop5'],10.)
        g.loc[24,'High']=113.
        self.assertIsNotNone(PL.label_one(g,'2026-10-05')['r_t12'])

    def test_entry_session_ambiguity_and_bound_semantics(self):
        g=bars(1); g.loc[0,['High','Low','Close']]=[115.,90.,100.]
        r=PL.label_one(g,'2026-10-05')
        self.assertEqual((r['outcome'],r['exit_ambiguous']),('STOP',1))
        self.assertAlmostEqual(r['pnl_pct'],-7.)
        self.assertLessEqual(r['mfe_upper_pct'],10.)
        self.assertFalse(r['extrema_exact'])
        g=bars(1);g.loc[0,['High','Low','Close']]=[115.,95.,105.]
        r=PL.label_one(g,'2026-10-05')
        self.assertEqual(r['outcome'],'T1')
        self.assertEqual((r['mae_lower_pct'],r['mae_upper_pct']),(-5.,0.))
        self.assertIsNone(r['first_green_day']) # exit-day close was after the fill

    def test_raw_common_denominator_and_landmark_shadow(self):
        g=bars(4);g.loc[3,'Low']=92.
        r=PL.label_one(g,'2026-10-05')
        self.assertEqual(r['pnl_stop5'],-5.)
        self.assertAlmostEqual(r['r_stop5'],-5./7)
        g=bars(4);g.loc[1,['Low','Close']]=[97.,98.]
        r=PL.label_one(g,'2026-10-05')
        self.assertEqual(r['outcome'],'OPEN');self.assertIsNone(r['r_live'])
        self.assertAlmostEqual(r['r_nevergreen_d2'],-2./7)

    def test_missing_session_fails_and_no_future_is_pending(self):
        g=bars(4)
        with self.assertRaises(PL.LabelDataError):PL.label_one(g.iloc[1:],'2026-10-05')
        with self.assertRaises(PL.LabelDataError):PL.label_one(g.drop(index=1),'2026-10-05')
        self.assertEqual(PL.label_one(g.iloc[:0],'2026-10-05'),PL.NOT_YET)

    def test_exact_day2_oi_not_second_later_scan(self):
        con=sqlite3.connect(':memory:');PL.ensure_schema(con)
        con.execute('CREATE TABLE candidate_log(ticker TEXT,scan_date TEXT,call_wall_oi REAL)')
        r=PL.label_one(bars(4),'2026-10-05');insert_path(con,'X','2026-10-05',r)
        con.executemany('INSERT INTO candidate_log VALUES(?,?,?)',[('X','2026-10-06',100),('X','2026-10-08',999)])
        PL.backfill_call_wall_oi_d2(con)
        self.assertIsNone(con.execute('SELECT call_wall_oi_d2 FROM tier_a_paths').fetchone()[0])
        con.execute('INSERT INTO candidate_log VALUES(?,?,?)',('X','2026-10-07',238))
        PL.backfill_call_wall_oi_d2(con)
        self.assertEqual(con.execute('SELECT call_wall_oi_d2 FROM tier_a_paths').fetchone()[0],238)
        con.close()

    def test_expected_missing_download_is_transaction_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'probe.db'); con=sqlite3.connect(db); PL.ensure_schema(con)
            insert_path(con,'OLD','2026-09-01',{'label_version':4,'entry':100.,'pnl_pct':10.})
            con.commit(); con.close()
            expected=pd.DataFrame({'ticker':['X'],'scan_date':['2026-10-05'],'n_legs':[1]})
            with patch.object(PL,'DB',db),patch.object(PL,'expected_qualifiers',return_value=expected), \
                 patch.object(MT,'last_completed_session',return_value=dt.date(2026,10,6)), \
                 patch.object(PL.yf,'download',return_value=pd.DataFrame()):
                with self.assertRaises(PL.LabelDataError):PL.main()
            con=sqlite3.connect(db)
            self.assertEqual(con.execute('SELECT ticker,label_version,pnl_pct FROM tier_a_paths').fetchall(),[('OLD',4,10.)])
            con.close()

    def test_coverage_ignores_preserved_history_but_names_missing_new_row(self):
        con=sqlite3.connect(':memory:');PL.ensure_schema(con)
        insert_path(con,'OLD','2026-09-01',{'label_version':4})
        insert_path(con,'X','2026-10-05',PL.label_one(bars(3),'2026-10-05'))
        expected=pd.DataFrame({'ticker':['X','Y'],'scan_date':['2026-10-05']*2})
        with patch.object(PL,'expected_qualifiers',return_value=expected), \
             patch.object(MT,'last_completed_session',return_value=dt.date(2026,10,8)):
            result=SA.label_coverage(con)
        self.assertFalse(result['ok']);self.assertEqual(result['missing'],['Y 2026-10-05'])
        self.assertEqual(result['legacy_rows_preserved'],1)
        with patch.object(PL,'expected_qualifiers',return_value=expected.iloc[:1]), \
             patch.object(MT,'last_completed_session',return_value=dt.date(2026,10,8)):
            self.assertTrue(SA.label_coverage(con)['ok'])
        con.close()

    def test_frozen_prices_replay_without_provider(self):
        g=bars(4);g['Stock Splits']=0.;g['Dividends']=0.;g.loc[3,'High']=113.
        original=PL.label_one(g,'2026-10-05')
        frozen=pd.DataFrame(json.loads(original['price_inputs_json'])['bars'])
        self.assertEqual(PL.price_hash(frozen),original['price_hash'])
        replay=PL.label_one(frozen,'2026-10-05')
        for field in ('entry','entry_date','outcome','pnl_pct','r_stop5','r_t12','mae_lower_pct','mfe_upper_pct'):
            self.assertEqual(replay[field],original[field])

    def test_frozen_feature_loader_ignores_changed_candidate_history(self):
        con=sqlite3.connect(':memory:');PL.ensure_schema(con)
        r=PL.label_one(bars(3),'2026-10-05')
        r['signal_features_json']=json.dumps({'ticker':'X','scan_date':'2026-10-05','sector':'Tech',
          'spot_close':100.,'put_wall_strike':90.,'atm_iv':80.,'sector_iv_rank':70.,
          'skew_slope':-2.,'combo_pass':1.,'washouts':12})
        insert_path(con,'X','2026-10-05',r)
        # No candidate_log/skew_daily read is necessary: snapshots are sufficient.
        frame=SA.load_frame(con)
        self.assertEqual(frame.sector_iv_rank.iloc[0],70.)
        self.assertEqual(frame.skew_slope.iloc[0],-2.)
        self.assertEqual(frame.washouts.iloc[0],12)
        con.close()

    def test_label_job_and_full_registry_on_prospective_temp_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'probe.db');con=sqlite3.connect(db)
            candidate={'ticker':'X','scan_date':'2026-10-05','sector':'Tech','industry':'Software',
              'current_signal':'BULLISH_REVERSAL','near_dte':4.,'skew_change_5d':-9.,'near_skew':-9.,
              'spot_return_pct':-15.,'put_wall_oi_change':-1.,'spot_close':100.,'put_wall_strike':90.,
              'atm_iv':80.,'skew':-12.,'screen_version':'nine-session-v2','window_sessions':9,
              'sector_iv_rank':70.,'iv_hv_ratio':1.2,'hv_10d':70.,'ticker_vix':95.,'call_wall_oi':200.}
            text={'ticker','scan_date','sector','industry','current_signal','screen_version'}
            con.execute('CREATE TABLE candidate_log (' + ','.join(f'{k} '+('TEXT' if k in text else 'REAL') for k in candidate)+')')
            con.execute('INSERT INTO candidate_log VALUES('+','.join('?' for _ in candidate)+')',list(candidate.values()))
            later={**candidate,'scan_date':'2026-10-07','current_signal':'NONE','call_wall_oi':238.}
            con.execute('INSERT INTO candidate_log VALUES('+','.join('?' for _ in later)+')',list(later.values()))
            con.execute('CREATE TABLE skew_daily(ticker TEXT,date TEXT,skew REAL)')
            con.executemany('INSERT INTO skew_daily VALUES(?,?,?)',[('X','2026-10-01',-6.),('X','2026-10-02',-9.),('X','2026-10-05',-12.)])
            q=PL.qualifiers(con,dt.date(2026,10,9));con.commit();con.close()
            raw=bars(3);raw.loc[2,'High']=113.;raw['Stock Splits']=0.;raw['Dividends']=0.
            raw.index=pd.to_datetime(raw.date);raw=raw.drop(columns='date')
            with patch.object(PL,'DB',db),patch.object(PL,'expected_qualifiers',return_value=q), \
                 patch.object(MT,'last_completed_session',return_value=dt.date(2026,10,8)), \
                 patch.object(PL.yf,'download',return_value=raw) as download:
                self.assertEqual(PL.main(),1)
                self.assertEqual(PL.main(),0)
                self.assertEqual(download.call_count,1)
                con=sqlite3.connect(db)
                with patch.object(SA,'inference_status',return_value={'ok':True,'checkpoint_due':False,'detail':'test-only frozen protocol'}):
                    results,bar,n=SA.score_registry(con,REGISTRY,today=dt.date(2026,10,9),archives_dir=tmp)
                self.assertTrue(SA._COVERAGE['ok'],SA._COVERAGE)
                self.assertEqual(n,28);self.assertEqual(len(results),16)
                self.assertNotIn('ERROR',{r['verdict'] for r in results})
                self.assertNotIn('READY FOR DECISION',{r['verdict'] for r in results})
                con.execute('UPDATE candidate_log SET sector_iv_rank=999');con.commit()
                self.assertEqual(SA.load_frame(con).sector_iv_rank.iloc[0],70.)
                frozen=json.loads(con.execute('SELECT price_inputs_json FROM tier_a_paths').fetchone()[0])
                self.assertEqual(len(frozen['bars']),3)
                con.close()


class ForwardTests(ResearchTestCase):
    def test_calendar_benchmark_consistent_basis(self):
        g=pd.DataFrame({'date':['2026-10-09','2026-10-12','2026-10-13'],
          'Open':[10.,10.,10.],'High':[10.1]*3,'Low':[9.9]*3,'Close':[10.,10.,10.]})
        r=LC.forward_labels(g,'2026-10-09')
        self.assertEqual(r['fwd_3d_date'],'2026-10-12')
        self.assertEqual(r['fwd_3d_return'],0.)
        with self.assertRaises(PL.LabelDataError):LC.forward_labels(g,'2026-10-08')

    def test_full_peer_residual_repair_is_order_independent(self):
        con=sqlite3.connect(':memory:')
        con.execute('CREATE TABLE candidate_log(id INTEGER PRIMARY KEY,scan_date TEXT,sector TEXT,industry TEXT,'
          'fwd_5d_return REAL,sector_residual_5d REAL,industry_residual_5d REAL)')
        con.executemany('INSERT INTO candidate_log VALUES(?,"2026-10-05","Tech","Software",?,NULL,NULL)',enumerate([0.,1.,2.],1))
        LC._compute_residuals(con)
        con.executemany('INSERT INTO candidate_log VALUES(?,"2026-10-05","Tech","Software",?,NULL,NULL)',enumerate([100.,101.,102.],4))
        LC._compute_residuals(con)
        expected=[-51.,-50.,-49.,49.,50.,51.]
        self.assertEqual([r[0] for r in con.execute('SELECT sector_residual_5d FROM candidate_log ORDER BY id')],expected)
        before=con.total_changes
        LC._compute_residuals(con)
        self.assertEqual(con.total_changes,before)
        con.close()

    def test_partial_forward_download_rolls_back_and_preserves_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'probe.db');con=sqlite3.connect(db)
            con.execute('CREATE TABLE candidate_log(id INTEGER PRIMARY KEY,ticker TEXT,scan_date TEXT,sector TEXT,industry TEXT,fwd_5d_return REAL)')
            con.executemany('INSERT INTO candidate_log VALUES(?,?,?,?,?,?)',[(1,'OLD','2026-09-01','Tech','Software',6514.),
              (2,'X','2026-10-05','Tech','Software',None),(3,'Y','2026-10-05','Tech','Software',None)])
            LC.ensure_forward_schema(con);con.commit();con.close()
            raw=bars(3)
            anchor=raw.iloc[:1].copy();anchor['date']='2026-10-05'
            raw=pd.concat([anchor,raw],ignore_index=True)
            raw['Stock Splits']=0.;raw['Dividends']=0.
            raw.index=pd.to_datetime(raw.date);raw=raw.drop(columns='date')
            def history(ticker):
                class Ticker:
                    def history(self,**kwargs):
                        if not (kwargs['auto_adjust'] is False and kwargs['actions'] is True):
                            raise AssertionError('adjusted provider request')
                        return raw if ticker=='X' else pd.DataFrame()
                return Ticker()
            # X writes first, then Y fails: none of X's partial transaction may survive.
            with patch.object(LC.yf,'Ticker',side_effect=history),patch.object(MT,'last_completed_session',return_value=dt.date(2026,10,8)):
                with self.assertRaises(PL.LabelDataError):LC.update_forward_returns(db)
            con=sqlite3.connect(db)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM candidate_forward_v2').fetchone()[0],0)
            self.assertEqual(con.execute('SELECT fwd_5d_return FROM candidate_log WHERE id=1').fetchone()[0],6514.)
            con.close()


class ProtocolTests(ResearchTestCase):
    def test_failed_send_retains_report_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'probe.db');con=sqlite3.connect(db);PL.ensure_schema(con);con.commit();con.close()
            result=[{'id':'probe','verdict':'INSUFFICIENT'}]
            with patch.object(SA,'DB',db),patch.object(SA,'HERE',tmp), \
                 patch.object(SA,'score_registry',return_value=(result,.05/28,28)), \
                 patch.object(SA,'digest',return_value='saved descriptive report'), \
                 patch.object(SA,'_INFERENCE_STATUS',{'ok':True,'checkpoint_due':False}), \
                 patch.object(SA,'_COVERAGE',{'ok':True}),patch.object(SA,'_CLUSTER_EVIDENCE',{}), \
                 patch.object(sys,'argv',['self_audit.py','--json','--send']), \
                 patch('dotenv.load_dotenv',return_value=False),patch('alert.send_telegram',return_value=False):
                with self.assertRaises(SystemExit) as error:SA.main()
            self.assertEqual(error.exception.code,1)
            self.assertEqual(json.loads((Path(tmp)/'audit_latest.json').read_text(encoding='utf-8'))['results'],result)
            self.assertEqual(len(list((Path(tmp)/'audit_history').rglob('*.json'))),1)

    def test_freeze_before_cohort_and_changed_engine_cannot_reuse_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for file in SA.ENGINE_FILES:(root/file).write_text('frozen engine',encoding='utf-8')
            lock=root/'lock.json';reg=copy.deepcopy(REGISTRY)
            SA.freeze_protocol(reg,lock,today=dt.date(2026,10,4),root=root)
            original=lock.read_bytes()
            SA.freeze_protocol(reg,lock,today=dt.date(2026,10,4),root=root)
            self.assertEqual(lock.read_bytes(),original)
            (root/SA.ENGINE_FILES[0]).write_text('changed engine',encoding='utf-8')
            with self.assertRaises(ValueError):SA.freeze_protocol(reg,lock,today=dt.date(2026,10,4),root=root)
            with self.assertRaises(ValueError):SA.freeze_protocol(reg,root/'late.json',today=dt.date(2026,10,5),root=root)

    def test_dependency_components_and_censored_count(self):
        d=pd.DataFrame({'ticker':['A','B','A','C'],'scan_date':['2026-10-05','2026-10-05','2026-10-20','2026-10-11'],
          'entry_date':['2026-10-06','2026-10-06','2026-10-21','2026-10-12'],
          'exit_date':['2026-10-07','2026-10-07',None,'2026-10-13'],
          'observed_through':['2026-10-07','2026-10-07','2026-10-22','2026-10-13'],
          'outcome':['T1','STOP','OPEN','T1'],'pnl_pct':[10.,-7.,np.nan,10.]})
        e=SA.cluster_evidence(d,REGISTRY)
        self.assertEqual((e['n_clusters'],e['n_resolved'],e['n_censored']),(2,3,1))
        self.assertFalse(e['promotion_evidence'])
        self.assertIn('interval_unavailable',e)

    def test_history_append_only_and_checkpoint_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            record={'date':'2027-07-02','results':[{'id':'x','verdict':'DESCRIPTIVE'}]}
            with patch.object(SA,'_INFERENCE_STATUS',{'ok':True,'checkpoint_due':True}), \
                 patch.object(SA,'_FRAME',pd.DataFrame()),patch.object(SA,'_COVERAGE',{'ok':True}), \
                 patch.object(SA,'_CLUSTER_EVIDENCE',{}):
                first=SA.save_record(record,REGISTRY,tmp)
                self.assertTrue(first.exists())
                target=Path(tmp)/'inference_checkpoints'/f"{REGISTRY['inference_protocol']['version']}.json"
                before=target.read_bytes()
                record['date']='2027-07-09';record['results'][0]['verdict']='INSUFFICIENT'
                SA.save_record(record,REGISTRY,tmp)
                self.assertEqual(target.read_bytes(),before)
            self.assertEqual(len(list((Path(tmp)/'audit_history').rglob('*.json'))),2)


if __name__=='__main__':
    unittest.main()
