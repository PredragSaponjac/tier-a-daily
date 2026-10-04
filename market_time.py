"""Regular NYSE sessions, including holidays, early closes and daylight saving.

Daily OHLC labels are session dates, not arbitrary UTC timestamps. All decision
clocks are aware and injectable so offline tests never depend on today's date.
"""
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo('America/New_York')


def now_eastern(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError('Market clock requires a timezone-aware datetime')
    return value.astimezone(EASTERN)


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


@lru_cache(maxsize=1)
def calendar():
    import exchange_calendars as xcals
    return xcals.get_calendar('XNYS', start='2020-01-01', end='2035-12-31')


def is_session(day):
    return bool(calendar().is_session(_date(day).isoformat()))


def session_completed(day, now=None):
    day = _date(day)
    if not is_session(day):
        return False
    close = calendar().session_close(day.isoformat()).to_pydatetime()
    return close <= now_eastern(now)


def last_completed_session(now=None):
    clock = now_eastern(now)
    day = calendar().date_to_session(clock.date().isoformat(), direction='previous')
    if not session_completed(day.date(), clock):
        day = calendar().previous_session(day)
    return day.date()


def next_session(day):
    return calendar().date_to_session((_date(day) + timedelta(days=1)).isoformat(),
                                      direction='next').date()


def sessions_between(start, end):
    if _date(start) > _date(end):
        return []
    return [x.date() for x in calendar().sessions_in_range(
        _date(start).isoformat(), _date(end).isoformat())]
