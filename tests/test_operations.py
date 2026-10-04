"""Offline failure/recovery tests; no Git push, release mutation or transport."""
from contextlib import closing, contextmanager, ExitStack, redirect_stdout
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import db_state as DS
import scan_marker as SM
import state_lock as SL
import verify_daily as VD

ROOT = Path(__file__).resolve().parents[1]
DAY = '2026-10-02'
NOW = dt.datetime(2026, 10, 3, 1, 30, tzinfo=dt.timezone.utc)
OLD = 'skewdb-20261002T220000Z-old.db'
NEW = 'skewdb-20261003T010000Z-new.db'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def database(path):
    with closing(sqlite3.connect(path)) as con, con:
        con.execute('CREATE TABLE candidate_log (scan_date TEXT, sector_iv_rank REAL, '
                    'skew_change_5d REAL, put_wall_oi_change REAL, current_signal TEXT, '
                    'near_dte INTEGER, near_skew REAL, spot_return_pct REAL)')
        con.executemany('INSERT INTO candidate_log VALUES (?,?,?,?,?,?,?,?)',
                        [(DAY, 60, -10, 0, 'BULLISH_REVERSAL', 5, -10, -9)] * 1000)
        for table in ('skew_daily', 'fixed_strike_vol'):
            con.execute(f'CREATE TABLE {table} (date TEXT)')
            con.executemany(f'INSERT INTO {table} VALUES (?)', [(DAY,)] * 1000)


class FakeRelease:
    def __init__(self, initial):
        self.data = {OLD: initial}
        self.overrides = {}
        self.fail_upload = False
        self.corrupt_download = False
        self.rival_on_upload = None
        self.deleted = []
        self.calls = []

    def assets(self):
        return [dict({'name': name, 'size': len(data), 'state': 'uploaded',
                      'digest': 'sha256:' + digest(data), 'createdAt': '2026-10-02T22:00:00Z'},
                     **self.overrides.get(name, {})) for name, data in self.data.items()]

    def gh(self, *args):
        self.calls.append(args)
        if args[:2] == ('release', 'view'):
            return json.dumps({'assets': self.assets()})
        if args[:2] == ('release', 'download'):
            name, dest = args[args.index('-p') + 1], args[args.index('-O') + 1]
            Path(dest).write_bytes(b'bad' if self.corrupt_download else self.data[name])
            return ''
        if args[:2] == ('release', 'upload'):
            if self.fail_upload:
                raise DS.DBStateError('mock interrupted upload')
            path = Path(args[-1])
            self.data[path.name] = path.read_bytes()
            if self.rival_on_upload:
                self.data[self.rival_on_upload] = path.read_bytes()
            return ''
        if args[:2] == ('release', 'delete-asset'):
            self.deleted.append(args[3])
            del self.data[args[3]]
            return ''
        raise AssertionError(f'unexpected release command {args[:2]}')


@contextmanager
def release_fixture():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        base = Path(tmp)
        path = base / 'local.db'
        database(path)
        release = FakeRelease(path.read_bytes())
        for name, value in {'DB': str(path), 'GEN_FILE': str(base / 'generation.json'),
                            'MIN_BYTES': 1, 'REQUIRED_TABLES': {'candidate_log': 1000, 'skew_daily': 1000},
                            'GH': release.gh, 'KEEP': 2,
                            'WRITER_CONTEXT': 'github-actions:tier-a-db-writer'}.items():
            stack.enter_context(patch.object(DS, name, value))
        stack.enter_context(patch.dict(os.environ, {'DB_STATE_PULL_GENERATION': ''}))
        stack.enter_context(redirect_stdout(io.StringIO()))
        yield base, path, release


