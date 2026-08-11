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
    import data_quality as DQ
    # A date far past the DB's last row must report UNAVAILABLE, never a fake std.
    r = DQ.assess_noise('AAPL', '2099-01-01')
    hard('noise gate refuses to compute across a gap',
         r['skew_std'] is None and 'UNAVAILABLE' in r['reason'], str(r))


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
        committed = (f'git add {base}' in wf) or (f'git add {art}' in wf)
        gitignored = base.rstrip('/') in (REPO / '.gitignore').read_text(encoding='utf-8')
        hard(f'artifact "{art}" survives the runner',
             committed or gitignored,
             'code writes it but NO workflow git-adds it — it will be discarded')

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


def main():
    print('PREFLIGHT — tier-a-daily')
    for fn in (check_gates, check_data_quality, check_formatters,
               check_wiring, check_silent_failures, check_network):
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
