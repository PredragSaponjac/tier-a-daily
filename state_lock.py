"""Portfolio locking, durable delivery checkpoints, and one Eastern session date.

The local file lock coordinates processes in a checkout. GitHub's short
``tier-a-portfolio-writer`` job group coordinates separate runner checkouts;
the long scanner uses a different group. A file lock alone is not distributed.
Live workflow delivery sets DURABLE_STATE_CHECKPOINT=git: an outbox transition
must reach origin before the caller may send. Local/offline runs retain files
without making a network mutation.
"""
from contextlib import contextmanager
import datetime as dt
import os
from pathlib import Path
import subprocess
import time
from market_time import now_eastern

ROOT = Path(__file__).resolve().parent
STATE_PATHS = ('signals', 'open_positions.json', 'closed_trades.json', 'open_trades.json',
               'x_drafts', 'x_posted.log', 'last_scan_am.json',
               'last_scan_pm.json', 'audit_latest.json', 'audit_history',
               'inference_checkpoints', 'inference_protocol_lock.json')


class StateCheckpointError(RuntimeError):
    """A delivery transition did not reach its required durable checkpoint."""


def trading_date(now=None):
    return now_eastern(now).date()


@contextmanager
def portfolio_lock(path=None, timeout=60.0):
    """Exclusive local lock; never delete the file while another process waits."""
    target = Path(path) if path is not None else ROOT / '.git' / 'portfolio-state.lock'
    if path is None and not target.parent.is_dir():
        # A worktree's .git is a pointer file; git-path resolves its private metadata.
        resolved = subprocess.run(['git', 'rev-parse', '--git-path', 'portfolio-state.lock'],
                                  cwd=ROOT, capture_output=True, text=True)
        if resolved.returncode:
            raise StateCheckpointError('cannot locate portfolio lock metadata')
        target = Path(resolved.stdout.strip())
        if not target.is_absolute():
            target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('a+b') as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b'0'); fh.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                fh.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StateCheckpointError('timed out waiting for the portfolio writer')
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fh.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _git(*args, check=True):
    result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode:
        # Do not copy a remote URL or credentials from Git diagnostics into alerts.
        raise StateCheckpointError(f'git {args[0]} failed (exit {result.returncode}); '
                                   'delivery state is not confirmed durable')
    return result


def _approved(path):
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(ROOT.resolve())
        except ValueError:
            raise StateCheckpointError('checkpoint path escapes the repository')
    resolved = (ROOT / p).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        raise StateCheckpointError('checkpoint path escapes the repository')
    relative = resolved.relative_to(ROOT.resolve()).as_posix()
    if not any(relative == base or relative.startswith(base + '/') for base in STATE_PATHS):
        raise StateCheckpointError(f'unsupported checkpoint path: {relative}')
    return relative


def checkpoint(paths=None):
    """Persist approved state to origin in configured live workflows, or files locally.

    Caller holds portfolio_lock and the workflow owns the shared short writer job.
    This deliberately fails on a competing remote push instead of auto-merging a
    stale portfolio after deciding which positions to admit or which outcome to send.
    """
    mode = os.environ.get('DURABLE_STATE_CHECKPOINT', 'local')
    if mode == 'local':
        return
    if mode != 'git':
        raise StateCheckpointError('invalid DURABLE_STATE_CHECKPOINT mode')
    branch = os.environ.get('GITHUB_REF_NAME') or _git('branch', '--show-current').stdout.strip()
    if not branch:
        raise StateCheckpointError('Git checkpoint requires an explicit writable branch')
    selected = [_approved(p) for p in (STATE_PATHS if paths is None else paths)]
    staged = _git('diff', '--cached', '--name-only').stdout.splitlines()
    for existing in staged:
        _approved(existing)  # refuse accidentally staged source/credentials
    for relative in selected:
        exists = (ROOT / relative).exists()
        tracked = _git('ls-files', '--', relative).stdout.strip()
        if exists or tracked:
            _git('add', '-A', '--', relative)
    dirty = _git('diff', '--cached', '--quiet', check=False)
    if dirty.returncode not in (0, 1):
        raise StateCheckpointError('cannot inspect staged delivery state')
    if dirty.returncode == 1:
        _git('config', 'user.name', 'tier-a-daily-bot')
        _git('config', 'user.email', 'bot@tieradaily.local')
        _git('commit', '-m', f'Durable delivery checkpoint {trading_date().isoformat()}')
    # Push even with no staged difference: a preceding commit may have failed to push.
    _git('push', 'origin', f'HEAD:refs/heads/{branch}')


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['date']:
        print(trading_date().isoformat())
    elif sys.argv[1:] == ['checkpoint']:
        checkpoint()
    else:
        raise SystemExit('usage: python state_lock.py date|checkpoint')