class StorageTests(unittest.TestCase):
    def test_corrupt_download_preserves_existing_local_database(self):
        with release_fixture() as (_, path, release):
            before = path.read_bytes()
            release.corrupt_download = True
            with self.assertRaises(DS.DBStateError):
                DS.pull()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(release.deleted, [])

    def test_failed_upload_never_deletes_last_generation(self):
        with release_fixture() as (_, path, release):
            DS.pull()
            with closing(sqlite3.connect(path)) as con, con:
                con.execute('INSERT INTO skew_daily VALUES (?)', (DAY,))
            release.fail_upload = True
            with self.assertRaises(DS.DBStateError):
                DS.push()
            self.assertIn(OLD, release.data)
            self.assertEqual(release.deleted, [])
            upload = next(call for call in release.calls if call[:2] == ('release', 'upload'))
            self.assertNotIn('--clobber', upload)

    def test_stale_and_same_name_changed_content_are_refused(self):
        with release_fixture() as (_, _, release):
            DS.pull()
            release.data[NEW] = release.data[OLD]
            with self.assertRaisesRegex(DS.DBStateError, 'moved on'):
                DS.push()
            del release.data[NEW]
            release.data[OLD] += b'changed'
            with self.assertRaisesRegex(DS.DBStateError, 'same generation'):
                DS.push()
            self.assertFalse(any(call[:2] == ('release', 'upload') for call in release.calls))

    def test_direct_writer_without_shared_workflow_context_is_refused(self):
        with release_fixture(), patch.object(DS, 'WRITER_CONTEXT', ''):
            with self.assertRaisesRegex(DS.DBStateError, 'shared tier-a-db-writer'):
                DS.push()

    def test_pinned_restore_uses_exact_generation_not_newest(self):
        with release_fixture() as (_, path, release):
            release.data[NEW] = release.data[OLD] + b'newer valid SQLite trailer'
            with patch.dict(os.environ, {'DB_STATE_PULL_GENERATION': OLD}):
                DS.pull()
            self.assertEqual(path.read_bytes(), release.data[OLD])
            self.assertEqual(DS.generation()['name'], OLD)

    def test_verified_upload_then_prunes_only_older_generations(self):
        with release_fixture() as (_, path, release), patch.object(DS, '_snapshot_name', return_value=NEW):
            DS.pull()
            with closing(sqlite3.connect(path)) as con, con:
                con.execute('INSERT INTO skew_daily VALUES (?)', (DAY,))
            older = 'skewdb-20261001T220000Z-before.db'
            release.data[older] = release.data[OLD]
            self.assertEqual(DS.push(), NEW)
            self.assertEqual(release.deleted, [older])
            self.assertIn(OLD, release.data)
            self.assertEqual(DS.generation()['sha256'], digest(release.data[NEW]))

    def test_unverified_upload_removes_only_new_asset_without_pruning(self):
        for bad in ({'digest': 'sha256:' + '0' * 64}, {'state': 'starter'}):
            with self.subTest(bad=bad), release_fixture() as (_, path, release), patch.object(DS, '_snapshot_name', return_value=NEW):
                DS.pull()
                with closing(sqlite3.connect(path)) as con, con:
                    con.execute('INSERT INTO skew_daily VALUES (?)', (DAY,))
                release.overrides[NEW] = bad
                with self.assertRaisesRegex(DS.DBStateError, 'could not be verified'):
                    DS.push()
                self.assertEqual(release.deleted, [NEW])
                self.assertIn(OLD, release.data)

    def test_failed_label_batch_retains_valid_schema_without_partial_rows(self):
        import path_labels as PL
        with release_fixture() as (_, path, release), patch.object(DS, '_snapshot_name', return_value=NEW):
            DS.pull()
            with closing(sqlite3.connect(path)) as con:
                PL.ensure_schema(con)
                con.execute('INSERT INTO tier_a_paths (ticker,scan_date) VALUES (?,?)', ('FIXTURE', DAY))
                con.rollback()  # Provider failure rolls back the batch, not prior DDL.
            DS.push()
            self.assertIn(NEW, release.data)
            with closing(sqlite3.connect(path)) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM tier_a_paths').fetchone()[0], 0)
                columns = {row[1] for row in con.execute('PRAGMA table_info(tier_a_paths)')}
                self.assertIn('price_inputs_json', columns)
                self.assertIn('label_version', columns)

    def test_rival_during_upload_withdraws_only_our_generation(self):
        rival = 'skewdb-20261003T000000Z-rival.db'
        with release_fixture() as (_, path, release), patch.object(DS, '_snapshot_name', return_value=NEW):
            DS.pull()
            with closing(sqlite3.connect(path)) as con, con:
                con.execute('INSERT INTO skew_daily VALUES (?)', (DAY,))
            release.rival_on_upload = rival
            with self.assertRaisesRegex(DS.DBStateError, 'rival database generation'):
                DS.push()
            self.assertEqual(release.deleted, [NEW])
            self.assertIn(rival, release.data)
            self.assertIn(OLD, release.data)
            self.assertEqual(json.loads(Path(DS.GEN_FILE).read_text())['name'], OLD)


