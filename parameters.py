"""Single source of truth for all tunable parameters.

Loaded from parameters.json. All modules import from here, never hardcode constants.
Phase 3's monthly_self_review.py is the only thing that should write to parameters.json
(and only after user approval via Telegram).
"""
import json
from pathlib import Path

_PARAMS_FILE = Path(__file__).parent / 'parameters.json'
_CACHE = None


def _load():
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(_PARAMS_FILE.read_text())
    return _CACHE


def reload():
    """Force re-read from disk (used by self-review module after writing changes)."""
    global _CACHE
    _CACHE = None
    return _load()


# --- Convenience accessors ---

def min_filter_score() -> int:
    return _load()['entry']['min_filter_score']


def selection_params() -> dict:
    """The ≥1-strong-leg gate config (defaults if section missing)."""
    s = _load().get('selection', {})
    return {
        'require_strong_leg': s.get('require_strong_leg', True),
        'strong_skew_max': s.get('strong_skew_max', -7.0),
        'strong_vol_cushion_min': s.get('strong_vol_cushion_min', 3.0),
        'skew_noise_std_max': s.get('skew_noise_std_max', 20.0),
    }


def tp_pcts() -> dict:
    e = _load()['exits']
    return {'tp1': e['tp1_pct'], 'tp2': e['tp2_pct'], 'tp3': e['tp3_pct']}


def stop_pct() -> float:
    return _load()['exits']['stop_pct']


def time_stop_days() -> int:
    return _load()['exits']['time_stop_days']


def scale_out_pcts() -> dict:
    e = _load()['exits']
    return {'tp1': e['scale_at_tp1_pct'], 'tp2': e['scale_at_tp2_pct'], 'tp3': e['scale_at_tp3_pct']}


def earnings_buffer_days() -> int:
    return _load()['vetoes']['earnings_within_days']


def min_total_oi() -> int:
    return _load()['vetoes']['min_total_oi']


def filter_thresholds() -> dict:
    return _load()['filter_thresholds']


def version() -> str:
    return _load().get('version', '?')


def options_volume_days() -> int:
    return _load().get('uw_pulls', {}).get('options_volume_days_back', 20)


def darkpool_days() -> int:
    return _load().get('uw_pulls', {}).get('darkpool_days_back', 10)


def all_params() -> dict:
    return _load()
