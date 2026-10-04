# -*- coding: utf-8 -*-
"""skew_history.db durable storage as VERSIONED release snapshots (audit F09/F10, 2026-10-04).

The database is not in git (it crossed GitHub's 100MB file limit on 2026-08-25). It lives
as an asset on the release tagged `db-state`, and that asset is the ONLY durable copy.

WHAT WAS WRONG
  F09  Every AM, PM and weekly run stored the database with `gh release upload --clobber`.
       gh's own help: "existing assets are deleted before new assets are uploaded. If the
       upload fails, the original assets will be lost." The only copy was deleted first,
       every time.
  F10  Three workflows (AM scan, PM scan, weekly audit) each pull the whole file, change it
       and push the whole file. Nothing stopped a later push from erasing another run's work.

NOW
  push  uploads a NEW, immutably named snapshot (skewdb-<UTC time>-<run>.db) WITHOUT
        --clobber, so nothing is deleted before the new copy is safe; checks the server's
        sha256 digest of the upload against the local file; only then prunes generations
        beyond the newest KEEP. A failed upload leaves every earlier generation untouched.
        It REFUSES to push if the newest snapshot on the release is not the one this
        checkout pulled: another writer stored a newer database in between, and uploading
        would silently erase that work (a compare-and-swap on the generation).
  pull  downloads the newest snapshot (or the legacy single asset `skew_history.db` until
        the first versioned push exists), checks the transfer against the server's digest
        (a download cut off mid-transfer once produced "database disk image is malformed"),
        runs SQLite's quick_check and the table-population floor, and records which
        generation it pulled so push can prove nobody moved it.

HARD FAILURE IS THE POINT: if the DB cannot be fetched or verified we ABORT. An empty or
truncated DB yields no skew history, so no signal fires: a silent no-op that looks healthy.

Usage: python db_state.py pull | push | list      (db_state.sh is a thin wrapper)
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

TAG = os.environ.get('DB_STATE_TAG', 'db-state')
DB = os.environ.get('DB_STATE_FILE', 'skew_history.db')
LEGACY = 'skew_history.db'          # the single asset used until 2026-10 (replaced in place)
SNAP_RE = re.compile(r'^skewdb-\d{8}T\d{6}Z-[A-Za-z0-9_]+\.db$')
KEEP = int(os.environ.get('DB_STATE_KEEP', '6'))
MIN_BYTES = int(float(os.environ.get('DB_STATE_MIN_MB', '50')) * 1024 * 1024)
GEN_FILE = os.environ.get('DB_STATE_GEN_FILE', '.db_generation.json')
REQUIRED_TABLES = {'candidate_log': 1000, 'skew_daily': 1000}


class DBStateError(RuntimeError):
    pass


def _real_gh(*args: str) -> str:
    r = subprocess.run(['gh', *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise DBStateError(f'gh {" ".join(args[:2])} failed (exit {r.returncode}): '
                           f'{(r.stderr or r.stdout).strip()[:500]}')
    return r.stdout


GH = _real_gh       # preflight swaps in a fake release so this module is tested offline


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path, obj) -> None:
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def list_assets() -> list:
    return json.loads(GH('release', 'view', TAG, '--json', 'assets'))['assets']


def _done(a) -> bool:
    return a.get('state', 'uploaded') == 'uploaded'     # never use a half-uploaded asset


def snapshots(assets) -> list:
    """Versioned snapshots that finished uploading, NEWEST FIRST (names sort by UTC time)."""
    return sorted((a for a in assets if SNAP_RE.match(a['name']) and _done(a)),
                  key=lambda a: a['name'], reverse=True)


def newest(assets):
    snaps = snapshots(assets)
    if snaps:
        return snaps[0]
    legacy = [a for a in assets if a['name'] == LEGACY and _done(a)]
    return legacy[0] if legacy else None


def _digest_matches(asset, local_hex):
    """True / False against the server's sha256; None when the server reported none."""
    d = asset.get('digest') or ''
    if not d.startswith('sha256:'):
        return None
    return d.split(':', 1)[1].lower() == local_hex


def check_db(path) -> dict:
    size = os.path.getsize(path)
    if size < MIN_BYTES:
        raise DBStateError(f'{path} is only {size / 1048576:.1f} MB (floor '
                           f'{MIN_BYTES / 1048576:.0f} MB): truncated or empty')
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        try:
            qc = con.execute('PRAGMA quick_check').fetchone()[0]
        except sqlite3.Error as e:
            raise DBStateError(f'SQLite cannot read {path}: {e}')
        if qc != 'ok':
            raise DBStateError(f'SQLite quick_check failed on {path}: {qc}')
        counts = {}
        for t, floor in REQUIRED_TABLES.items():
            try:
                n = con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            except sqlite3.Error as e:
                raise DBStateError(f'table {t} unreadable: {e}')
            if n < floor:
                raise DBStateError(f'table {t} has only {n} rows (floor {floor}): '
                                   f'database looks empty')
            counts[t] = n
    finally:
        con.close()
    return {'bytes': size, **counts}


def _download(name: str, dest: str) -> None:
    GH('release', 'download', TAG, '-p', name, '-O', dest, '--clobber')


