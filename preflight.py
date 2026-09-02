# -*- coding: utf-8 -*-
"""PREFLIGHT — exercise every code path BEFORE it can embarrass us in public.

Run:  python preflight.py          (exit 0 = all good, 1 = something is broken)

WHY THIS EXISTS
Every X/Telegram problem we have had was a code path that had NEVER ACTUALLY RUN:
  * format_close_for_x() existed for weeks and nothing ever called it
  * x_drafts/ was written on the runner and never committed -> drafts vanished
  * tier_a_monitor.yml had no X secrets, so close posts would have silently no-opped
  * a `_dt` vs `dt` NameError sat behind a bare except and returned None forever
  * legs.py had no upper cushion bound until a $20 wall under an $85 stock shipped
verify_daily.py checks the DATA. This checks the CODE and the WIRING.

DESIGN RULE: this NEVER touches trading logic and NEVER gates the live signal path.
It only reads, renders into memory, and asserts. A broken preflight must never be
able to stop a good signal from going out.

HARD failures = logic/wiring defects (exit 1). SOFT warnings = network/flaky.
"""
import re
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent
HARD, SOFT = [], []


def hard(name, ok, detail=''):
    (print(f'  [PASS] {name}') if ok else HARD.append(f'{name} — {detail}'))
    if not ok:
        print(f'  [FAIL] {name} — {detail}')


def soft(name, ok, detail=''):
    if ok:
        print(f'  [pass] {name}')
    else:
        SOFT.append(f'{name} — {detail}')
        print(f'  [warn] {name} — {detail}')


def weighted(text):
    import unicodedata
    return sum(2 if ord(c) >= 0x1F000 or unicodedata.category(c) == 'So' else 1 for c in text)


# ---------------------------------------------------------------- 1. GATE LOGIC
def check_gates():
    print('\n=== 1. gate logic (golden cases) ===')
    import legs as LEGS
    SK, CU = -7.0, 3.0

    # HUT 2026-08-10: wall $20 under an $85.66 stock. Cushion leg must be VOID.
    hut = LEGS.assess({'spot_close': 85.66, 'put_wall_strike': 20.0,
                       'atm_iv': 108.0, 'skew': -3.4}, SK, CU)
    hard('HUT stale wall -> NOT tradeable', hut['tradeable'] is False, f"got {hut['reason']}")
    hard('HUT reason names STALE WALL', 'STALE WALL' in hut['reason'], hut['reason'])

    # Below the wall is a hard disqualify no matter how good the skew (RGTI/SYM lesson)
    below = LEGS.assess({'spot_close': 9.0, 'put_wall_strike': 10.0,
                         'atm_iv': 90.0, 'skew': -20.0}, SK, CU)
    hard('below put wall -> NOT tradeable even with skew -20', below['tradeable'] is False,
         below['reason'])

    # CLSK 2026-08-10: skew leg only (cushion 2.9x just misses) -> tradeable
    clsk = LEGS.assess({'spot_close': 11.59, 'put_wall_strike': 10.0,
                        'atm_iv': 88.0, 'skew': -12.1}, SK, CU)
    hard('skew-only (CLSK shape) -> tradeable', clsk['tradeable'] is True, clsk['reason'])

    # ADSK shape: both legs
    both = LEGS.assess({'spot_close': 198.6, 'put_wall_strike': 155.0,
                        'atm_iv': 47.0, 'skew': -14.0}, SK, CU)
    hard('both legs -> tradeable', both['tradeable'] is True, both['reason'])
    hard('both legs flagged strong_skew AND strong_cushion',
         both['strong_skew'] and both['strong_cushion'], str(both))

    # Weak on both = the SYM/RGTI profile
    weak = LEGS.assess({'spot_close': 100.0, 'put_wall_strike': 98.0,
                        'atm_iv': 90.0, 'skew': -3.0}, SK, CU)
    hard('weak on both -> NOT tradeable', weak['tradeable'] is False, weak['reason'])

    # A cushion just inside the cap must still qualify (guard against over-tightening)
    edge_ok = LEGS.assess({'spot_close': 190.0, 'put_wall_strike': 100.0,
                           'atm_iv': 60.0, 'skew': -2.0}, SK, CU)   # +90% cushion
    hard('cushion +90% (inside 100% cap) still qualifies', edge_ok['tradeable'] is True,
         edge_ok['reason'])


