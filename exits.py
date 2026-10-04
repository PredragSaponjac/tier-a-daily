# -*- coding: utf-8 -*-
"""ONE exit rule for live monitoring AND research labels. Import it; never re-implement it.

WHY (external audit, 2026-10-04, findings F02 and F03):
  F02  monitor.py checked the target BEFORE the stop, so a daily bar that touched both was
       booked as a WIN; path_labels.py and every backtest booked the same bar as a LOSS.
       Two engines, two answers, one trade.
  F03  Both filled a stop at exactly the stop price. When a stock OPENS below its stop,
       every trade that session was below it, so the booked fill never traded.

This module is now the single definition. monitor.py (the live book) and path_labels.py
(the research labels and every exit shadow) both call resolve_bar(), so they cannot
disagree about how a bar resolves.

Known limit, stated rather than hidden: daily bars cannot order intraday prints. An
ambiguous bar is resolved conservatively as a STOP and FLAGGED, never silently.
"""


def resolve_bar(opn: float, high: float, low: float, T1: float, STOP: float):
    """Resolve one daily OHLC bar against a long position's target and stop.

    Returns (reason, exit_price, note), or None if the bar touches neither level.
      reason      'TP1' or 'STOP'
      exit_price  the price the exit is booked at
      note        '' for a clean hit, else why the fill is not simply the level
    """
    if opn <= STOP:
        # Gap through the stop: a resting stop executes at the OPEN, the first print.
        return ('STOP', opn, f'gapped through stop: opened {opn:.2f} below stop {STOP:.2f}')
    if opn >= T1:
        # Gap through the target: booked AT the target, not at the gap-up windfall.
        return ('TP1', T1, f'gapped through target at the open ({opn:.2f}); booked at target')
    hit_t1, hit_stop = high >= T1, low <= STOP
    if hit_t1 and hit_stop:
        return ('STOP', STOP, 'AMBIGUOUS bar touched both target and stop; booked STOP (conservative)')
    if hit_t1:
        return ('TP1', T1, '')
    if hit_stop:
        return ('STOP', STOP, '')
    return None
