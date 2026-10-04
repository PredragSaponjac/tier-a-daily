"""Offline deployment preflight. Behavioral regressions live in tests/.

This validates syntax, configuration, durable records and workflow contracts.
It never reads credentials, contacts providers, sends messages or mutates state.
Run `python -m unittest discover -s tests -v` for recovery/execution regressions.
"""
import ast
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parent


def validate():
    problems = []
    checked = 0
    for path in sorted(ROOT.glob('*.py')):
        try:
            ast.parse(path.read_text(encoding='utf-8'), filename=path.name)
            checked += 1
        except (OSError, SyntaxError) as exc:
            problems.append(f'{path.name}: {type(exc).__name__}')
    for name in ('parameters.json', 'hypotheses.json', 'open_positions.json', 'closed_trades.json'):
        try:
            json.loads((ROOT/name).read_text(encoding='utf-8'))
            checked += 1
        except (OSError, ValueError) as exc:
            problems.append(f'{name}: {type(exc).__name__}')
    try:
        import parameters as P
        import position_tracker as PT
        p = P.all_params()
        assert p['screen']['lookback_sessions'] == 9
        assert p['screen']['sigma_time_basis'] == 'trading_sessions'
        assert p['entry']['execution_model'] == 'next_regular_open'
        assert p['vetoes']['unknown_policy'] == 'block'
        assert p['exits']['time_stop_days'] is None
        assert p['exits']['tp1_pct'] > 0 > p['exits']['stop_pct']
        assert P.selection_params()['max_concurrent'] > 0
        PT.list_open()  # strict reader; no writes
        closed = json.loads((ROOT/'closed_trades.json').read_text(encoding='utf-8'))
        if not isinstance(closed, list):
            raise ValueError('closed trade record must be a list')
        keys = [(x.get('ticker'), x.get('signal_date',x.get('entry_date'))) for x in closed]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate canonical closed identities')
        checked += 1
    except Exception as exc:
        problems.append(f'Configuration or portfolio validation: {type(exc).__name__}: {exc}')
    flows = {}
    for path in sorted((ROOT/'.github'/'workflows').glob('*.yml')):
        try:
            # YAML 1.1 safe_load interprets GitHub "on" as a boolean. BaseLoader
            # preserves the actual workflow key and treats expressions as data.
            value = yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
            assert isinstance(value.get('jobs'), dict) and value['jobs']
            for schedule in value.get('on',{}).get('schedule',[]):
                assert schedule.get('timezone') == 'America/New_York'
            flows[path.stem] = value
            checked += 1
        except Exception as exc:
            problems.append(f'{path.name}: invalid workflow contract ({type(exc).__name__})')
    try:
        pm, monitor = flows['skew_pm'], flows['tier_a_monitor']
        prep, signal = pm['jobs']['prepare'], pm['jobs']['signal']
        assert prep['concurrency']['group'] == 'tier-a-db-writer'
        assert signal['concurrency']['group'] == 'tier-a-portfolio-writer'
        mj = next(iter(monitor['jobs'].values()))
        assert mj['concurrency']['group'] == 'tier-a-portfolio-writer'
        for job in (prep,signal,mj):
            assert job['concurrency']['cancel-in-progress'] == 'false'
            assert job['concurrency']['queue'] == 'max'
        assert 'git' in signal['env']['DURABLE_STATE_CHECKPOINT']
        assert mj['env']['DURABLE_STATE_CHECKPOINT'] == 'git'
        assert prep['env']['DB_STATE_SERIALIZED'] == 'github-actions:tier-a-db-writer'
        checked += 1
    except Exception as exc:
        problems.append(f'Durable writer workflow contract: {type(exc).__name__}')
    try:
        from self_audit import inference_status
        registry = json.loads((ROOT/'hypotheses.json').read_text(encoding='utf-8'))
        integrity = inference_status(registry)
        if not integrity.get('ok'):
            problems.append('Inference protocol lock: '+integrity.get('detail','missing or stale'))
        else:
            checked += 1
    except ImportError:
        problems.append('Inference protocol integrity API unavailable')
    except Exception as exc:
        problems.append(f'Inference protocol lock: {type(exc).__name__}: {exc}')
    tracked = subprocess.run(['git','ls-files','--','.env'],cwd=ROOT,capture_output=True,text=True)
    if tracked.returncode or tracked.stdout.strip():
        problems.append('Cannot establish that the credential file is untracked')
    else:
        checked += 1
    return checked, problems


def main():
    checked, problems = validate()
    for problem in problems:
        print('::error::'+problem)
    print(f'PREFLIGHT: {checked} offline configuration/source/workflow checks; {len(problems)} problems')
    return int(bool(problems))


if __name__ == '__main__':
    sys.exit(main())
