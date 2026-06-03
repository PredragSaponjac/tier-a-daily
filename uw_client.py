"""Thin UW API client — auth + cached GET. Adapted from orca-v3/uw_flow.py."""
import os
import json
import time
import requests
from pathlib import Path

BASE_URL = "https://api.unusualwhales.com"
CACHE_DIR = Path(__file__).parent / 'uw_cache'
CACHE_DIR.mkdir(exist_ok=True)
_LAST_CALL = [0.0]
# UW limits: 120 req/min, 20,000 req/day.
# 0.55s pause = ~109/min, gives 9% safety buffer under the 120/min cap.
RATE_LIMIT_SECONDS = 0.55

# Daily-quota tripwire. A 429 whose body mentions the DAILY limit means every
# subsequent call today will also fail — so we flip this flag and stop wasting
# retries. Downstream (uw_filter / main) reads it to tell "UW quota exhausted"
# apart from "genuinely no data", so the bot never prints a false NO TRADE when
# it simply couldn't see the flow. Reset at the start of each fresh run.
_QUOTA_HIT = [False]


def quota_exhausted() -> bool:
    """True if a daily-request-limit 429 was seen during this process run."""
    return _QUOTA_HIT[0]


def reset_quota_flag():
    """Clear the daily-quota tripwire (call once at the start of a run)."""
    _QUOTA_HIT[0] = False


def _rate_limit():
    elapsed = time.time() - _LAST_CALL[0]
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    _LAST_CALL[0] = time.time()


def _get(endpoint: str, params: dict = None, timeout: int = 30, max_retries: int = 2):
    api_key = os.environ.get('UW_API_KEY', '')
    if not api_key:
        raise RuntimeError('UW_API_KEY env var not set')
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {api_key}'}
    url = f'{BASE_URL}{endpoint}'
    for attempt in range(max_retries + 1):
        _rate_limit()
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                body = (resp.text or '').lower()
                if 'daily' in body or 'daily_request_limit' in body:
                    # Daily cap hit — retrying is futile; trip the tripwire and
                    # bail so callers can report "quota exhausted", not "no data".
                    if not _QUOTA_HIT[0]:
                        print('  [uw] DAILY request limit hit — flow scoring unavailable for the rest of today.')
                    _QUOTA_HIT[0] = True
                    return None
                # Per-minute burst — back off and retry.
                time.sleep(2.0)
                continue
            if resp.status_code in (403, 404):
                return None  # historical-data unavailable or missing
            print(f'  [uw] {resp.status_code} on {endpoint}: {resp.text[:120]}')
            return None
        except requests.exceptions.RequestException as e:
            if attempt == max_retries:
                print(f'  [uw] error on {endpoint}: {e}')
                return None
            time.sleep(1.0)
    return None


def cached_get(endpoint: str, params: dict = None, cache_date: str = None):
    """GET with on-disk cache keyed by endpoint + params.

    Args:
      cache_date: If set, included in cache key so each day forces a fresh pull.
                  Use for ROLLING / TIME-SENSITIVE endpoints (e.g., options-volume
                  history where today's row matters).
                  OMIT for IMMUTABLE historical endpoints (e.g., darkpool with
                  explicit ?date=YYYY-MM-DD — that date's prints are fixed forever).
    """
    cache_key = endpoint.replace('/', '_')
    if params:
        for k, v in sorted(params.items()):
            cache_key += f'_{k}{v}'
    if cache_date:
        cache_key += f'_cd{cache_date}'
    cache_key = cache_key.strip('_').replace(':', '')[:200]
    cache_file = CACHE_DIR / f'{cache_key}.json'
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass
    data = _get(endpoint, params)
    if data is not None:
        try:
            cache_file.write_text(json.dumps(data))
        except Exception:
            pass
    return data
