"""
Job Scraper
Checks configured job sources, finds new listings, and posts them to a
Discord channel via webhook. Designed to run periodically (e.g. via
GitHub Actions) rather than as a long-running process.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()  # loads .env locally; no-op in CI (secrets come from env vars there)

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (personal job-alert script)"}

# Target roles supplied by the user. Keep this deliberately narrow so generic
# engineering leads and unrelated "quality manager" roles do not get posted.
TARGET_TITLE_PATTERN = re.compile(
    r"\b(?:QA Manager|Test Lead|Test Manager|QA Director|Software QA Manager)\b",
    re.IGNORECASE,
)
TARGET_LOCATION_PATTERN = re.compile(r"\bBarcelona\b|\bRemote\b", re.IGNORECASE)


def is_target_title(title: str) -> bool:
    return bool(TARGET_TITLE_PATTERN.search(title))


def meets_experience_preference(text: str) -> bool:
    """Reject only listings that explicitly cap experience below five years."""
    years = [
        int(value)
        for value in re.findall(
            r"\b(\d{1,2})\+?\s*(?:years?|yrs?)\b", text, re.IGNORECASE
        )
    ]
    return not years or max(years) >= 5


# ---------------------------------------------------------------------------
# 1. Scrapers — one function per site. Each must return a list of dicts:
#    {"id": <unique str>, "title": <str>, "url": <str>, "company": <str>}
# The "id" is what dedup is based on, so make it stable (e.g. the job URL,
# or a site-specific job ID if one exists).
# ---------------------------------------------------------------------------

def scrape_remoteok() -> list[dict]:
    """
    RemoteOK has a free public JSON API (no auth needed): https://remoteok.com/api
    The first element of the response is a legal/metadata notice, not a job —
    skip it. Filters down to remote QA Manager/Director-level roles.
    """
    resp = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    listings = resp.json()

    jobs = []
    for item in listings:
        title = item.get("position") or item.get("title") or ""
        if not is_target_title(title):
            continue
        url = item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('id')}"
        jobs.append({
            "id": url,
            "title": title,
            "url": url,
            "company": item.get("company", "Unknown"),
        })
    return jobs


def scrape_eu_institutions(max_pages: int = 3) -> list[dict]:
    """
    Scrapes the EU Careers temporary/contract vacancy listings. This table
    covers roles across all EU institutions, bodies, and agencies (European
    Commission, Council, Parliament, ECDC, EEAS, etc.) — no filtering by
    institution, so every posted role is included.
    """
    base_url = "https://eu-careers.europa.eu/en/temporary-agents-other-institutions-vacancies"
    jobs = []

    for page in range(max_pages):
        params = {"page": page} if page > 0 else {}
        resp = requests.get(base_url, headers=HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        table = soup.find("table")
        if not table:
            break

        rows = table.find("tbody").find_all("tr") if table.find("tbody") else []
        if not rows:
            break

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            title_cell = cells[0]
            link = title_cell.find("a")
            title = link.get_text(strip=True) if link else title_cell.get_text(strip=True)
            href = link["href"] if link else ""
            url = href if href.startswith("http") else f"https://eu-careers.europa.eu{href}"

            institution = cells[3].get_text(strip=True)
            listing_text = row.get_text(" ", strip=True)
            if not is_target_title(title):
                continue
            if not TARGET_LOCATION_PATTERN.search(listing_text):
                continue

            jobs.append({
                "id": url,
                "title": title,
                "url": url,
                "company": institution,
            })

    return jobs


def scrape_jobicy() -> list[dict]:
    """
    Jobicy has a free public JSON API with a dedicated "qa-testing" industry
    filter, so we can ask for relevant jobs directly instead of filtering
    client-side. Still narrows to Manager/Director-level titles, since the
    category includes QA roles at all seniority levels.
    """
    resp = requests.get(
        "https://jobicy.com/api/v2/remote-jobs",
        params={"count": 50, "industry": "qa-testing"},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for item in data.get("jobs", []):
        title = item.get("jobTitle", "")
        description = BeautifulSoup(
            item.get("jobDescription", ""), "html.parser"
        ).get_text(" ", strip=True)
        if not is_target_title(title):
            continue
        if not meets_experience_preference(description):
            continue
        url = item.get("url", "")
        jobs.append({
            "id": url,
            "title": title,
            "url": url,
            "company": item.get("companyName", "Unknown"),
        })
    return jobs


def scrape_weworkremotely() -> list[dict]:
    """
    We Work Remotely doesn't have a dedicated QA category, so this pulls the
    all-jobs RSS feed and filters by title client-side. Parsed with the
    standard library (no extra dependency needed for basic RSS).
    """
    import xml.etree.ElementTree as ET

    resp = requests.get("https://weworkremotely.com/remote-jobs.rss", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    jobs = []
    for item in root.findall(".//item"):
        raw_title = (item.findtext("title") or "").strip()
        # WWR titles are usually formatted "Company: Job Title"
        title = raw_title.split(":", 1)[-1].strip() if ":" in raw_title else raw_title
        description = item.findtext("description") or ""
        if not is_target_title(title):
            continue
        if not meets_experience_preference(description):
            continue
        url = (item.findtext("link") or "").strip()
        company = raw_title.split(":", 1)[0].strip() if ":" in raw_title else "Unknown"
        jobs.append({
            "id": url,
            "title": title,
            "url": url,
            "company": company,
        })
    return jobs


SCRAPERS = [
    scrape_jobicy,
    scrape_weworkremotely,
    scrape_eu_institutions,
    # add more scraper functions here as you build them out
]


# ---------------------------------------------------------------------------
# 2. Dedup: track which job IDs we've already notified about
# ---------------------------------------------------------------------------

def load_seen_ids() -> set[str]:
    if not SEEN_JOBS_FILE.exists():
        return set()
    with open(SEEN_JOBS_FILE, "r") as f:
        return set(json.load(f))


def save_seen_ids(ids: set[str]) -> None:
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2)


# ---------------------------------------------------------------------------
# 3. Notification
# ---------------------------------------------------------------------------

def notify_discord(jobs: list[dict]) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("WARNING: DISCORD_WEBHOOK_URL not set, skipping notification.")
        return

    for job in jobs:
        content = f"**{job['title']}** at {job['company']}\n{job['url']}"
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
        if resp.status_code >= 300:
            print(f"Failed to notify for job {job['id']}: "
                  f"{resp.status_code} {resp.text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 4. Main run
# ---------------------------------------------------------------------------

def main() -> None:
    seen_ids = load_seen_ids()
    all_jobs: list[dict] = []

    for scraper in SCRAPERS:
        try:
            all_jobs.extend(scraper())
        except Exception as e:
            print(f"Scraper {scraper.__name__} failed: {e}", file=sys.stderr)

    new_jobs = [job for job in all_jobs if job["id"] not in seen_ids]

    if new_jobs:
        print(f"Found {len(new_jobs)} new job(s).")
        notify_discord(new_jobs)
        seen_ids.update(job["id"] for job in new_jobs)
        save_seen_ids(seen_ids)
    else:
        print("No new jobs found.")


if __name__ == "__main__":
    main()
