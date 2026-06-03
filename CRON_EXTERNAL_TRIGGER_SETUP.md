# External Trigger Setup — cron-job.org → GitHub workflow_dispatch

**Why:** GitHub's free-tier `schedule:` cron drops ~90% of runs under load
(verified 2026-05-29: AM cron 91 min overdue + never fired, intraday monitor
fired 3×/day instead of 32×). Our YAML/branch/cron are all correct — GitHub's
scheduler itself is unreliable. The `workflow_dispatch` (manual/API) trigger
ALWAYS works. cron-job.org calls that API at exact times, bypassing the flaky
scheduler. Fires within ~5 sec, every time.

The `schedule:` blocks stay in the YAML as a free backup (if GitHub happens to
fire, no harm — the run is idempotent for the day). cron-job.org is the
primary, reliable trigger.

---

## PART A — GitHub fine-grained PAT (YOU create this; I cannot make tokens)

1. Go to: https://github.com/settings/personal-access-tokens/new
2. **Token name:** `cron-job-tier-a-dispatch`
3. **Expiration:** 1 year (or custom max). Set a calendar reminder to rotate.
4. **Resource owner:** PredragSaponjac
5. **Repository access:** "Only select repositories" → pick **tier-a-daily**
6. **Permissions** → expand "Repository permissions":
   - **Actions** → set to **Read and write**
   - (leave everything else "No access" — this is the ONLY permission needed)
7. Click **Generate token**. Copy it (starts `github_pat_…`).
   - You'll paste it into cron-job.org in Part B. Do NOT paste it in chat.

---

## PART B — cron-job.org (YOU create the account + jobs)

1. Sign up free: https://cron-job.org → create account → verify email.
2. Dashboard → **Create cronjob**. Make TWO jobs:

### JOB 1 — Skew AM (9:30 AM CT, Mon–Fri)
- **Title:** `Tier A — Skew AM`
- **URL:**
  `https://api.github.com/repos/PredragSaponjac/tier-a-daily/actions/workflows/skew_am.yml/dispatches`
- **Schedule tab:**
  - Timezone: **America/Chicago** (handles DST automatically)
  - Days of week: **Mon, Tue, Wed, Thu, Fri** only
  - Time: **09:30**
- **Advanced / Request tab:**
  - Request method: **POST**
  - Headers (add three):
    - `Accept` = `application/vnd.github+json`
    - `Authorization` = `Bearer github_pat_…`  ← your PAT from Part A
    - `X-GitHub-Api-Version` = `2022-11-28`
  - Request body:
    `{"ref":"master"}`
- Save.

### JOB 2 — Skew PM (2:45 PM CT, Mon–Fri)
- **Title:** `Tier A — Skew PM`
- **URL:**
  `https://api.github.com/repos/PredragSaponjac/tier-a-daily/actions/workflows/skew_pm.yml/dispatches`
- **Schedule tab:**
  - Timezone: **America/Chicago**
  - Days: **Mon–Fri**
  - Time: **14:45**
- **Request tab:** same 3 headers + same `{"ref":"master"}` body + POST.
- Save.

---

## VERIFY
- In cron-job.org, each job has an "Execute now" / "Test run" button.
- A successful dispatch returns **HTTP 204 No Content** (that's success — GitHub
  returns empty 204 for a good dispatch).
- After a test run, check: https://github.com/PredragSaponjac/tier-a-daily/actions
  — a new "Skew AM/PM" run should appear within ~5 sec, triggered by
  `workflow_dispatch`.

## DST NOTE
Using timezone America/Chicago means cron-job.org auto-adjusts for CST/CDT.
No need to ever shift the times (unlike the UTC cron in the YAML, which would
need the Nov 2 2026 shift). One less thing to maintain.
