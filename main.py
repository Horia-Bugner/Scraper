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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()  # loads .env locally; no-op in CI (secrets come from env vars there)

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"
RUN_LOG_FILE = Path(__file__).parent / "scraper_log.md"
RUN_AUDIT: dict[str, dict] = {}

HEADERS = {"User-Agent": "Mozilla/5.0 (personal job-alert script)"}

# Target roles supplied by the user. Keep this deliberately narrow so generic
# engineering leads and unrelated "quality manager" roles do not get posted.
TARGET_TITLE_PATTERN = re.compile(
    r"\b(?:"
    r"(?:Software )?QA Manager|"
    r"Quality Assurance Manager|"
    r"Test Lead|"
    r"Test Manager|"
    r"QA Director|"
    r"Director of (?:QA|Quality Assurance)"
    r")\b",
    re.IGNORECASE,
)
TARGET_LOCATION_PATTERN = re.compile(r"\bBarcelona\b|\bRemote\b", re.IGNORECASE)
BARCELONA_REMOTE_REGIONS = re.compile(
    r"\b(?:"
    r"Anywhere|Worldwide|Global|Europe|European Union|EU|EMEA|"
    r"Spain|España|Barcelona"
    r")\b",
    re.IGNORECASE,
)
CLOSED_JOB_PATTERN = re.compile(
    r"\b(?:"
    r"no longer accepting applications|"
    r"no longer available|"
    r"position (?:has been|is) filled|"
    r"job (?:has )?expired|"
    r"job is closed|"
    r"applications? (?:are |is )?closed|"
    r"vacancy (?:has been|is) closed|"
    r"this job was removed"
    r")\b",
    re.IGNORECASE,
)


def is_target_title(title: str) -> bool:
    return bool(TARGET_TITLE_PATTERN.search(title))


def accepts_barcelona_remote_location(location: str) -> bool:
    """Return whether a remote job accepts applicants based in Barcelona."""
    return not location.strip() or bool(BARCELONA_REMOTE_REGIONS.search(location))


def audit_listing(
    source: str,
    title: str,
    company: str,
    url: str,
    location_ok: bool = True,
    specific_job: bool = True,
    active_job: bool = True,
) -> bool:
    """Record every inspected listing and return whether it passes filters."""
    if not specific_job:
        result = "Skipped: not a specific job page"
        matched = False
    elif not active_job:
        result = "Skipped: closed or expired"
        matched = False
    elif not is_target_title(title):
        result = "Skipped: title"
        matched = False
    elif not location_ok:
        result = "Skipped: location"
        matched = False
    else:
        result = "Matched"
        matched = True
    key = f"{source}|{url or company + '|' + title}"
    RUN_AUDIT[key] = {
        "source": source,
        "title": title or "(missing title)",
        "company": company or "Unknown",
        "url": url,
        "result": result,
    }
    return matched