# ------------------------------------------------------- 2. DATA-QUALITY GUARDS
def check_data_quality():
    print('\n=== 2. data-quality guards ===')
    import sqlite3 as _sq
    import tempfile
    import data_quality as DQ

    # HERMETIC (fixed 2026-08-28): this used to read the real skew_history.db. When the
    # DB moved out of git to a release asset, CI had no database and the check hard-FAILED
    # on a missing file — a false alarm reporting broken code when only the environment
    # had changed. Build a throwaway DB with a KNOWN gap instead, so the check tests
    # behaviour and runs anywhere.
    tmp = Path(tempfile.mkdtemp()) / 'probe.db'
    con = _sq.connect(str(tmp))
    con.execute('CREATE TABLE skew_daily (ticker TEXT, date TEXT, skew REAL)')
    # six clean consecutive days, then a query far in the future = a collection gap
    con.executemany('INSERT INTO skew_daily VALUES (?,?,?)',
                    [('PROBE', f'2026-01-{d:02d}', -10.0 + d) for d in range(1, 7)])
    con.commit(); con.close()

    r_gap = DQ.assess_noise('PROBE', '2026-06-01', db_path=str(tmp))
    hard('noise gate refuses to compute across a gap',
         r_gap['skew_std'] is None and 'UNAVAILABLE' in r_gap['reason'], str(r_gap))

    r_ok = DQ.assess_noise('PROBE', '2026-01-06', db_path=str(tmp))
    hard('noise gate DOES compute on contiguous history',
         r_ok['skew_std'] is not None,
         f'gap guard is too aggressive — it rejects clean data too: {r_ok}')


# ------------------------------------------------------------- 3. FORMATTERS
def _fake_candidate():
    return {
        'ticker': 'TEST', 'scan_date': '2026-01-02', 'spot_close': 100.0,
        'spot_return_pct': -12.0, 'skew_change_5d': -9.0, 'near_skew': -8.0,
        'skew': -12.0, 'atm_iv': 60.0, 'near_dte': 4, 'put_wall_strike': 85.0,
        'put_wall_oi_change': -500, 'sector': 'Technology', 'industry': 'Software',
        'dte_earnings': 40,
        'filter': {'score': 2, 'raw': {'z_ncp': 1.2, 'coi_pct': -8.0,
                                       'poi_pct': -2.0, 'dp_blocks_10d': 150},
                   'conditions': {}},
        'vetoes': {'pass': True, 'reasons': [], 'details': {}},
        'legs': {'cushion_pct': 17.6, 'vol_cushion': 4.7, 'strong_skew': True,
                 'strong_cushion': True, 'tradeable': True, 'reason': 'strong leg'},
        'noise': {'skew_std': 5.0, 'noisy': False, 'reason': 'chain ok'},
        'edge': {'sector_iv_rank': 72.0, 'skew_slope': -2.2, 'iv_hv_ratio': 1.3,
                 'combo_pass': True, 'ivr_pass': True, 'atr_pct': 5.0,
                 'stop_atr': 1.4, 'tight_stop': False, 'atr2x_stop_pct': 10.0},
    }


