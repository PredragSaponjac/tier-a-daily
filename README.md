# Tier A Daily

Daily options-skew reversal signal. Built on the Skew Tracker methodology: each weekday PM
scan of ~660 optionable names flags Tier A candidates, which this bot gates, tracks and
publishes. Every entry and every close (wins and losses alike) goes to Telegram, X and a
public Google Sheet.

## What it does (parameters 1.1.0)

1. Reads the day's Tier A candidates from the PM scan (bullish skew reversal after a
   washout: near-dated skew ≤ −7, skew change ≤ −7 and spot down ≥ 8% over the scan's
   lookback window, put-wall OI not rising). The column is named `skew_change_5d`, but the
   2026-10-04 audit (F01) found the window actually spans 9 sessions; all evidence to date
   was gathered on that 9-session window.
2. Applies the gates: at least one strong leg (structural skew or vol cushion), not below the
   put wall, stale-wall cap, chain-noise filter, earnings/liquidity vetoes
3. **Takes every name that passes** (since 2026-09-02), up to 6 open positions, each sized at
   1/6 of the book. Ranking only decides who yields to the cap; it has no measured skill.
4. Monitors intraday: exits at **+10% (TP1)** or **−7% (stop)**, nothing else. TP2/TP3 are shown
   for readers who hold longer, at their own discretion.
5. Logs every signal, decision and outcome; a weekly self-audit scores registered ideas on
   new data only and never changes the rules by itself.

## How exits are booked

One shared rule (`exits.py`) for the live monitor, the research labels and the legacy
backtest/exit-model scripts: a daily bar touching both levels is booked as the stop (an
assumption, flagged on every such row: a daily bar cannot say which came first); a gap
through the stop fills at the open; a gap through the target is booked at the target. No
time limit anywhere: a position exits only at the target or the stop, like the live book.

MAE/MFE ("worst drawdown held through", "best unrealised reached") exclude prices beyond the
exit fill. On the exit day the OTHER side of the bar is still included, because daily data
cannot show whether it came before or after the exit; for that one day the numbers are
bounds, not exact. The monitor also timestamps each new extreme it observes live.

## Performance

See the live track record (Google Sheet, linked in every post) and `audit_latest.json`.
The May 2026 backtest headline previously shown here (n=63, +5.2%/trade) predates the
current gates and universe and is superseded. As of the 2026-10-04 external audit, the
all-qualifier research cohort does not yet establish an edge statistically: its 95%
interval for mean P&L per trade includes zero.

## Data and infrastructure

- `skew_history.db` lives as versioned, digest-verified snapshots on the `db-state` release
  (`db_state.py`); earlier generations are kept, and a stale writer cannot overwrite a newer one.
  A writer that loses a simultaneous upload withdraws its copy and retries.
- AM scan, PM scan and the weekly audit share one writer queue; the PM run has a guarded
  backup run and marks itself complete only after records and database are stored.
- Delivery runs from OUTBOXES: each day's decision is archived before anything is sent
  (`signals/`, with an immutable copy per run in `signals/runs/`), and each close is
  recorded before it is announced (`closed_trades.json`). Every channel's result is stored,
  so a retry resumes the same decision instead of deciding or posting again; X is retried
  only after a definite rejection and never when the post may already be live.
- `preflight.py` exercises the live code paths and the audit fixes on every push.

## CLI

```bash
python main.py --require-today          # production: today's scan only
python main.py --dry-run                # compute + format, send nothing
python main.py --scan-date 2026-09-29   # READ-ONLY replay of a past date (not point-in-time)
python main.py --scan-date D --live     # a replay that really sends/tracks (rarely right)
python db_state.py pull | push | list   # database snapshots on the release
python preflight.py                     # verify before shipping
```

The Unusual Whales composite filter (research_uw_picker_v1) is inactive since the UW
subscription was cancelled; it only ever set ranking priority, never a hard gate.

See `SETUP.md` for Telegram, Google Sheet, GitHub and environment variables.