def normalize_for_dedup(value: str) -> str:
    """Normalize text so punctuation/case differences do not create duplicates."""
    ascii_value = unicodedata.normalize("NFKD", value).encode(
        "ascii", "ignore"
    ).decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def job_keys(job: dict) -> set[str]:
    """
    Track both the source ID and a title/company fingerprint. The fingerprint
    catches the same vacancy when multiple job boards publish different URLs.
    """
    title = normalize_for_dedup(job.get("title", ""))
    company = normalize_for_dedup(job.get("company", ""))
    keys = {
        str(job["id"]),  # legacy key retained for existing seen_jobs.json files
        f"id:{job['id']}",
    }
    if company and company != "unknown":
        keys.add(f"job:{company}|{title}")
    return keys


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
        location = item.get("location") or ""
        url = item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('id')}"
        company = item.get("company", "Unknown")
        if not audit_listing(
            "Remote OK", title, company, url,
            accepts_barcelona_remote_location(location),
        ):
            continue
        jobs.append({
            "id": url,
            "title": title,
            "url": url,
            "company": company,
            "source": "Remote OK",
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
            if not audit_listing(
                "EU Careers", title, institution, url,
                bool(TARGET_LOCATION_PATTERN.search(listing_text)),
            ):
                continue

            jobs.append({
                "id": url,
                "title": title,
                "url": url,
                "company": institution,
                "source": "EU Careers",
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
        location = item.get("jobGeo", "")
        url = item.get("url", "")
        company = item.get("companyName", "Unknown")
        if not audit_listing(
            "Jobicy", title, company, url,
            accepts_barcelona_remote_location(location),
        ):
            continue
        jobs.append({
            "id": url,
            "title": title,
            "url": url,
            "company": company,
            "source": "Jobicy",
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
        url = (item.findtext("link") or "").strip()
        company = raw_title.split(":", 1)[0].strip() if ":" in raw_title else "Unknown"
        if not audit_listing("We Work Remotely", title, company, url):
            continue
        jobs.append({
            "id": url,
            "title": title,
            "url": url,
            "company": company,
            "source": "We Work Remotely",
        })
    return jobs


def scrape_remotive() -> list[dict]:
    """Fetch remote listings from Remotive's public API."""
    resp = requests.get(
        "https://remotive.com/api/remote-jobs",
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()

    jobs = []
    for item in resp.json().get("jobs", []):
        title = item.get("title", "")
        location = item.get("candidate_required_location", "")
        url = item.get("url", "")
        company = item.get("company_name", "Unknown")
        if not audit_listing(
            "Remotive", title, company, url,
            accepts_barcelona_remote_location(location),
        ):
            continue
        jobs.append({
            "id": f"remotive:{item.get('id') or url}",
            "title": title,
            "url": url,
            "company": company,
            "source": "Remotive",
        })
    return jobs


def scrape_himalayas() -> list[dict]:
    """Search Himalayas for each target title, including Spain/worldwide jobs."""
    jobs_by_id = {}
    searches = [
        "QA Manager",
        "Test Lead",
        "Test Manager",
        "QA Director",
        "Software QA Manager",
    ]
    for search in searches:
        resp = requests.get(
            "https://himalayas.app/jobs/api/search",
            params={"q": search, "country": "ES", "sort": "recent"},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        for item in resp.json().get("jobs", []):
            title = item.get("title", "")
            url = item.get("applicationLink", "")
            company = item.get("companyName", "Unknown")
            if not audit_listing("Himalayas", title, company, url):
                continue
            job_id = str(item.get("guid") or url)
            jobs_by_id[job_id] = {
                "id": f"himalayas:{job_id}",
                "title": title,
                "url": url,
                "company": company,
                "source": "Himalayas",
            }
    return list(jobs_by_id.values())


def scrape_arbeitnow(max_pages: int = 3) -> list[dict]:
    """Fetch recent remote and Barcelona jobs from Arbeitnow's public API."""
    jobs = []
    url = "https://www.arbeitnow.com/api/job-board-api"
    for page in range(1, max_pages + 1):
        resp = requests.get(
            url,
            params={"page": page},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        listings = resp.json().get("data", [])
        if not listings:
            break
        for item in listings:
            title = item.get("title", "")
            location = item.get("location", "")
            is_remote = bool(item.get("remote"))
            job_url = item.get("url", "")
            company = item.get("company_name", "Unknown")
            location_ok = (
                bool(TARGET_LOCATION_PATTERN.search(location))
                if not is_remote
                else accepts_barcelona_remote_location(location)
            )
            if not audit_listing(
                "Arbeitnow", title, company, job_url, location_ok
            ):
                continue
            jobs.append({
                "id": f"arbeitnow:{item.get('slug') or job_url}",
                "title": title,
                "url": job_url,
                "company": company,
                "source": "Arbeitnow",
            })
    return jobs


TAVILY_JOB_SOURCES = {
    "LinkedIn": ["linkedin.com"],
    "Xing": ["xing.com"],
    "StepStone": ["stepstone.es", "stepstone.de", "stepstone.com"],
    "Indeed": ["indeed.com", "indeed.es"],
    "Glassdoor": ["glassdoor.com", "glassdoor.es"],
}


def is_specific_job_page(source: str, url: str) -> bool:
    """Reject search/category pages while retaining individual job pages."""
    lowered = url.lower()
    patterns = {
        "LinkedIn": ("/jobs/view/",),
        "Xing": ("/jobs/",),
        "StepStone": ("/stellenangebote--", "/job/", "/jobs/"),
        "Indeed": ("viewjob", "/rc/clk", "jk="),
        "Glassdoor": ("/job-listing/", "joblisting"),
    }
    if not any(marker in lowered for marker in patterns[source]):
        return False
    path = urlparse(url).path.rstrip("/").lower()
    return path not in {"", "/jobs", "/job", "/jobs/search"}


def company_from_search_title(title: str, source: str) -> str:
    """Best-effort company extraction from a search-result page title."""
    cleaned = re.sub(
        rf"\s*[|\-–—]\s*{re.escape(source)}.*$", "", title, flags=re.IGNORECASE
    ).strip()
    match = re.search(r"\s+(?:at|bei)\s+(.+)$", cleaned, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    parts = [part.strip() for part in re.split(r"\s+[|–—]\s+", cleaned)]
    return parts[-1] if len(parts) > 1 else "Unknown"


def scrape_tavily() -> list[dict]:
    """Find publicly indexed job pages on five major boards via Tavily."""
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY is not configured")

    query = (
        '("QA Manager" OR "Quality Assurance Manager" OR "Test Lead" OR '
        '"Test Manager" OR "QA Director" OR "Software QA Manager") '
        '(Barcelona OR remote OR Spain OR Europe) job'
    )
    jobs = []
    for source, domains in TAVILY_JOB_SOURCES.items():
        response = requests.post(
            "https://api.tavily.com/search",
            headers=HEADERS,
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": 10,
                "include_domains": domains,
                "include_answer": False,
                "include_images": False,
                "include_raw_content": "text",
                "time_range": "month",
            },
            timeout=30,
        )
        response.raise_for_status()
        for item in response.json().get("results", []):
            title = item.get("title", "").strip()
            url = item.get("url", "").strip()
            content = item.get("content", "") or ""
            raw_content = item.get("raw_content", "") or ""
            page_text = f"{title} {content} {raw_content}"
            company = company_from_search_title(title, source)
            location_ok = accepts_barcelona_remote_location(page_text)
            if not audit_listing(
                f"Tavily / {source}",
                title,
                company,
                url,
                location_ok=location_ok,
                specific_job=is_specific_job_page(source, url),
                active_job=not bool(CLOSED_JOB_PATTERN.search(page_text)),
            ):
                continue
            jobs.append({
                "id": f"tavily:{source.lower()}:{url}",
                "title": title,
                "url": url,
                "company": company,
                "source": f"Tavily / {source}",
            })
    return jobs


SCRAPERS = [
    scrape_remoteok,
    scrape_jobicy,
    scrape_weworkremotely,
    scrape_remotive,
    scrape_himalayas,
    scrape_arbeitnow,
    scrape_eu_institutions,
    scrape_tavily,
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

def notify_discord(jobs: list[dict]) -> list[dict]:
    if not DISCORD_WEBHOOK_URL:
        print("WARNING: DISCORD_WEBHOOK_URL not set, skipping notification.")
        return []

    notified = []
    for job in jobs:
        source = job.get("source", "Unknown source")
        content = (
            f"**{job['title']}** at {job['company']}\n"
            f"Source: {source}\n{job['url']}"
        )
        try:
            resp = requests.post(
                DISCORD_WEBHOOK_URL,
                json={"content": content},
                timeout=15,
            )
            resp.raise_for_status()
            notified.append(job)
        except requests.RequestException as exc:
            print(
                f"Failed to notify for job {job['id']}: {exc}",
                file=sys.stderr,
            )
    return notified


def write_run_log(matched_jobs, new_jobs, notified_jobs, scraper_errors) -> None:
    """Write a readable audit of every listing inspected during this run."""
    lines = [
        "# Job scraper run log", "",
        f"- Run time (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Listings inspected: {len(RUN_AUDIT)}",
        f"- Listings matching title/location: {len(matched_jobs)}",
        f"- New listings: {len(new_jobs)}",
        f"- Discord job notifications delivered: {len(notified_jobs)}",
        f"- Scraper errors: {len(scraper_errors)}",
        "- Experience filtering: disabled", "",
    ]
    if scraper_errors:
        lines.extend(["## Errors", ""])
        lines.extend(f"- {error}" for error in scraper_errors)
        lines.append("")
    lines.extend([
        "## Listings inspected", "",
        "| Source | Result | Company | Title | Link |",
        "|---|---|---|---|---|",
    ])
    for entry in sorted(
        RUN_AUDIT.values(),
        key=lambda value: (value["source"], value["company"], value["title"]),
    ):
        clean = {
            key: str(value).replace("|", "\\|").replace("\n", " ")
            for key, value in entry.items()
        }
        link = f"[Open]({clean['url']})" if clean["url"] else ""
        lines.append(
            f"| {clean['source']} | {clean['result']} | {clean['company']} | "
            f"{clean['title']} | {link} |"
        )
    RUN_LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def notify_run_complete(matched_count, new_count, notified_count, error_count) -> None:
    """Send one status message for every completed scraper run."""
    if not DISCORD_WEBHOOK_URL:
        return
    status = "completed" if error_count == 0 else "completed with errors"
    content = (
        f"✅ **Job scraper {status}**\n"
        f"Inspected: {len(RUN_AUDIT)} | Matched: {matched_count} | "
        f"New: {new_count} | Posted: {notified_count} | Errors: {error_count}\n"
        "Full run log attached."
    )
    try:
        with RUN_LOG_FILE.open("rb") as log_file:
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                params={"wait": "true"},
                data={"payload_json": json.dumps({"content": content})},
                files={
                    "files[0]": (
                        RUN_LOG_FILE.name,
                        log_file,
                        "text/markdown",
                    )
                },
                timeout=30,
            )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Failed to send run status: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 4. Main run
# ---------------------------------------------------------------------------

def main() -> None:
    seen_ids = load_seen_ids()
    all_jobs: list[dict] = []
    scraper_errors = []

    for scraper in SCRAPERS:
        try:
            all_jobs.extend(scraper())
        except Exception as e:
            error = f"{scraper.__name__}: {e}"
            scraper_errors.append(error)
            print(f"Scraper failed: {error}", file=sys.stderr)

    new_jobs = []
    keys_this_run = set(seen_ids)
    for job in all_jobs:
        keys = job_keys(job)
        if keys_this_run.isdisjoint(keys):
            new_jobs.append(job)
            keys_this_run.update(keys)

    notified_jobs = []
    if new_jobs:
        print(f"Found {len(new_jobs)} new job(s).")
        notified_jobs = notify_discord(new_jobs)
        for job in notified_jobs:
            seen_ids.update(job_keys(job))
        if notified_jobs:
            save_seen_ids(seen_ids)
    else:
        print("No new jobs found.")

    write_run_log(all_jobs, new_jobs, notified_jobs, scraper_errors)
    notify_run_complete(
        len(all_jobs), len(new_jobs), len(notified_jobs), len(scraper_errors)
    )


if __name__ == "__main__":
    main()