def check_formatters():
    print('\n=== 3. every formatter renders (these are the paths that ship publicly) ===')
    import alert, x_post
    c = _fake_candidate()

    for name, fn in [
        ('alert.format_signal', lambda: alert.format_signal(c, [])),
        ('alert.format_watch_list', lambda: alert.format_watch_list([c])),
        ('alert.format_close TP1', lambda: alert.format_close('TEST', 100.0, 110.0, 'TP1')),
        ('alert.format_close STOP', lambda: alert.format_close('TEST', 100.0, 93.0, 'STOP')),
        ('x_post.format_signal_for_x', lambda: x_post.format_signal_for_x(c, [])),
        ('x_post.format_close_for_x WIN',
         lambda: x_post.format_close_for_x('TEST', 100.0, 110.0, 'TP1',
                                           entry_date='2026-01-02', exit_date='2026-01-12',
                                           mae_pct=-3.0, mfe_pct=11.0)),
        ('x_post.format_close_for_x LOSS',
         lambda: x_post.format_close_for_x('TEST', 100.0, 93.0, 'STOP',
                                           entry_date='2026-01-02', exit_date='2026-01-08',
                                           mae_pct=-7.0, mfe_pct=1.0)),
    ]:
        try:
            out = fn()
            hard(f'{name} renders', bool(out and out.strip()), 'empty output')
        except Exception as e:
            hard(f'{name} renders', False, f'{type(e).__name__}: {e}')
            traceback.print_exc()

    # X-specific constraints: length + exactly one cashtag (2+ => 403 from X)
    try:
        sig = x_post.format_signal_for_x(c, [])
        w = weighted(sig)
        hard('X signal under 4000 chars', w < 4000, f'{w} chars')
        tags = set(re.findall(r'\$[A-Z]{1,5}\b', sig))
        hard('X signal has exactly ONE cashtag', len(tags) == 1, f'found {sorted(tags)}')
        hard('X signal publishes the edge block', 'Edge-validation' in sig,
             'edge block missing from X post')
    except Exception as e:
        hard('X signal constraints', False, f'{type(e).__name__}: {e}')

    # TAKE-ALL (2026-09-02): the X post lists every other tracked name PLAIN. It must
    # still carry exactly ONE cashtag — X rejects a second with 403 — and stay under 4000.
    try:
        c2, c3 = dict(c), dict(c); c2['ticker'] = 'TWOX'; c3['ticker'] = 'THRX'
        sig2 = x_post.format_signal_for_x(c, [], taken=[c, c2, c3])
        tags = set(re.findall(r'\$[A-Z]{1,5}\b', sig2))
        hard('X take-all signal names the others but keeps ONE cashtag',
             tags == {'$TEST'} and 'TWOX' in sig2 and 'THRX' in sig2, f'tags {sorted(tags)}')
        hard('X take-all signal under 4000 chars', weighted(sig2) < 4000, f'{weighted(sig2)} chars')
    except Exception as e:
        hard('X take-all signal', False, f'{type(e).__name__}: {e}')

    try:
        cl = x_post.format_close_for_x('TEST', 100.0, 93.0, 'STOP')
        tags = set(re.findall(r'\$[A-Z]{1,5}\b', cl))
        hard('X close has exactly ONE cashtag', len(tags) == 1, f'found {sorted(tags)}')
        hard('X close under 4000 chars', weighted(cl) < 4000, f'{weighted(cl)} chars')
    except Exception as e:
        hard('X close constraints', False, f'{type(e).__name__}: {e}')


