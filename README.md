# Tier A Daily

Daily stock-screen research and paper position tracking. The bot sends alerts and
records assumed outcomes; it does not place broker orders or establish profitability.

Audit findings, implemented remedies and verification limits are recorded in
[AUDIT_REMEDIATION.txt](AUDIT_REMEDIATION.txt).

## Prospective execution contract (v2, from 2026-10-05)

- The existing washout screen uses **nine trading-session intervals**, not five.
  Ten consecutive XNYS session observations are required. Sigma annualization uses
  the same session count. Legacy SQLite column `skew_change_5d` remains an alias;
  `screen_version` and `window_sessions` identify the actual calculation.
- Earnings, options OI and chain-noise gates must have verified data. Unknown values
  block admission. UW is supplementary ranking information, not a required gate.
- Eligible names reserve up to six slots, including pending entries. Each paper slot
  represents 1/6 of the book. Ranking and take-all benefits remain unproven.
- A signal records a reference quote and its provenance. New paper entries use the
  **next regular-session open**; a missing opening bar blocks activation rather than
  substituting a later price. Existing trades keep their recorded entry assumptions.
- Monitoring and research use completed regular-session daily OHLC. A target/stop
  dual touch is marked ambiguous and conservatively booked as STOP. Gap-down stops
  fill at the opening price; target gaps book the target. These are explicit paper
  assumptions, not recovered intraday order or actual brokerage executions.
- Positions have no timeout. Unresolved research paths are censored, not counted as
  realized wins/losses. Stops/targets retain +10%/-7% default widths.
- Prices use a consistent split basis and price-only returns; dividends are recorded
  separately. Daily OHLC cannot recover exact exit-bar excursions, so future MAE/MFE
  reports show bounds. Historical records remain labeled as legacy/unverified.

XNYS holidays, early closes and daylight saving are modeled with exchange_calendars.
All scheduled market-related jobs use `America/New_York`, including GitHub cron.

## Durable state and delivery

Decisions, full candidate inputs, parameter snapshots, portfolio context and exact
message bodies are archived **before delivery**. Immutable run records and delivery
transition events accompany the latest `signals/YYYY-MM-DD.json` view.

Live workflows checkpoint pending/inflight/acknowledged delivery state to Git before
continuing. The short signal and monitor jobs share a portfolio writer queue; the
long scanner uses a separate database writer queue. A refresh or checkpoint failure
stops delivery. Local processes use a file lock as well.

Definite rejection can be retried. A timeout, interrupted inflight send or other
unknown result needs receipt reconciliation; the bot does not blindly resend it.
Entry receipts can be reconciled without sending anything:

```powershell
python outbox_cli.py 2026-10-05 telegram --receipt 123 --evidence "Checked message in channel"
# Only after verifying that nothing was delivered:
python outbox_cli.py 2026-10-05 x --not-sent --evidence "Verified no matching publication"
python outbox_cli.py 2026-10-05 resume  # resumes saved entry decision, including an older date
# A close is keyed by its signal date and ticker:
python outbox_cli.py 2026-10-05 telegram --close-ticker TEST --receipt 124 --evidence "Checked close message"
```

Close outboxes resume on the ordinary monitor run and are retained in the
canonical closed record, even when the last open position has been removed.
Required delivery or Sheet failure keeps pipeline completion false. X automatic
publication requires `ENABLE_X_AUTOPOST=true`; otherwise entry drafts are explicit
manual-review output.

Schedules in Eastern time: AM scan 10:30; PM scan 16:05 with a 17:15 backup;
close monitor 16:15 and 17:30; weekly research Friday 18:30; heartbeat 21:30.
These jobs review completed bars; they do not enforce intraday brokerage stops.

SQLite release backups are unique, digest/integrity-verified snapshots. The previous
copy is retained until a new copy is verified. The generation comparison is a stale
writer precheck, not atomic server CAS: supported uploads require the serialized
`tier-a-db-writer` workflow. Completion markers identify both the stored database
snapshot and the original archived decision generation. Heartbeat checks the exact
validated generation rather than any recently uploaded asset.

## Research and inference

Older cohorts are preserved as descriptive evidence, separately from the new price,
screen and execution contracts. Next-open path inputs and engine versions are
recorded; incomplete/missing inputs fail visibly. Weekly scores are descriptive.
No historical small-sample p-value or weekly repeated look can promote a rule.

The frozen prospective protocol starts on 2026-10-05, ends admissions on 2027-03-31
and observes through 2027-06-30. A checkpoint retains immutable inputs/results and
clustered uncertainty; calibrated policy promotion requires explicit review. The
protocol lock binds the registry and source/config hashes. Changing a frozen engine
invalidates promotion eligibility until a separately documented new protocol.
Stop shadows report percentage P&L and R using a common live denominator, matching
fixed-dollar slots. Forward calendar-day close benchmarks are separate from executable
next-open paths and from live admitted, capital-constrained portfolio performance.

The old n=63 / 67% / +5.2% public backtest footer has been removed. Historical cohorts,
live paper records and a net portfolio return are different quantities. None establishes
a prospective net edge under this version.

## Verification and commands

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests -v
python preflight.py
python main.py --dry-run
python main.py --scan-date 2026-09-29  # archived as-of preview; no current-world queries
python monitor.py --dry-run          # read-only completed-session preview
```

A dated preview requires its recorded archive; missing historical inputs are reported,
not fabricated from today's calendar/chains. Historical dates cannot create live entries.
The large database remains on the GitHub `db-state` release, outside Git.

Before production rollout, rotate the historically exposed Telegram token with
BotFather, update the GitHub secret, then verify the first new snapshot, label coverage,
checkpoint acknowledgements and final marker. Token revocation cannot be established
by a clean present-day secret scan. This repository does not revoke external credentials.
