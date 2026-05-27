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
