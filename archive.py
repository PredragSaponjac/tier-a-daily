"""Daily archive — write signals/YYYY-MM-DD.json after each run.

This is the SINGLE MOST IMPORTANT file for long-term self-improvement:
every Tier A candidate seen, every UW score, every veto, every pick decision
preserved per day. After 30+ days, enables retrospective backtests:
  - "What if we'd used different filter thresholds?"
  - "What if we'd used score >= 2 instead of >= 1?"
  - "Which UW conditions actually predicted winners?"

Without UW re-pulls, since the raw metric values are saved.
"""
import hashlib
import json
import os
import tempfile
import uuid
import math
from pathlib import Path
from datetime import datetime, date, timezone

ARCHIVE_DIR = Path(__file__).parent / 'signals'
ARCHIVE_DIR.mkdir(exist_ok=True)


def run_id() -> str:
    """Identity of THIS run: the GitHub run + attempt, or a local timestamp."""
    if os.environ.get('GITHUB_RUN_ID'):
        return f"gh{os.environ['GITHUB_RUN_ID']}_{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}_{uuid.uuid4().hex}"
    return 'local_' + uuid.uuid4().hex


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text); fh.flush(); os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _safe(value):
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if hasattr(value, 'item'):
        return _safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def save_draft(scan_date, name, text):
    scan_date = date.fromisoformat(scan_date).isoformat()
    if not str(name).replace('_', '').replace('-', '').isalnum():
        raise ValueError('Invalid draft identity')
    path = ARCHIVE_DIR.parent / 'x_drafts' / f'{scan_date}_{name}.txt'
    _atomic_write(path, text)
    return path


def _parameters_sha256() -> str | None:
    try:
        return hashlib.sha256((Path(__file__).parent / 'parameters.json').read_bytes()).hexdigest()
    except OSError:
        return None


