# Phase 2 — Full Automation Setup

GitHub repo: https://github.com/PredragSaponjac/tier-a-daily
Phase 1 MVP: shipped. Phase 2: adds GH Actions automation, public Telegram channel, Google Sheet track record, X auto-posting, retrospective backtest tool.

## ✅ What's already DONE (auto)

- [x] Repo created at github.com/PredragSaponjac/tier-a-daily (public)
- [x] Code pushed (Phase 1 + Phase 2 foundation)
- [x] GH Actions workflows defined (`.github/workflows/`)
- [x] 3 secrets set in repo: `UW_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- [x] `archive.py` saves every day's full signal record to `signals/YYYY-MM-DD.json` — committed back to git after each run
- [x] `backtest.py` replays archives with alternative parameters (no UW re-pulls needed)
- [x] `sheet_sync.py` and `x_post.py` are gated by env vars — they NO-OP if creds missing

## 🟡 What YOU need to do for full Phase 2 — in order

### Step 1 (REQUIRED — 5 min): GH_PAT secret for cross-repo checkout

Workflows need to read `skew_history.db` from your `skew-tracker` repo. GH Actions default token can't access other repos — needs a Personal Access Token.

1. Go to https://github.com/settings/personal-access-tokens/new
2. **Fine-grained PAT** → repository access: **Only select repositories** → pick `skew-tracker`
3. Permissions → Repository → **Contents: Read-only**
4. Expiry: 1 year
5. Copy the token (starts with `github_pat_...`)
6. Add to tier-a-daily repo:
   ```bash
   echo "github_pat_YOUR_TOKEN_HERE" | gh secret set GH_PAT --repo PredragSaponjac/tier-a-daily
   ```

**Without this, the daily workflow will fail.** This is the only blocker for tomorrow's automated run.

### Step 2 (10 min): Public Telegram channel

1. Telegram → New Channel → Public → name: "Tier A Daily" → handle `@TierADailySignals` (or your pick)
2. Add a description: "Daily Tier A options signals. Honest track record. Not financial advice."
3. Settings → Administrators → Add Admin → search for your bot (the one your `TELEGRAM_BOT_TOKEN` represents) → grant "Post messages"
4. Post one message in the channel (any text)
5. Forward that message to @userinfobot → it returns the channel ID (looks like `-1001234567890`)
6. Add as secret:
   ```bash
   echo "-1001234567890" | gh secret set TELEGRAM_CHANNEL_ID --repo PredragSaponjac/tier-a-daily
   ```
7. To make bot post to CHANNEL instead of your private chat, update workflows to use `TELEGRAM_CHANNEL_ID` env var. Or for now, keep private testing and flip later.

### Step 3 (15 min): Google Sheet for public track record

1. Create new Google Sheet → name: "Tier A Daily — Live Track Record"
2. File → Share → Add `orca-sheet-bot@vix-code.iam.gserviceaccount.com` → Editor → Send
3. File → Share → "Anyone with the link can view" (public read)
4. Copy sheet ID from URL (the long string between `/d/` and `/edit`)
5. Add secrets:
   ```bash
   echo "1abc...XYZ" | gh secret set GOOGLE_SHEET_ID --repo PredragSaponjac/tier-a-daily

   # Base64-encode your existing service account creds JSON
   base64 -w0 path/to/orca-sheet-bot-creds.json | gh secret set GOOGLE_SHEETS_CREDS_BASE64 --repo PredragSaponjac/tier-a-daily
   ```
6. After first signal goes through, sheet auto-populates with two tabs:
   - **Track Record**: every closed signal with MAE/MFE
   - **Open Positions**: currently monitored

### Step 4 (20 min): X (Twitter) account

1. Create @TierADaily or similar account on X
2. Apply for X API at https://developer.x.com → Free tier (100 reads, 500 writes/month — plenty for daily signals)
3. Get the 4 keys (API Key, API Secret, Access Token, Access Secret)
4. Add as secrets:
   ```bash
   echo "..." | gh secret set X_API_KEY --repo PredragSaponjac/tier-a-daily
   echo "..." | gh secret set X_API_SECRET --repo PredragSaponjac/tier-a-daily
   echo "..." | gh secret set X_ACCESS_TOKEN --repo PredragSaponjac/tier-a-daily
   echo "..." | gh secret set X_ACCESS_SECRET --repo PredragSaponjac/tier-a-daily
   ```
5. Bot will auto-post signals to X (gated by env presence — silently skips if not set)

## How the GH Actions workflows fire

**`tier_a_daily.yml`** — main run:
- Cron: `45 20 * * 1-5` = **20:45 UTC = 3:45 PM CDT** (Mar-Nov), Mon-Fri
- Steps: checkout tier-a-daily, checkout skew-tracker (via GH_PAT), run `python main.py --require-today`, commit `signals/`, `open_positions.json`, `track_record.csv` back to git
- Manual trigger via Actions tab → "Run workflow" with optional `--scan-date` override

**`tier_a_monitor.yml`** — intraday close monitor:
- Cron: `*/15 14-21 * * 1-5` = **every 15 min between 14:00-21:00 UTC** (9:00 AM - 4:00 PM CDT), Mon-Fri
- Skips if `open_positions.json` is empty (saves API costs)
- Otherwise: runs `python monitor.py`, sends close alerts on TP1/stop, commits state

## 🍂 DST handoff (Nov 2, 2026)

Both cron times shift +1 UTC when CDT → CST:
- `tier_a_daily.yml`: `45 20` → `45 21`
- `tier_a_monitor.yml`: `*/15 14-21` → `*/15 15-22`

Set a reminder for Nov 1, 2026.

## How to retrospectively backtest after 30+ days

```bash
cd tier-a-daily
git pull   # get latest signals/*.json archives committed by the bot

# Replay with current parameters
python backtest.py

# Test alternative: tighter stop
python backtest.py --stop -5.0

# Test: higher min-score threshold
python backtest.py --min-score 2

# Test: tighter NCP z-score requirement
python backtest.py --ncp-z -1.0

# Test: only signals from a date onward
python backtest.py --since 2026-07-01
```

This uses the PRESERVED raw UW metrics in each archive — no UW re-pulls needed. You can replay 6 months of data in seconds with any parameter combination.

## What's still Phase 3 (later)

- `monthly_self_review.py` — runs on 1st of each month, computes MAE/MFE distributions, suggests parameter changes, posts to private Telegram, awaits Y/N approval, auto-updates `parameters.json`
- This is the "self-learning" layer — runs AFTER enough live data accumulated (n=20+ closed signals minimum)

## File map after Phase 2

```
tier-a-daily/
├── .env                       (local only, NEVER pushed)
├── .github/workflows/
│   ├── tier_a_daily.yml       (3:45 PM CT cron, runs main.py)
│   └── tier_a_monitor.yml     (every 15min market hours)
├── parameters.json            (tunables, single source of truth)
├── signals/                   (per-day archive — committed automatically)
│   └── 2026-MM-DD.json
├── open_positions.json        (live state — committed automatically)
├── track_record.csv           (closed signals + MAE/MFE — committed automatically)
├── uw_cache/                  (gitignored, rebuilt on demand)
├── *.py                       (all the code)
└── *.md                       (docs)
```

## Bottom line

After Step 1 above, **the system is fully autonomous tomorrow**: GH Actions cron fires at 3:45 PM CT, posts to your Telegram, archives the day. Phase 3 self-learning unlocks once 30+ days of data accumulate.

Steps 2-4 (channel, sheet, X) are optional for tomorrow — bot still works without them, just less public.