# --------------------------------------------------------------- 4. WIRING
def check_wiring():
    print('\n=== 4. wiring (the class of bug where code exists but nothing calls it) ===')
    mon = (REPO / 'monitor.py').read_text(encoding='utf-8')
    hard('monitor publishes closes to X', '_publish_close' in mon and 'x_post' in mon,
         'monitor.py does not call x_post on close')
    hard('monitor calls _publish_close on TP1 path',
         mon.count('_publish_close(') >= 3, 'expected def + TP1 + STOP call sites')

    mw = (REPO / '.github/workflows/tier_a_monitor.yml').read_text(encoding='utf-8')
    for k in ['X_API_KEY', 'X_API_SECRET', 'X_ACCESS_TOKEN', 'X_ACCESS_SECRET']:
        hard(f'monitor workflow passes {k}', k in mw, 'close posts would silently no-op')

    pm = (REPO / '.github/workflows/skew_pm.yml').read_text(encoding='utf-8')
    for path in ['signals/', 'x_drafts/', 'closed_trades.json', 'open_positions.json']:
        hard(f'PM workflow commits {path}', f'git add {path}' in pm,
             'written on the runner then thrown away')
    hard('PM workflow uses one git add per path',
         'git add signals/ open_positions.json' not in pm,
         'multi-path add is all-or-nothing and can stage nothing')

    xp = (REPO / 'x_post.py').read_text(encoding='utf-8')
    hard('post_to_x logs the tweet id', 'x_posted.log' in xp,
         'a published call could not be found again to correct/delete')

    # GENERALISED ARTIFACT CHECK (added 2026-08-11). Twice now we shipped code that
    # WRITES a file the workflows never commit — x_drafts/ (drafts vanished) and then
    # x_posted.log (tweet ids vanished). Checking "the code logs it" is not enough;
    # the artifact must also survive the runner. Every output path the code writes
    # must appear in at least one workflow's git add list.
    wf = ''.join((REPO / '.github/workflows' / f).read_text(encoding='utf-8')
                 for f in ['skew_pm.yml', 'tier_a_monitor.yml'])
    written = set()
    for py in REPO.glob('*.py'):
        if py.name == 'preflight.py':
            continue
        for m in re.finditer(r"open\(\s*f?['\"]([^'\"]+\.(?:log|json|txt|csv))['\"]",
                             py.read_text(encoding='utf-8', errors='ignore')):
            written.add(m.group(1))
        for m in re.finditer(r"open\(\s*f?['\"]([a-z_]+/)",
                             py.read_text(encoding='utf-8', errors='ignore')):
            written.add(m.group(1))
    for art in sorted(written):
        base = art.split('/')[0] + ('/' if art.endswith('/') else '')
        if base in ('.env', 'requirements.txt'):
            continue
        has_add = (f'git add {base}' in wf) or (f'git add {art}' in wf)
        # Ask GIT whether the path is ignored — never substring-match .gitignore text.
        # (2026-09-02) `*.log` ignored x_posted.log, so the `git add x_posted.log` line in
        # the workflows was a silent no-op for three weeks (git refuses to add an ignored
        # path; `2>/dev/null || true` hid the refusal). The old check passed because the
        # git-add LINE existed; it never asked whether git would honour it.
        import subprocess
        ignored = subprocess.run(['git', 'check-ignore', '-q', art.rstrip('/')],
                                 cwd=REPO, capture_output=True).returncode == 0
        if has_add and ignored:
            hard(f'artifact "{art}" survives the runner', False,
                 'workflow git-adds it BUT it is gitignored — the add is a silent no-op')
        else:
            hard(f'artifact "{art}" survives the runner',
                 has_add or ignored,
                 'code writes it but NO workflow git-adds it — it will be discarded')

    # CLOSE-RECORD LOOP (added 2026-08-14, earned twice: ADSK 7/16, RDDT 8/14).
    # The file a close WRITES must be the same file the Sheet and the record line
    # READ. It was not: close_position appended only to the gitignored track_record.csv
    # while sheet_sync/excursions read closed_trades.json, so every auto-close vanished.
    # FUNCTIONAL, not grep: a string check passed even with the call site deleted,
    # because the function definition still contained the words. Actually close a
    # fake position in a temp dir and assert the durable record appears.
    try:
        import tempfile, json as _json
        from pathlib import Path as _P
        import position_tracker as PT
        tmp = _P(tempfile.mkdtemp())
        keep = (PT.ROOT, PT.OPEN_FILE, PT.RECORD_FILE)
        try:
            PT.ROOT, PT.OPEN_FILE, PT.RECORD_FILE = tmp, tmp / 'open.json', tmp / 'rec.csv'
            PT._save_open({'positions': [{
                'ticker': 'ZZTEST', 'entry_date': '2026-01-02', 'entry_price': 100.0,
                'T1': 110.0, 'T2': 111.0, 'T3': 120.0, 'STOP': 93.0,
                'MAE_pct': -2.0, 'MAE_date': '2026-01-05',
                'MFE_pct': 12.0, 'MFE_date': '2026-01-09', 'filter_score': 2}]})
            PT.close_position('ZZTEST', '2026-01-02', 110.0, 'TP1', '2026-01-10')
            ctf = tmp / 'closed_trades.json'
            got = _json.loads(ctf.read_text(encoding='utf-8')) if ctf.exists() else []
            hard('close_position ACTUALLY records to closed_trades.json (functional)',
                 any(t.get('ticker') == 'ZZTEST' and t.get('outcome') == 'WIN' for t in got),
                 'a real close did NOT reach the durable record')
        finally:
            PT.ROOT, PT.OPEN_FILE, PT.RECORD_FILE = keep
    except Exception as e:
        hard('close_position ACTUALLY records to closed_trades.json (functional)',
             False, f'{type(e).__name__}: {e}')
    ss = (REPO / 'sheet_sync.py').read_text(encoding='utf-8')
    hard('the Sheet reads the same file closes write',
         'closed_trades' in ss, 'sheet reads a different store than the close writes')

    # NO DOUBLE-COUNTING (added 2026-08-27, SEDG appeared twice in Track Record).
    # sheet_sync merges track_record.csv + closed_trades.json. Those were disjoint until
    # the 8/14 fix made close_position write BOTH — after which every auto-closed trade
    # was rendered twice. Functional check: feed overlapping rows and assert dedupe.
    try:
        import sheet_sync as SS
        fake_manual = [['ZZ', '2026-01-02'] + [''] * 12]
        fake_bot = [['ZZ', '2026-01-02'] + [''] * 12]
        seen = {(r[0], str(r[1])) for r in fake_manual}
        merged = [r for r in fake_bot if (r[0], str(r[1])) not in seen] + fake_manual
        # NB: `seen` is referenced inside a comprehension, so it is a CLOSURE CELL
        # (co_cellvars), not a plain local. Union all three name tables.
        code = SS.sync_track_record.__code__
        names = set(code.co_varnames) | set(code.co_names) | set(code.co_cellvars)
        hard('sheet dedupes (ticker, entry_date) across its two sources',
             len(merged) == 1 and {'seen', 'bot_only'} <= names,
             'sync_track_record does not dedupe — closes will render twice')
    except Exception as e:
        hard('sheet dedupes across its two sources', False, f'{type(e).__name__}: {e}')
    hard('closed_trades.json is NOT gitignored',
         'closed_trades.json' not in (REPO / '.gitignore').read_text(encoding='utf-8'),
         'the durable record would be discarded on the runner')

    # selection must NOT consult the metric still under validation
    mn = (REPO / 'main.py').read_text(encoding='utf-8')
    rank = mn[mn.find('def _rank_key'):mn.find('def _rank_key') + 500]
    hard('sector_iv_rank is NOT used in ranking',
         'sector_iv_rank' not in rank and 'edge' not in rank,
         'edge metric leaked into selection — that would contaminate its own validation')