def pull() -> dict:
    a = newest(list_assets())
    if a is None:
        raise DBStateError(f'no database snapshot on release {TAG!r}')
    fd, tmp = tempfile.mkstemp(prefix='.db_pull_', suffix='.db',
                               dir=os.path.dirname(os.path.abspath(DB)))
    os.close(fd)
    try:
        print(f'[db-state] downloading {a["name"]} ({a.get("size", 0) / 1048576:.0f} MB) '
              f'from release {TAG} ...')
        _download(a['name'], tmp)
        local = sha256(tmp)
        ok = _digest_matches(a, local)
        if ok is False:
            raise DBStateError(f'download of {a["name"]} does not match the server digest '
                               f'(transfer corrupted); not using it')
        if ok is None:
            if a.get('size') is not None and os.path.getsize(tmp) != a['size']:
                raise DBStateError(f'download of {a["name"]} is {os.path.getsize(tmp)} bytes, '
                                   f'server says {a["size"]}')
            print('[db-state] note: server reported no digest; verified by size + SQLite only')
        info = check_db(tmp)
        os.replace(tmp, DB)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    _write_json(GEN_FILE, {'name': a['name'], 'sha256': local, 'bytes': info['bytes'],
                           'pulled_utc': _utcnow()})
    print(f'[db-state] OK {a["name"]}: ' + ', '.join(f'{k}={v:,}' for k, v in info.items()))
    if a['name'] == LEGACY:
        print('[db-state] (legacy single asset; the first push starts versioned snapshots)')
    return info


def _snapshot_name() -> str:
    run = os.environ.get('GITHUB_RUN_ID', 'local')
    if os.environ.get('GITHUB_RUN_ATTEMPT'):
        run += '_' + os.environ['GITHUB_RUN_ATTEMPT']
    run = re.sub(r'[^A-Za-z0-9_]', '_', run)
    return f'skewdb-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{run}.db'


def push() -> str | None:
    info = check_db(DB)
    gen = _read_json(GEN_FILE)
    if not gen or not gen.get('name'):
        raise DBStateError('no record of which generation this checkout pulled (run pull '
                           'first); refusing to push blind over whatever is newest')
    cur = newest(list_assets())
    cur_name = cur['name'] if cur else None
    if cur_name != gen['name']:
        raise DBStateError(f'the release moved on since this checkout pulled: pulled '
                           f'{gen["name"]}, newest is now {cur_name}. Another writer stored a '
                           f'newer database; uploading this copy would erase its work. '
                           f'NOT pushing.')
    local = sha256(DB)
    if local == gen.get('sha256'):
        print(f'[db-state] database unchanged since pull ({gen["name"]}); nothing to store')
        return None
    name = _snapshot_name()
    tmpdir = tempfile.mkdtemp(prefix='.db_push_', dir=os.path.dirname(os.path.abspath(DB)))
    try:
        path = os.path.join(tmpdir, name)
        shutil.copyfile(DB, path)
        if sha256(path) != local:
            raise DBStateError('the database changed while staging the upload')
        print(f'[db-state] uploading {name} ({info["bytes"] / 1048576:.0f} MB) to release '
              f'{TAG} as a NEW asset; nothing is deleted first ...')
        GH('release', 'upload', TAG, path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    assets = list_assets()
    up = next((a for a in assets if a['name'] == name), None)
    ok = None if up is None else _digest_matches(up, local)
    if up is not None and ok is None:            # no server digest: download and compare
        fd, back = tempfile.mkstemp(prefix='.db_verify_', suffix='.db',
                                    dir=os.path.dirname(os.path.abspath(DB)))
        os.close(fd)
        try:
            _download(name, back)
            ok = sha256(back) == local
        finally:
            os.remove(back)
    if up is None or not ok or up.get('size', info['bytes']) != info['bytes']:
        if up is not None:
            try:
                GH('release', 'delete-asset', TAG, name, '-y')
            except DBStateError as e:
                print(f'::error::could not remove the unverified upload {name}: {e}')
        raise DBStateError(f'upload of {name} could not be verified against the local '
                           f'database; removed it. The previous generation {gen["name"]} '
                           f'is untouched.')
    _write_json(GEN_FILE, {'name': name, 'sha256': local, 'bytes': info['bytes'],
                           'pushed_utc': _utcnow()})
    print(f'[db-state] stored and verified {name} (sha256 {local[:12]}...)')
    prune(assets)
    return name


def prune(assets) -> None:
    """Keep the newest KEEP versioned snapshots. The legacy asset is never touched."""
    for a in snapshots(assets)[KEEP:]:
        try:
            GH('release', 'delete-asset', TAG, a['name'], '-y')
            print(f'[db-state] pruned old generation {a["name"]}')
        except DBStateError as e:
            print(f'::warning::could not prune {a["name"]}: {e}')


def main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else ''
    try:
        if cmd == 'pull':
            pull()
        elif cmd == 'push':
            push()
        elif cmd == 'list':
            for a in list_assets():
                print(f'{a["name"]:48s} {a.get("size", 0) / 1048576:7.1f} MB  '
                      f'{a.get("createdAt") or a.get("created_at") or ""}  {a.get("state", "")}')
        else:
            print('usage: db_state.py pull|push|list', file=sys.stderr)
            return 2
    except DBStateError as e:
        print(f'::error::[db-state] FATAL: {e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
