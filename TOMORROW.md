# TOMORROW (Thu 5/28) — first live day runbook

Goal: get the first real signal (or "no trade today" message) to your Telegram, with track-record + MAE/MFE logging starting today.

## Tonight (Wed 5/27 evening) — one-time setup, ~5 min

```bash
cd C:/Users/18329/Downloads/tier-a-daily

# Install deps
/c/Users/18329/anaconda3/python.exe -m pip install requests yfinance python-dotenv

# Verify pipeline works locally (uses today's 5/27 scan — likely 0 candidates)
PYTHONIOENCODING=utf-8 /c/Users/18329/anaconda3/python.exe main.py --dry-run

# Smoke-test Telegram delivery (live send, harmless "no trade today")
PYTHONIOENCODING=utf-8 /c/Users/18329/anaconda3/python.exe main.py
# → Check your Telegram (chat 8660012381) for the message
```

If Telegram message arrives, you're good. If not: check `.env` has correct token + chat_id.

## Tomorrow (Thu 5/28) — three commands at three times

### 9:30 AM CT — AM Skew Tracker (your existing routine, no change)

```bash
cd C:/Users/18329/Downloads/skew-tracker
PYTHONIOENCODING=utf-8 /c/Users/18329/anaconda3/python.exe skew_tracker.py > skew_manual_am.log 2>&1
# (Wait ~45 min for completion)
git add skew_history.db skew_manual_am.log && git commit -m "Skew tracker snapshot 2026-05-28 AM (manual)"
PYTHONIOENCODING=utf-8 /c/Users/18329/anaconda3/python.exe label_candidates.py
git add skew_history.db && git commit -m "Label candidates forward returns 2026-05-28 AM" && git push
```

### 2:45 PM CT — PM Skew Tracker (your existing routine)

```bash
cd C:/Users/18329/Downloads/skew-tracker
PYTHONIOENCODING=utf-8 /c/Users/18329/anaconda3/python.exe skew_tracker.py > skew_manual_pm.log 2>&1
# (~45 min)
git add skew_history.db skew_manual_pm.log && git commit -m "Skew tracker snapshot 2026-05-28 PM (manual)"
PYTHONIOENCODING=utf-8 /c/Users/18329/anaconda3/python.exe label_candidates.py
git add skew_history.db && git commit -m "Label candidates forward returns 2026-05-28 PM" && git push
```

### 3:45 PM CT — NEW: Tier A Daily bot

```bash
cd C:/Users/18329/Downloads/tier-a-daily
PYTHONIOENCODING=utf-8 /c/Users/18329/anaconda3/python.exe main.py --require-today
```

**What happens:**
- Reads Tier A candidates from skew_history.db (just-committed PM snapshot)
- Pulls UW data for each candidate (~10-30 API calls)
- Computes composite filter score 0-4
- Applies vetoes (earnings within 14d, liquidity floor)
- Picks top survivor with score >= 1 (configurable in parameters.json)
- Sends Telegram alert with: setup, UW breakdown, entry/T1/T2/T3/stop, conviction stars, honest track record
- Adds signal to `open_positions.json` for intraday monitoring
- If no candidates qualify: sends "no trade today" message

### Every 15 min during market hours (next day) — auto-close monitor

Run via Windows Task Scheduler OR just manually after market close each day:

```bash
cd C:/Users/18329/Downloads/tier-a-daily
PYTHONIOENCODING=utf-8 /c/Users/18329/anaconda3/python.exe monitor.py
```

**What happens:**
- For each open position: pull yfinance daily OHLC since entry
- Update MAE (worst drawdown) and MFE (best peak) in real-time
- If High ≥ T1: auto-close at TP1, send 📍 alert, log to track_record.csv
- If Low ≤ stop: auto-close at stop, send 🛑 alert, log
- If 10+ trading days since entry: timeout close at last price

## Verification — how to know it's working

### Check 1: Telegram delivered
- Open Telegram → see the bot's message
- Format should have ⭐ conviction stars, UW breakdown, T1/T2/T3/stop levels

### Check 2: Position tracked
```bash
cat open_positions.json
# Should show the new signal with MAE/MFE fields at 0
```

### Check 3: After monitor runs
```bash
cat open_positions.json
# MAE/MFE should be updated based on intraday action

# After a close event:
cat track_record.csv
# Should have a row for the closed signal with all metrics
```

## What lives where

```
C:/Users/18329/Downloads/tier-a-daily/
├── .env                    ← API keys (gitignored, NEVER commit)
├── parameters.json         ← all tunable parameters (committed)
├── open_positions.json     ← currently-monitored signals (live state)
├── track_record.csv        ← closed signals log (the dataset for self-learning)
├── uw_cache/               ← API response cache (gitignored)
├── *.py                    ← code
└── *.md                    ← docs
```

## Common issues

**Bot says "0 Tier A candidates today"** — normal on days with no signal (like 5/27 had 0). Bot still sends "no trade today" to confirm it ran.

**Telegram send: FAILED** — check `.env` has correct `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

**UW error 429 (rate limit)** — shouldn't happen at our usage (0.16% of daily limit). If it does, the client auto-retries.

**Earnings veto hits a name you wanted** — that's working correctly per the rule. If you disagree with the 14-day buffer, edit `parameters.json` → `vetoes.earnings_within_days`.

**Position never closes despite price hitting T1** — make sure `monitor.py` is running. Without it, positions sit open forever.

## Optional: Windows Task Scheduler

To fully automate (no manual command needed):

1. Open Task Scheduler → Create Task
2. Triggers: Daily, 3:45 PM (one for main.py), every 15 min from 8:35 AM to 3:00 PM for monitor.py
3. Action: Start program → `C:\Users\18329\anaconda3\python.exe`
4. Arguments: `C:\Users\18329\Downloads\tier-a-daily\main.py --require-today`
5. Start in: `C:\Users\18329\Downloads\tier-a-daily`

OR — Phase 2 GitHub Actions (set up in next session) handles this server-side, no PC required.

## Phase 2 — coming soon (after MVP validates)

- Push to GitHub + GH Actions cron (PC can be off)
- Public Telegram channel + X auto-posting
- Public Google Sheet for track record (subscriber transparency)
- `monthly_self_review.py` (adaptive parameter tuning)

For now: just run the 3 commands tomorrow, verify Telegram delivery, and we're tracking from day one.