class CheckpointTests(unittest.TestCase):
    def test_date_is_eastern_across_utc_midnight_and_dst(self):
        self.assertEqual(SL.trading_date(dt.datetime(2026, 7, 2, 0, 30, tzinfo=dt.timezone.utc)), dt.date(2026, 7, 1))
        self.assertEqual(SL.trading_date(dt.datetime(2026, 12, 2, 0, 30, tzinfo=dt.timezone.utc)), dt.date(2026, 12, 1))
        with self.assertRaises(ValueError):
            SL.now_eastern(dt.datetime(2026, 7, 1))

    def test_independent_process_cannot_enter_held_portfolio_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = str(Path(tmp) / 'writer.lock')
            code = ('from state_lock import portfolio_lock, StateCheckpointError\n'
                    'import sys\ntry:\n'
                    f'    with portfolio_lock({lock!r}, timeout=0.15): pass\n'
                    'except StateCheckpointError: sys.exit(7)\n')
            with SL.portfolio_lock(lock):
                blocked = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True)
                self.assertEqual(blocked.returncode, 7, blocked.stderr)
            admitted = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True)
            self.assertEqual(admitted.returncode, 0, admitted.stderr)

    def test_local_checkpoint_has_no_git_or_network_activity(self):
        with patch.dict(os.environ, {'DURABLE_STATE_CHECKPOINT': 'local'}), patch.object(SL, '_git') as git:
            SL.checkpoint()
        git.assert_not_called()

    def test_failed_git_push_is_loud_and_never_auto_rebases(self):
        calls = []
        def git(*args, **kwargs):
            calls.append(args)
            if args[0] == 'push':
                raise SL.StateCheckpointError('mock failed push')
            return SimpleNamespace(returncode=0, stdout='')
        with patch.dict(os.environ, {'DURABLE_STATE_CHECKPOINT': 'git', 'GITHUB_REF_NAME': 'master'}), patch.object(SL, '_git', side_effect=git):
            with self.assertRaisesRegex(SL.StateCheckpointError, 'failed push'):
                SL.checkpoint([])
        self.assertTrue(any(call[0] == 'push' for call in calls))
        self.assertFalse(any(call[0] in ('pull', 'rebase', 'merge') for call in calls))

    def test_source_or_external_paths_cannot_enter_checkpoint_commit(self):
        with patch.dict(os.environ, {'DURABLE_STATE_CHECKPOINT': 'git', 'GITHUB_REF_NAME': 'master'}), patch.object(SL, '_git') as git:
            for path in ('main.py', '../outside.json'):
                with self.assertRaises(SL.StateCheckpointError):
                    SL.checkpoint([path])
            git.assert_not_called()

    def test_already_staged_source_is_refused_before_state_is_added_or_pushed(self):
        with patch.dict(os.environ, {'DURABLE_STATE_CHECKPOINT': 'git', 'GITHUB_REF_NAME': 'master'}), patch.object(SL, '_git', return_value=SimpleNamespace(returncode=0, stdout='main.py\n')) as git:
            with self.assertRaises(SL.StateCheckpointError):
                SL.checkpoint(['signals'])
            self.assertEqual(git.call_args_list[0].args, ('diff', '--cached', '--name-only'))
            self.assertEqual(git.call_count, 1)


class MarkerAndHealthTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with release_fixture() as (base, path, release):
            DS.pull()
            gen = DS.generation()
            (base / 'signals').mkdir()
            archive = {'run_id': 'frozen-decision', 'scan_date': DAY, 'taken_tickers': [],
                       'candidates': [], 'delivery': {'complete': True}, 'outbox': {}, 'data_generation': gen}
            (base / 'signals' / f'{DAY}.json').write_text(json.dumps(archive), encoding='utf-8')
            before = Path.cwd()
            try:
                os.chdir(base)
                with patch.object(SM, 'DB', str(path)):
                    yield base, path, release, archive
            finally:
                os.chdir(before)

    def test_final_marker_and_health_require_exact_generations_and_decision(self):
        with self.fixture() as (base, _, release, archive):
            rec = SM.write_marker('PM', DAY)
            self.assertEqual(rec['decision_run_id'], archive['run_id'])
            self.assertEqual(rec['db_generation'], rec['decision_db_generation'])
            self.assertEqual(DS.health(now=NOW), [])
            release.overrides[OLD] = {'digest': 'sha256:' + '0' * 64}
            self.assertTrue(any('digest differs' in p for p in DS.health(now=NOW)))
            release.overrides[OLD] = {}
            archive['delivery']['complete'] = False
            (base / 'signals' / f'{DAY}.json').write_text(json.dumps(archive), encoding='utf-8')
            self.assertTrue(any('incomplete' in p for p in DS.health(now=NOW)))

    def test_incomplete_delivery_cannot_replace_last_good_marker(self):
        with self.fixture() as (base, _, _, archive):
            previous = {'latest_scan_date': '2026-10-01'}
            (base / 'last_scan_pm.json').write_text(json.dumps(previous), encoding='utf-8')
            archive['delivery']['complete'] = False
            (base / 'signals' / f'{DAY}.json').write_text(json.dumps(archive), encoding='utf-8')
            with self.assertRaises(RuntimeError):
                SM.write_marker('PM', DAY)
            self.assertEqual(json.loads((base / 'last_scan_pm.json').read_text()), previous)

    def test_modified_unstored_database_cannot_get_marker(self):
        with self.fixture() as (_, path, _, _):
            with closing(sqlite3.connect(path)) as con, con:
                con.execute('INSERT INTO skew_daily VALUES (?)', (DAY,))
            with self.assertRaisesRegex(DS.DBStateError, 'differs'):
                SM.write_marker('PM', DAY)

    def test_unrelated_uploaded_asset_cannot_mask_missing_database(self):
        with self.fixture() as (_, _, release, _):
            SM.write_marker('PM', DAY)
            release.data = {'some-other-file.zip': b'x' * 100000}
            self.assertTrue(any('no database snapshot' in p for p in DS.health(now=NOW)))

    def test_holiday_health_uses_previous_completed_session(self):
        with self.fixture():
            SM.write_marker('PM', DAY)
            # Weekend does not demand a new empty scan or a fresh asset.
            sunday = dt.datetime(2026, 10, 5, 1, 30, tzinfo=dt.timezone.utc)
            self.assertEqual(DS.health(now=sunday), [])

    def test_unresolved_close_is_loud_with_empty_open_book(self):
        with self.fixture() as (base, _, _, _):
            SM.write_marker('PM', DAY)
            (base / 'closed_trades.json').write_text(json.dumps([{'publication': {'telegram': 'unknown', 'x': 'posted'}}]))
            self.assertTrue(any('close announcements' in p for p in DS.health(now=NOW)))

    def test_daily_verifier_rejects_new_incomplete_outbox_preserves_legacy(self):
        with self.fixture() as (base, path, _, archive), patch.object(VD, 'REPO', base), patch.object(VD, 'DB', path), patch.object(sys, 'argv', ['verify_daily.py', DAY]):
            archive['delivery']['complete'] = False
            dest = base / 'signals' / f'{DAY}.json'
            dest.write_text(json.dumps(archive), encoding='utf-8')
            self.assertEqual(VD.main(), 1)
            del archive['outbox']; del archive['delivery']
            dest.write_text(json.dumps(archive), encoding='utf-8')
            self.assertEqual(VD.main(), 0)


