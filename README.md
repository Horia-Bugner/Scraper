# Job Scraper

Checks Jobicy, We Work Remotely, Remotive, Remote OK, Himalayas, Arbeitnow,
and EU Careers for QA Manager, Test Lead, Test Manager, QA Director, and
Software QA Manager openings. Results are limited to remote roles available
from Barcelona or Barcelona-based roles and favor listings requiring at least
five years of experience. Matching jobs are posted to Discord via webhook.

Runs automatically at 07:59, 13:29, and 20:19 Europe/Bucharest time via
GitHub Actions, including daylight-saving-time changes.

LinkedIn is not scraped because its terms prohibit unauthorized automated
scraping. Use LinkedIn's own job alerts alongside this scraper.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # then fill in DISCORD_WEBHOOK_URL
python main.py
```

## Adding a Discord webhook

1. In your Discord server, go to **Server Settings → Integrations → Webhooks**.
2. Create a webhook pointed at the channel you want notifications in.
3. Copy the webhook URL into `.env` (locally) or as a GitHub Actions secret
   (for deployment — see below).

## Adding a job source

Add a new scraper function in `main.py` following the shape of
`scrape_example_site()`, and register it in the `SCRAPERS` list. Each
scraper should return a list of dicts with `id`, `title`, `url`, and
`company`. The `id` should be stable and unique (a job URL works well) —
it's what prevents duplicate notifications.

## Deploying (GitHub Actions)

1. Push this repo to GitHub.
2. Go to **Settings → Secrets and variables → Actions** in your repo.
3. Add a repository secret named `DISCORD_WEBHOOK_URL` with your webhook URL.
4. The workflow in `.github/workflows/scrape.yml` will run automatically at
   07:59, 13:29, and 20:19 Europe/Bucharest time. You can also trigger it
   manually from the **Actions** tab.

Each run scrapes, diffs against `seen_jobs.json` to avoid duplicate
alerts, notifies Discord about new listings, and commits the updated
`seen_jobs.json` back to the repo so state persists between runs.
