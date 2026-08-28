#!/usr/bin/env bash
# Database state transfer — skew_history.db lives as a RELEASE ASSET, not in git.
#
# WHY: the DB was committed to git daily since March. By 2026-08-25 it crossed
# GitHub's hard 100MB file limit, so every push was rejected — and because the
# workflows ended their push with `|| true`, the runs still reported SUCCESS.
# Three days of scans (8/25-8/27) were computed and thrown away before the
# heartbeat caught it. Versioning it had also pushed .git to 5.2GB.
#
# A release asset has a 2GB limit, does not bloat git history, and downloads in
# ~4s vs cloning a 5GB repo.
#
# HARD FAILURE IS THE POINT: if the DB cannot be fetched we must ABORT, never run
# a scan against an empty database. An empty DB yields no skew history, so
# skew_change_5d is null, so no signal fires — a silent no-op that looks healthy.
set -euo pipefail

TAG="db-state"
DB="skew_history.db"
MIN_BYTES=$((50 * 1024 * 1024))   # sanity floor; the real DB is ~100MB

case "${1:-}" in
  pull)
    echo "[db-state] downloading $DB from release $TAG ..."
    gh release download "$TAG" -p "$DB" --clobber
    SZ=$(stat -c%s "$DB" 2>/dev/null || stat -f%z "$DB")
    echo "[db-state] got $((SZ / 1048576)) MB"
    if [ "$SZ" -lt "$MIN_BYTES" ]; then
      echo "[db-state] FATAL: $DB is only $((SZ / 1048576)) MB — refusing to scan against a truncated database."
      exit 1
    fi
    # structural check: the tables the pipeline needs must exist and be populated
    python - <<'PY'
import sqlite3, sys
c = sqlite3.connect('skew_history.db')
for t in ('candidate_log', 'skew_daily'):
    try:
        n = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    except Exception as e:
        print(f'[db-state] FATAL: table {t} unreadable: {e}'); sys.exit(1)
    if n < 1000:
        print(f'[db-state] FATAL: {t} has only {n} rows — database looks empty/corrupt'); sys.exit(1)
    print(f'[db-state] {t}: {n:,} rows')
print('[db-state] integrity OK')
PY
    ;;
  push)
    SZ=$(stat -c%s "$DB" 2>/dev/null || stat -f%z "$DB")
    echo "[db-state] uploading $DB ($((SZ / 1048576)) MB) to release $TAG ..."
    if [ "$SZ" -lt "$MIN_BYTES" ]; then
      echo "[db-state] FATAL: refusing to upload a $((SZ / 1048576)) MB database — that would destroy the good copy."
      exit 1
    fi
    gh release upload "$TAG" "$DB" --clobber
    echo "[db-state] upload complete"
    ;;
  *)
    echo "usage: db_state.sh {pull|push}" >&2
    exit 2
    ;;
esac