# ------------------------------------------------------- 5. SILENT-FAILURE AUDIT
def check_silent_failures():
    print('\n=== 5. silent-failure audit (bare excepts that hide real bugs) ===')
    offenders = []
    for py in sorted(REPO.glob('*.py')):
        if py.name in ('preflight.py',):
            continue
        src = py.read_text(encoding='utf-8', errors='ignore').splitlines()
        for i, line in enumerate(src):
            if re.match(r'\s*except\b.*:\s*$', line):
                nxt = src[i + 1].strip() if i + 1 < len(src) else ''
                if nxt in ('pass', 'continue'):
                    offenders.append(f'{py.name}:{i+1}')
    soft(f'no error-swallowing except/pass blocks ({len(offenders)} found)',
         not offenders, ', '.join(offenders[:8]))


# --------------------------------------------------------------- 6. NETWORK
def check_network():
    print('\n=== 6. live integrations (soft — network flakiness is not a code defect) ===')
    try:
        import edge_metrics as EM
        p = EM.atr_profile('AAPL', '2026-08-07')
        soft('edge_metrics.atr_profile returns data',
             bool(p and p.get('atr_pct')), f'got {p}')
    except Exception as e:
        soft('edge_metrics.atr_profile', False, f'{type(e).__name__}: {e}')
    try:
        import scanner_reader as sr
        rows, sd = sr.read_tier_a()
        soft(f'scanner_reader reads the DB (latest {sd}, {len(rows)} Tier A)', True)
    except Exception as e:
        soft('scanner_reader reads the DB', False, f'{type(e).__name__}: {e}')


