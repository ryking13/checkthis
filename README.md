# eBay Alert Bot

Scans eBay every 5 minutes for 4 specific items and posts a Discord
alert when a Buy-It-Now listing is found under that item's price
threshold. Runs entirely on GitHub Actions - no PC needs to stay on.

## Currently tracking

| Item | Max price | Notes |
|---|---|---|
| AirPort Express A1392 | $20 | Buy It Now only |
| Codenames Deep Undercover | $20 | Buy It Now only |
| TI-84 Plus | $20 | Title must say "Plus" |
| TI-Nspire CX | $30 | Buy It Now only |

Edit the `ITEMS` list in `bot.py` to change items/prices/filters.

## One-time setup

### 1. Create the GitHub repo
Push this folder to a new **public** GitHub repository (public is
required for unlimited free GitHub Actions minutes on personal
accounts - private repos have a free minutes cap that a 5-minute
schedule would burn through quickly).

### 2. Add repo secrets
Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret name | Value |
|---|---|
| `EBAY_CLIENT_ID` | Your eBay Production App ID |
| `EBAY_CLIENT_SECRET` | Your eBay Production Cert ID |
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL |

### 3. Test manually first
Repo → Actions tab → "eBay Alert Scan" → "Run workflow" → watch for a
green checkmark. Check Discord for any alerts (there may be none if
nothing currently matches your thresholds - that's normal).

### 4. Set up cron-job.org for reliable 5-minute triggering
GitHub's built-in `schedule` trigger is not precise/reliable at 5-minute
intervals, so we trigger the workflow externally instead:

1. Create a free account at cron-job.org
2. Create a GitHub Personal Access Token:
   GitHub → Settings → Developer settings → Personal access tokens →
   Tokens (classic) → check the **workflow** scope only
3. In cron-job.org, create a new cron job:
   - **URL:** `https://api.github.com/repos/YOUR_USERNAME/YOUR_REPO/actions/workflows/ebay-scan.yml/dispatches`
   - **Schedule:** every 5 minutes
   - **Method:** POST
   - **Headers:** `Authorization: Bearer YOUR_GITHUB_TOKEN`, `Content-Type: application/json`
   - **Body:** `{"ref":"main"}`
4. Test Run should return **204 No Content**

## Ongoing maintenance

- **Edit items/prices:** change `ITEMS` in `bot.py`, commit/push - takes effect next run
- **Check activity:** repo → Actions tab
- **eBay free tier limit:** 5,000 calls/day (this bot uses ~4 calls per run, well within limits even at 5-minute intervals)
