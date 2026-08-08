# Job Scraper

Checks Jobicy, We Work Remotely, Remotive, Remote OK, Himalayas, Arbeitnow,
and EU Careers directly. Tavily adds publicly indexed results from LinkedIn,
Xing, StepStone, Indeed, and Glassdoor. The scraper looks for QA Manager,
Test Lead, Test Manager, QA Director, and Software QA Manager openings.
Results are limited to remote roles available from Barcelona or
Barcelona-based roles. Experience requirements do not exclude roles.
Matching jobs are posted to Discord via webhook.

Every run sends a Discord status message, even when no new jobs are found.
The message includes `scraper_log.md` as an attachment, listing every
inspected role and the reason it matched or was skipped. A local run also
leaves the latest copy of that file in the project folder.

Runs automatically at 07:20 and 20:20 Europe/Bucharest time via
GitHub Actions, including daylight-saving-time changes.

The scraper does not log into or directly crawl those five commercial boards;
it processes pages returned by Tavily's public web-search index. Tavily
results are limited to the last month and explicit closed/expired notices are
excluded before notification.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # then fill in the webhook and Tavily key
python main.py
```

## Adding a Discord webhook

1. In your Discord server, go to **Server Settings → Integrations → Webhooks**.
2. Create a webhook pointed at the channel you want notifications in.
3. Copy the webhook URL into `.env` (locally) or as a GitHub Actions secret
   (for deployment — see below).

## Adding Tavily

1. Create a free API key at [Tavily](https://app.tavily.com/).
2. Put it in `.env` locally as `TAVILY_API_KEY`.
3. Add it to GitHub Actions as a repository secret named `TAVILY_API_KEY`.

The integration uses five basic searches per run. At two runs per day this
uses approximately 300 of Tavily's 1,000 free monthly credits.

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
4. Add a repository secret named `TAVILY_API_KEY` with your Tavily API key.
5. The workflow in `.github/workflows/scrape.yml` will run automatically at
   07:20 and 20:20 Europe/Bucharest time. You can also trigger it
   manually from the **Actions** tab.

Each run scrapes, diffs against `seen_jobs.json` to avoid duplicate
alerts, notifies Discord about new listings, and commits the updated
`seen_jobs.json` back to the repo so state persists between runs.
