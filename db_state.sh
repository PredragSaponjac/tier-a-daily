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
# 2026-10-04 (external audit F09/F10): the logic moved to db_state.py. This script
# used `gh release upload --clobber`, which DELETES the only copy before uploading
# the new one ("If the upload fails, the original assets will be lost" — gh's own
# help). Now every push is a new, immutably named, digest-verified snapshot; older
# generations are kept; and a push that would overwrite a newer generation than the
# one this checkout pulled is refused. See db_state.py.
#
# HARD FAILURE IS THE POINT: if the DB cannot be fetched we must ABORT, never run
# a scan against an empty database. An empty DB yields no skew history, so
# skew_change_5d is null, so no signal fires — a silent no-op that looks healthy.
set -euo pipefail
exec "${PYTHON:-python}" "$(dirname "$0")/db_state.py" "$@"