def archive_daily_run(scan_date: str, enriched: list[dict], picked_ticker: str | None,
                       min_score_used: int, parameters_version: str,
                       notes: str = '', taken_tickers: list | None = None,
                       delivery: dict | None = None, outbox: dict | None = None,
                       context: dict | None = None) -> Path:
    """Write a complete record of today's run to signals/YYYY-MM-DD.json.

    picked_ticker  = the top-ranked name (kept for the self-audit's picked-vs-skipped
                     ledger, which needs to know what the OLD one-per-day rule would do).
    taken_tickers  = every name ADMITTED by today's decision (TAKE-ALL regime from
                     2026-09-02). Under the old regime this is just [picked_ticker].
                     Since 2026-10-04 positions open only after the Telegram announcement
                     succeeds; delivery['positions'] says whether they are tracked yet.
    delivery/outbox (re-audit R2/R6, 2026-10-04): the decision is archived BEFORE any send,
                     with the exact messages and position specs; main.run_outbox() delivers
                     from this record and stores each channel's status, so a retry resumes
                     the SAME decision instead of deciding (and posting) again.
    context        = what the decision was made against (portfolio before, etc.; R7).

    Every call ALSO writes an immutable copy to signals/runs/<date>_<run>.json (R7): the
    date file is the latest view, the runs/ files are the history it is derived from.
    """
    scan_date = date.fromisoformat(scan_date).isoformat()
    import parameters as P
    data_generation = None
    if os.environ.get('DURABLE_STATE_CHECKPOINT') == 'git':
        import db_state
        data_generation = db_state.generation()
    record = {
        'scan_date': scan_date,
        'run_at': datetime.utcnow().isoformat() + 'Z',
        'run_id': run_id(),
        'parameters_version': parameters_version,
        'parameters_sha256': _parameters_sha256(),
        'parameters_snapshot': _safe(P.all_params()),
        'candidate_inputs': _safe(enriched),
        'execution_model': 'next_regular_open_completed_daily_conservative_v2',
        'data_generation': data_generation,
        'min_filter_score_used': min_score_used,
        'picked_ticker': picked_ticker,
        # AUDIT F18: distinguish "not supplied" (None -> legacy single pick) from an
        # explicit EMPTY list. `if taken_tickers` treated [] as missing and substituted
        # [picked_ticker], recording a position that was never opened.
        'taken_tickers': (list(taken_tickers) if taken_tickers is not None
                          else ([picked_ticker] if picked_ticker else [])),
        'notes': notes,
        'context': context or {},
        'candidates': [],
    }
    if delivery is not None:
        record['delivery'] = delivery
    if outbox is not None:
        record['outbox'] = outbox

    for c in enriched:
        f = c.get('filter', {}) or {}
        v = c.get('vetoes', {}) or {}
        record['candidates'].append({
            'ticker': c['ticker'],
            # Skew Tracker fields (these will let us recompute is_tier_a if filter changes)
            'spot_close': c['spot_close'],
            'spot_return_pct': c['spot_return_pct'],
            'skew_change_5d': c['skew_change_5d'],
            'near_skew': c['near_skew'],
            'near_dte': c['near_dte'],
            'put_wall_strike': c['put_wall_strike'],
            'put_wall_oi_change': c['put_wall_oi_change'],
            'sector': c.get('sector'),
            'industry': c.get('industry'),
            'dte_earnings': c.get('dte_earnings'),
            # UW filter outputs (preserved so backtests don't need re-pulls)
            'filter_score': f.get('score'),
            'filter_raw': f.get('raw', {}),
            'filter_conditions_pass': {k: v['pass'] for k, v in f.get('conditions', {}).items()},
            # Vetoes
            'veto_pass': v.get('pass'),
            'veto_reasons': v.get('reasons', []),
            'veto_details': {
                'earnings_next': v.get('details', {}).get('earnings', {}).get('next_earnings'),
                'liquidity_oi': v.get('details', {}).get('liquidity', {}).get('total_oi'),
            },
            # Edge-validation metrics (added 2026-07-28) — research logging only
            'edge': c.get('edge'),
            # RE-AUDIT R7 (2026-10-04): the gate inputs as computed AT decision time, so
            # eligibility can be reconstructed without a later DB requery (AM/PM rescans
            # overwrite the day's rows). None = not computed on this path.
            'structural_skew': c.get('skew'),
            'atm_iv': c.get('atm_iv'),
            'legs': c.get('legs'),
            'noise': c.get('noise'),
            # F04 provenance: the entry is the scan's cached last price; the scan stores no
            # per-quote timestamp, so the decision time is the only as-of available.
            'quote_source': c.get('quote_source', 'legacy source; quote as-of unknown'),
            'quote_asof': c.get('quote_asof'),
            'observed_at': c.get('observed_at'),
            'screen_version': c.get('screen_version'),
            'window_sessions': c.get('window_sessions'),
        })

    text = json.dumps(_safe(record), indent=2, allow_nan=False)
    file_path = ARCHIVE_DIR / f'{scan_date}.json'
    _atomic_write(file_path, text)
    runs = ARCHIVE_DIR / 'runs'
    runs.mkdir(exist_ok=True)
    snap = runs / f"{scan_date}_{record['run_id']}.json"
    if snap.exists():                       # same run deciding twice (e.g. --resend): keep both
        snap = runs / f"{scan_date}_{record['run_id']}_{datetime.utcnow():%H%M%S}.json"
    _atomic_write(snap, text)
    return file_path


def update_delivery(scan_date: str, **fields) -> dict:
    """Record channel results on the day's archived decision (atomic). Returns the record."""
    scan_date = date.fromisoformat(scan_date).isoformat()
    path = ARCHIVE_DIR / f'{scan_date}.json'
    rec = json.loads(path.read_text(encoding='utf-8'))
    rec.setdefault('delivery', {}).update(fields)
    event = {'run_id': rec.get('run_id'), 'scan_date': scan_date,
             'recorded_at': datetime.now(timezone.utc).isoformat(), 'delivery_update': _safe(fields)}
    event_path = ARCHIVE_DIR / 'events' / f'{scan_date}_{uuid.uuid4().hex}.json'
    _atomic_write(event_path, json.dumps(event, indent=2, allow_nan=False))
    _atomic_write(path, json.dumps(_safe(rec), indent=2, allow_nan=False))
    return rec


def load_archive(scan_date: str) -> dict | None:
    """Load a specific day's archive."""
    scan_date = date.fromisoformat(scan_date).isoformat()
    file_path = ARCHIVE_DIR / f'{scan_date}.json'
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text(encoding='utf-8'))


def list_archives() -> list[str]:
    """List all archived scan dates."""
    return sorted([p.stem for p in ARCHIVE_DIR.glob('*.json')])


if __name__ == '__main__':
    archives = list_archives()
    print(f'Daily archives: {len(archives)}')
    for d in archives[-10:]:
        a = load_archive(d)
        n = len(a.get('candidates', []))
        pick = a.get('picked_ticker') or '(none)'
        print(f'  {d}: {n} Tier A candidates, picked {pick}')