class WorkflowSyntaxTests(unittest.TestCase):
    def test_storage_conditions_preserve_successful_stages_after_later_failure(self):
        import ast
        def condition(filename, step_name):
            source = (ROOT / '.github' / 'workflows' / filename).read_text(encoding='utf-8')
            block = source.split('- name: ' + step_name, 1)[1].split('- name: ', 1)[0]
            expression = re.search(r'^\s*if: (.+)$', block, re.M).group(1)
            expression = expression.replace('&&', ' and ').replace('||', ' or ')
            tree = ast.parse(expression, mode='eval')
            allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq,
                       ast.NotEq, ast.Attribute, ast.Name, ast.Load, ast.Constant, ast.Call)
            self.assertTrue(all(isinstance(n, allowed) for n in ast.walk(tree)))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    self.assertTrue(isinstance(node.func, ast.Name) and node.func.id == 'always' and not node.args)
                if isinstance(node, ast.Attribute):
                    self.assertIn(node.attr, ('pull', 'label', 'scan', 'outcome', 'dry_run'))
                if isinstance(node, ast.Name):
                    self.assertIn(node.id, ('steps', 'inputs', 'always', 'true'))
            return compile(tree, '<workflow condition>', 'eval')
        weekly = condition('weekly_audit.yml', 'Store database state even if reporting failed')
        am = condition('skew_am.yml', 'Store database state')
        def run(compiled, pull='success', label='success', scan='success', dry=False):
            steps = SimpleNamespace(**{key: SimpleNamespace(outcome=value) for key, value in
                                     {'pull': pull, 'label': label, 'scan': scan}.items()})
            return eval(compiled, {'__builtins__': {}}, {'steps': steps, 'always': lambda: True,
                        'inputs': SimpleNamespace(dry_run=dry), 'true': True})
        self.assertTrue(run(weekly, label='failure'))  # Schema survives failed batch.
        self.assertTrue(run(weekly, label='success')) # Report failure cannot discard labels.
        self.assertFalse(run(weekly, pull='failure', label='skipped'))
        self.assertFalse(run(weekly, label='skipped'))
        self.assertTrue(run(am, scan='success', label='failure'))
        self.assertFalse(run(am, scan='failure'))     # Partial scans are not completed data.
        self.assertFalse(run(am, dry=True))

    def test_embedded_shell_and_python_parse_without_executing_jobs(self):
        bash = shutil.which('bash')
        if not bash and Path(r'C:\Program Files\Git\bin\bash.exe').exists():
            bash = r'C:\Program Files\Git\bin\bash.exe'
        if not bash:
            self.skipTest('bash parser unavailable on this platform')
        import ast
        for path in (ROOT / '.github' / 'workflows').glob('*.yml'):
            lines = path.read_text(encoding='utf-8').splitlines()
            for index, line in enumerate(lines):
                match = re.match(r'^(\s*)run: (.*)$', line)
                if not match:
                    continue
                indent, value = len(match[1]), match[2]
                if value == '|':
                    block = []
                    for candidate in lines[index + 1:]:
                        if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= indent:
                            break
                        block.append(candidate)
                    body = textwrap.dedent('\n'.join(block))
                else:
                    body = value
                body = re.sub(r'\$\{\{.*?\}\}', 'EXPR', body)
                with self.subTest(workflow=path.name, line=index + 1):
                    checked = subprocess.run([bash, '-n'], input=body + '\n', capture_output=True, text=True, encoding='utf-8')
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    for embedded in re.findall(r"python - <<'PY'\n(.*?)\nPY", body, re.S):
                        ast.parse(embedded)


if __name__ == '__main__':
    unittest.main()
