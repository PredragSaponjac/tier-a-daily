# Tier A Daily

Daily options-flow-enriched signal service. Built on top of the Skew Tracker AR methodology + UW composite filter (research_uw_picker_v1).

## What it does

After the daily Skew Tracker PM scan produces Tier A candidates, this bot:
1. Pulls UW options flow + dark pool data for each candidate
2. Computes the composite filter score (NCP entry z-score, call/put OI 10d % change, dark pool footprint)
3. Applies vetoes (earnings within 14d, liquidity floor, recent news)
4. Ranks remaining candidates by composite score
5. Posts the top-ranked signal to Telegram with: entry, T1/T2/T3, stop, reasoning, track record, disclaimer
6. (Phase 2) Monitors intraday for TP1 or stop hits, auto-posts close alerts
7. (Phase 2) Logs every signal + outcome + MAE/MFE to public Google Sheet

## Exit rules (TP1-default)

- **TP1 (+10%):** auto-close signal — bot's default exit
- **TP2 (+11%)**, **TP3 (+20%):** shown in post for traders who want to hold longer (manual at their own discretion)
- **Stop (-7%):** auto-close signal

Backtest: 67% TP1 hit rate, 71% profitable, +5.20% avg/trade across n=63 Tier A historical.

## Composite filter (research_uw_picker_v1)

Score 0-4 across:
1. `net_call_premium` entry-day z-score ≤ -0.5 (vs prior 10d baseline)
2. `call_OI` 10-day % change ≤ -5%
3. `put_OI` 10-day % change ≤ 0%
4. Bonus: dark pool 10d large blocks ≥ 30

Theory: structural unwind + entry capitulation precedes exceptional bottoms (3 Bonferroni-significant findings, n=177 historical).

## Setup

See `SETUP.md` for: Telegram channel, Google Sheet, GitHub repo, env vars.

## CLI

```bash
python main.py                          # run against latest Tier A scan
python main.py --scan-date 2026-05-18   # test against specific date
python main.py --dry-run                # compute + format but don't send
```

## Status

- **Phase 1 (now):** local MVP, Telegram alerts to private chat for testing
- **Phase 2 (next):** GH Actions cron, public Telegram channel, X auto-posting, Google Sheet logging with MAE/MFE
- **Phase 3 (~Month 3-4):** subscription tier ($200/mo after track record established + lawyer consult for publisher exemption)