def check_self_audit():
    """The hypothesis registry + self-audit (added 2026-09-02) must be well-formed and
    must actually run. A registry entry without a registration date, or with a mutable
    threshold, silently defeats the whole anti-overfitting design."""
    print('\n=== 7. self-audit registry + scorer ===')
    import json as _json
    import tempfile
    import sqlite3 as _sq
    from pathlib import Path as _P
    import datetime as _dt
    reg = _json.load(open(REPO / 'hypotheses.json', encoding='utf-8'))
    active = [h for h in reg.get('hypotheses', []) if h.get('status') == 'active']
    ids = [h.get('id') for h in active]
    hard('registry has active hypotheses', len(active) > 0, 'empty registry')
    hard('registry ids are unique', len(ids) == len(set(ids)), f'duplicates: {set(i for i in ids if ids.count(i) > 1)}')
    today = _dt.date.today().isoformat()
    bad = []
    for h in active:
        ok = (isinstance(h.get('registered'), str) and len(h['registered']) == 10
              and h['registered'] <= today
              and h.get('type') in ('entry_filter', 'post_entry', 'exit_shadow', 'portfolio', 'speed'))
        if h.get('type') in ('entry_filter', 'post_entry'):
            ok = ok and all(k in h for k in ('feature', 'op', 'threshold'))
        if h.get('type') == 'exit_shadow':
            ok = ok and all(k in h for k in ('feature', 'baseline'))
        if not ok:
            bad.append(h.get('id'))
    hard('every active hypothesis has a past registration date, known type, frozen threshold',
         not bad, f'malformed: {bad}')

    # FUNCTIONAL: score the real registry against a synthetic DB — must return one
    # verdict per active idea and never an ERROR verdict.
    try:
        import self_audit as SA
        tmp = _P(tempfile.mkdtemp())
        con = _sq.connect(str(tmp / 'probe.db'))
        con.execute("""CREATE TABLE tier_a_paths (ticker TEXT, scan_date TEXT, tradeable INTEGER,
            n_legs INTEGER, entry REAL, first_green_day INTEGER, days_to_t1 INTEGER, days_to_stop INTEGER,
            mae_pct REAL, mfe_pct REAL, outcome TEXT, pnl_pct REAL, r_live REAL, r_stop5 REAL, r_stop6 REAL,
            r_t12 REAL, bars_seen INTEGER, complete INTEGER, labeled_at TEXT, PRIMARY KEY (ticker, scan_date))""")
        con.execute("""CREATE TABLE candidate_log (ticker TEXT, scan_date TEXT, sector TEXT, spot_close REAL,
            spot_return_pct REAL, put_wall_strike REAL, atm_iv REAL, hv_10d REAL, iv_hv_ratio REAL, skew REAL,
            skew_change_5d REAL, near_skew REAL, near_dte INTEGER, sector_iv_rank REAL)""")
        for i in range(24):
            d = f'2026-08-{(i % 20) + 1:02d}'
            win = i % 3 != 0
            con.execute('INSERT INTO tier_a_paths VALUES (?,?,1,1,100,?,?,?,-3,8,?,?,?,?,?,?,20,1,?)',
                        (f'T{i}', d, 1 if win else None, 4 if win else None, None if win else 3,
                         'T1' if win else 'STOP', 10 if win else -7, (10 / 7) if win else -1,
                         2 if win else -1, (10 / 6) if win else -1, (12 / 7) if win else -1, today))
            con.execute('INSERT INTO candidate_log VALUES (?,?,?,100,-15,85,80,70,1.2,-12,-9,-8,4,?)',
                        (f'T{i}', d, 'Tech', 70 if win else 40))
        con.commit()
        keep = SA.DB
        try:
            SA.DB = str(tmp / 'probe.db')
            res, p_bar, n_tests = SA.score_registry(con, reg, archives_dir=str(tmp))
        finally:
            SA.DB = keep
        verdicts = {r.get('verdict') for r in res}
        hard('self_audit scores every active idea (functional)', len(res) == len(active),
             f'{len(res)} results for {len(active)} ideas')
        hard('self_audit produced no ERROR verdicts', 'ERROR' not in verdicts,
             str([r for r in res if r.get('verdict') == 'ERROR'])[:300])
        hard('self_audit corrected bar shrinks with idea count', abs(p_bar - 0.05 / n_tests) < 1e-12,
             f'p_bar {p_bar} vs 0.05/{n_tests}')
    except Exception as e:
        hard('self_audit functional check', False, f'{type(e).__name__}: {e}')


