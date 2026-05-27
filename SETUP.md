# Tier A Daily — Setup Guide

## Prerequisites (Phase 1 MVP)

All credentials you already have from your local secrets store:
- `UW_API_KEY` (Unusual Whales)
- `TELEGRAM_BOT_TOKEN` (your existing "ORCA Alerts" bot)
- `TELEGRAM_CHAT_ID` (your private chat for testing)

Skew Tracker sibling repo at `C:/Users/18329/Downloads/skew-tracker/` with `skew_history.db`.

## Phase 1 — Local MVP (today)

### 1. Install dependencies

```bash
cd C:/Users/18329/Downloads/tier-a-daily
pip install -r requirements.txt
```

### 2. Set env vars

Copy `.env.example` to `.env` and fill in with YOUR actual values from your local credentials store. **Do NOT commit `.env` — it's gitignored for security reasons.**

```
UW_API_KEY=your_unusual_whales_key_here
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### 3. Daily run (after Skew Tracker PM scan completes)

```bash
# Dry run — see what would be sent without actually sending
python main.py --dry-run

# Live run — send to Telegram
python main.py

# Backtest a specific past day
python main.py --scan-date 2026-05-18 --dry-run
```

### 4. Recommended workflow

After your daily Skew Tracker AM + PM + scanner sequence:
```bash
# Your existing routine
python skew_tracker.py > skew_manual_pm.log
git add ... && git commit ... && git push
python label_candidates.py
git add ... && git commit ... && git push

# NEW: append Tier A Daily run
cd ../tier-a-daily
python main.py
```

## Phase 2 (after MVP validated — likely next session)

### A. Public Telegram channel
1. Telegram → Create new channel → "Tier A Daily" (public)
2. Add @YourBot as admin (the bot from `TELEGRAM_BOT_TOKEN`)
3. Get channel ID: post a message in channel, forward to @userinfobot
4. Add to `.env`:
   ```
   TELEGRAM_CHANNEL_ID=-100xxxxxxxxxx
   ```

### B. Public Google Sheet (track record + MAE/MFE)
1. Create new sheet "Tier A Daily — Live Track Record"
2. Share with your existing service account email (orca-sheet-bot@vix-code.iam.gserviceaccount.com)
3. Add sheet ID to `.env`: `GOOGLE_SHEET_ID=...`
4. Bot will auto-create columns: ticker, entry_date, entry, exit_date, exit, return, MAE_%, MFE_%, time_to_TP1, time_to_stop, time_to_MFE, time_to_MAE, filter_score, z_ncp, coi_pct, poi_pct, dp_blocks

### C. X account (free initially)
1. Create @TierADaily account
2. Apply for X API v2 access (free tier OK for ~50 posts/month, enough for daily signals + closes)
3. Add to `.env`:
   ```
   X_API_KEY=...
   X_API_SECRET=...
   X_ACCESS_TOKEN=...
   X_ACCESS_SECRET=...
   ```

### D. GitHub Actions cron
1. Create new public repo `PredragSaponjac/tier-a-daily`
2. Push local code: `git remote add origin ... && git push -u origin main`
3. Add all `.env` values as repo Secrets (Settings → Secrets and variables → Actions)
4. Create `.github/workflows/tier_a_daily.yml`:
   - Trigger after Skew Tracker PM run pushes (workflow_run dependency)
   - OR cron at 4pm CT Mon-Fri (after PM scan typically finishes)
5. Create `.github/workflows/tier_a_monitor.yml`:
   - Cron every 15 min during US market hours (9:30am-4pm ET = 14:30-21:00 UTC)
   - Checks all open signals for TP1/stop hits + tracks MAE/MFE

### E. Phase 3 monetization checklist (~Month 3-4)
- [ ] 30-min lawyer call ($300) — secure publisher exemption wording
- [ ] Stripe account + subscription product ($200/mo)
- [ ] Landing page (one-pager: track record, what you get, FAQ, signup)
- [ ] Money-back-first-month guarantee (your standard pattern)
- [ ] Cold email campaign to retail-trader leads (your standard Gmail-SMTP playbook)
- [ ] Pitch free Telegram subscribers to upgrade

## File layout

```
tier-a-daily/
├── README.md
├── SETUP.md (this file)
├── .env.example
├── .gitignore
├── requirements.txt
├── scanner_reader.py     # Read Tier A from skew_history.db
├── uw_client.py          # UW API auth + cache
├── uw_filter.py          # Composite filter scoring
├── vetoes.py             # Earnings, liquidity vetoes
├── alert.py              # Format + send Telegram
├── main.py               # Orchestrator
└── uw_cache/             # API response cache (gitignored)
```

## Known design choices (worth tracking)

1. **No "spot > put_wall" veto** — old methodology had this rule, but TER (5/18) was below wall and went +22%. UW composite filter caught it. Tracking with MAE/MFE going forward.
2. **TP1-default exit** — bot auto-closes at +10%. TP2/TP3 shown for traders who want to hold longer at their discretion.
3. **min-score=3 default** — only alerts if composite filter score >= 3/4. Below that = "no trade today" message.
4. **Liquidity floor = 500 total OI** across next 3 expiries. Conservative; may tune up if MVP data shows tradeable thresholds higher.