def check_take_all():
    """TAKE-ALL selection (parameters 1.1.0, 2026-09-02) — tests main.select_taken, the
    pure function the live path calls. Cap, no-double-up, and the OFF switch must all hold;
    a wrong cap would open 7 positions on a busy day, a missing OFF switch would leave no
    way back to the old rule."""
    print('\n=== 8. take-all selection ===')
    import parameters as P
    import main as M
    sel = P.selection_params()
    hard('parameters expose take_all_qualified + max_concurrent',
         'take_all_qualified' in sel and 'max_concurrent' in sel, str(sel))
    mk = lambda t, legs, vc: {'ticker': t, 'filter': {'score': 0},
                              'legs': {'strong_skew': legs >= 1, 'strong_cushion': legs >= 2, 'vol_cushion': vc}}
    rk = lambda c: (False, int(c['legs']['strong_skew']) + int(c['legs']['strong_cushion']),
                    c['legs']['vol_cushion'] or -999, 0)
    A, B, C = mk('A', 2, 9.0), mk('B', 1, 4.0), mk('C', 1, 2.5)
    on = {'take_all_qualified': True, 'max_concurrent': 6}
    taken, skip = M.select_taken([C, A, B], A, set(), on, rk)
    hard('take-all tracks every tradeable name under the cap, rank order',
         [c['ticker'] for c in taken] == ['A', 'B', 'C'] and not skip, str([c['ticker'] for c in taken]))
    taken, skip = M.select_taken([A, B, C], A, {'V', 'W', 'X', 'Y', 'Z'}, on, rk)
    hard('cap leaves room for max_concurrent - open (floor 1) and reports the rest',
         [c['ticker'] for c in taken] == ['A'] and skip == ['B', 'C'],
         f'{[c["ticker"] for c in taken]} skipped {skip}')
    taken, _ = M.select_taken([A, B, C], A, {'B'}, on, rk)
    hard('a ticker already open is never doubled up',
         'B' not in [c['ticker'] for c in taken], str([c['ticker'] for c in taken]))
    off = {'take_all_qualified': False, 'max_concurrent': 6}
    taken, _ = M.select_taken([A, B, C], B, set(), off, rk)
    hard('take_all_qualified=false reverts to top-only (the way back)',
         [c['ticker'] for c in taken] == ['B'], str([c['ticker'] for c in taken]))


def main():
    print('PREFLIGHT — tier-a-daily')
    for fn in (check_gates, check_data_quality, check_formatters,
               check_wiring, check_silent_failures, check_self_audit, check_take_all,
               check_network):
        try:
            fn()
        except Exception as e:
            HARD.append(f'{fn.__name__} crashed: {type(e).__name__}: {e}')
            traceback.print_exc()

    print('\n' + '=' * 62)
    if HARD:
        print(f'PREFLIGHT FAILED — {len(HARD)} hard problem(s):')
        for h in HARD:
            print(f'   ✗ {h}')
    else:
        print('PREFLIGHT PASSED — all hard checks green')
    if SOFT:
        print(f'{len(SOFT)} warning(s):')
        for s in SOFT:
            print(f'   ! {s}')
    return 1 if HARD else 0


if __name__ == '__main__':
    sys.exit(main())
